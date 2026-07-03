"""
main.py
-------
Entry point for the Email Validation System.

Usage:
    python main.py <path_to_input.xlsx> [--workers N] [--domain-concurrency N]
                                         [--output-dir PATH]

Orchestration flow per email:
  1. Syntax validation   (syntax_validator)
  2. DNS / MX check      (dns_checker)
  3. SMTP verification   (smtp_validator)      -- throttled per domain
  4. Catch-all detection (catch_all_detector)

All emails are processed in parallel using a ThreadPoolExecutor, with a
per-domain semaphore to cap concurrent SMTP connections to any single mail
server (protects deliverability / avoids tripping rate limits or spam
defenses on the receiving end).

Results are saved to:
  - output/validation_results.xlsx
  - output/failed_emails.xlsx
  - output/summary_report.txt

Ctrl+C during a run stops issuing new work and writes out whatever was
completed so far, instead of losing the whole batch.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from catch_all_detector import is_catch_all, mark_catch_all
from config import (
    MAX_WORKERS,
    OUTPUT_DIR,
    STATUS_INVALID_DOMAIN,
    STATUS_INVALID_FORMAT,
    STATUS_VALID,
    SUMMARY_REPORT,
)
from dns_checker import check_domain, get_domain
from excel_handler import read_emails, write_failed_emails, write_results
from logger import get_logger
from smtp_validator import verify_mailbox
from syntax_validator import validate_syntax

log = get_logger(__name__)

# config.py may not define this yet on older setups - default to a
# conservative value that keeps SMTP probing polite rather than blasting
# a single mail server with dozens of simultaneous connections.
try:
    from config import MAX_CONCURRENT_PER_DOMAIN
except ImportError:
    MAX_CONCURRENT_PER_DOMAIN = 3

STATUS_UNKNOWN = "UNKNOWN"
STATUS_PENDING = "PENDING"  # never reached SMTP/catch-all before interrupt

# ── Shared, thread-safe state ───────────────────────────────────────────────

_catch_all_cache: dict[str, bool] = {}
_catch_all_lock = threading.Lock()

_domain_semaphores: dict[str, threading.Semaphore] = {}
_domain_semaphore_lock = threading.Lock()

_progress_lock = threading.Lock()
_progress_count = 0


def _get_domain_semaphore(domain: str) -> threading.Semaphore:
    """Return (creating if needed) the per-domain SMTP concurrency gate."""
    with _domain_semaphore_lock:
        sem = _domain_semaphores.get(domain)
        if sem is None:
            sem = threading.Semaphore(MAX_CONCURRENT_PER_DOMAIN)
            _domain_semaphores[domain] = sem
        return sem


def _get_catch_all_status(domain: str, mx_hosts: list[str]) -> bool:
    """
    Thread-safe, cached catch-all lookup. Avoids two threads probing the
    same domain simultaneously (double-checked locking).
    """
    if domain in _catch_all_cache:
        return _catch_all_cache[domain]
    with _catch_all_lock:
        if domain not in _catch_all_cache:
            _catch_all_cache[domain] = is_catch_all(domain, mx_hosts)
        return _catch_all_cache[domain]


def _verify_mailbox_with_retry(
    email: str, mx_hosts: list[str], attempts: int = 2, backoff_seconds: float = 1.5
) -> dict:
    """
    Wrap verify_mailbox with a small retry budget for transient failures
    (connection resets, temporary greylisting, timeouts). Only retries on
    unexpected exceptions - a clean INVALID/VALID/CATCH_ALL result from
    the validator is returned immediately, not retried.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return verify_mailbox(email, mx_hosts)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, network I/O
            last_exc = exc
            log.warning(
                "SMTP attempt %d/%d failed for %s: %s", attempt, attempts, email, exc
            )
            if attempt < attempts:
                time.sleep(backoff_seconds * attempt)
    return {
        "email": email,
        "status": STATUS_UNKNOWN,
        "reason": f"SMTP verification failed after {attempts} attempts: {last_exc}",
        "smtp_response": "",
    }


def validate_single(raw_email: str) -> dict:
    """
    Run all validation stages for a single email address.

    Parameters
    ----------
    raw_email : str
        Raw email string from the Excel cell.

    Returns
    -------
    dict
        Final result with keys: email, status, reason, smtp_response.
    """
    # ── Stage 1: Syntax ──────────────────────────────────────────────────
    result = validate_syntax(raw_email)
    if result["status"] == STATUS_INVALID_FORMAT:
        log.info("INVALID FORMAT | %s", result["email"])
        return result

    email = result["email"]

    # ── Stage 2: DNS / MX ────────────────────────────────────────────────
    dns_result = check_domain(email)
    if dns_result["status"] == STATUS_INVALID_DOMAIN:
        log.info("INVALID DOMAIN | %s | %s", email, dns_result["reason"])
        return dns_result

    mx_hosts = dns_result.get("mx_hosts", [])
    domain = get_domain(email)

    # ── Stage 3: SMTP mailbox verification (throttled per domain) ─────────
    semaphore = _get_domain_semaphore(domain)
    with semaphore:
        smtp_result = _verify_mailbox_with_retry(email, mx_hosts)

    # ── Stage 4: Catch-all detection (only if SMTP returned VALID) ────────
    if smtp_result["status"] == STATUS_VALID:
        with semaphore:
            catch_all = _get_catch_all_status(domain, mx_hosts)
        if catch_all:
            smtp_result = mark_catch_all(smtp_result)
            log.info("CATCH-ALL | %s", email)
        else:
            log.info("VALID | %s", email)
    else:
        log.info("%s | %s | %s", smtp_result["status"], email, smtp_result["reason"])

    return smtp_result


def _log_progress(total: int) -> None:
    """Thread-safe progress ticker, logged at ~10%% increments."""
    global _progress_count
    with _progress_lock:
        _progress_count += 1
        count = _progress_count
    step = max(1, total // 10)
    if count % step == 0 or count == total:
        pct = count / total * 100
        log.info("Progress: %d/%d (%.0f%%)", count, total, pct)


def validate_bulk(emails: list[str]) -> list[dict]:
    """
    Validate a list of emails using a thread pool for concurrency.

    Parameters
    ----------
    emails : list[str]
        Raw email addresses to validate.

    Returns
    -------
    list[dict]
        Results in the same order as the input list. If interrupted
        (Ctrl+C), unfinished entries are filled with a STATUS_PENDING
        placeholder rather than raising, so partial results can still
        be written out.
    """
    global _progress_count
    _progress_count = 0

    total = len(emails)
    results: list[dict | None] = [None] * total

    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    future_to_idx = {
        executor.submit(validate_single, email): idx
        for idx, email in enumerate(emails)
    }

    try:
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as exc:  # noqa: BLE001
                log.error("Unexpected error for '%s': %s", emails[idx], exc, exc_info=True)
                results[idx] = {
                    "email": emails[idx],
                    "status": STATUS_UNKNOWN,
                    "reason": f"Unexpected error: {exc}",
                    "smtp_response": "",
                }
            _log_progress(total)
    except KeyboardInterrupt:
        log.warning("Interrupted - cancelling remaining work and saving partial results…")
        for future in future_to_idx:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        for idx, res in enumerate(results):
            if res is None:
                results[idx] = {
                    "email": emails[idx],
                    "status": STATUS_PENDING,
                    "reason": "Run interrupted before this email was processed",
                    "smtp_response": "",
                }
        return results  # type: ignore[return-value]
    else:
        executor.shutdown(wait=True)

    return results  # type: ignore[return-value]


def write_summary(results: list[dict]) -> None:
    """
    Write a plain-text summary report to output/summary_report.txt.

    Parameters
    ----------
    results : list[dict]
        Full list of validation results.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    total = len(results)
    lines = [
        "=" * 50,
        "  EMAIL VALIDATION SUMMARY REPORT",
        "=" * 50,
        f"  Total Emails   : {total}",
        "-" * 50,
    ]
    for status, count in sorted(counts.items()):
        pct = count / total * 100 if total else 0
        lines.append(f"  {status:<22}: {count:>5}  ({pct:.1f}%)")
    lines += ["=" * 50, ""]

    report = "\n".join(lines)
    with open(SUMMARY_REPORT, "w", encoding="utf-8") as fh:
        fh.write(report)

    print("\n" + report)
    log.info("Summary report saved -> %s", SUMMARY_REPORT)


# ── CLI / entry point ────────────────────────────────────────────────────

def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Validate a spreadsheet of email addresses (syntax, DNS, SMTP, catch-all).",
    )
    parser.add_argument("input_file", help="Path to the input .xlsx file")
    parser.add_argument(
        "--workers", type=int, default=None, help=f"Override MAX_WORKERS (default: {MAX_WORKERS})"
    )
    parser.add_argument(
        "--domain-concurrency",
        type=int,
        default=None,
        help=f"Override max simultaneous SMTP connections per domain (default: {MAX_CONCURRENT_PER_DOMAIN})",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args(sys.argv[1:])

    global MAX_WORKERS, MAX_CONCURRENT_PER_DOMAIN
    if args.workers is not None:
        MAX_WORKERS = args.workers
    if args.domain_concurrency is not None:
        MAX_CONCURRENT_PER_DOMAIN = args.domain_concurrency

    input_path = Path(args.input_file)
    if not input_path.exists():
        log.error("Input file not found: %s", input_path)
        sys.exit(1)
    if input_path.suffix.lower() != ".xlsx":
        log.error("Input file must be a .xlsx file, got: %s", input_path.suffix)
        sys.exit(1)

    log.info("=" * 60)
    log.info("Email Validation System - starting")
    log.info("Input file: %s", input_path)

    start = time.perf_counter()

    # Step 1 - read emails from Excel
    try:
        emails = read_emails(str(input_path))
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to read input file: %s", exc, exc_info=True)
        sys.exit(1)

    if not emails:
        log.warning("No email addresses found in the input file.")
        sys.exit(0)

    # Step 2 - validate all emails
    log.info(
        "Validating %d emails with %d workers (max %d concurrent per domain)…",
        len(emails), MAX_WORKERS, MAX_CONCURRENT_PER_DOMAIN,
    )
    results = validate_bulk(emails)

    elapsed = time.perf_counter() - start
    log.info("Validation finished in %.1f seconds", elapsed)

    # Step 3 - write outputs (always, even on partial/interrupted results)
    try:
        write_results(results)
        write_failed_emails(results)
        write_summary(results)
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to write output files: %s", exc, exc_info=True)
        sys.exit(1)

    log.info("All done. Check the 'output/' folder for results.")


if __name__ == "__main__":
    main()