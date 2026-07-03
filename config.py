"""
config.py
---------
Central configuration for the Email Validation System.
All tunable parameters are defined here to keep other modules clean.

This module performs light validation at import time so misconfiguration
(e.g. negative timeouts, zero workers) fails fast and loudly instead of
causing confusing behaviour deep inside the validation pipeline.
"""

from __future__ import annotations

from pathlib import Path

# ── SMTP Settings ─────────────────────────────────────────────────────────────

# Sender address used during SMTP handshake (never actually sends mail)
SMTP_SENDER = "verify@yourdomain.com"

# Domain presented in the SMTP HELO/EHLO greeting. Some servers reject
# handshakes where this doesn't resemble a real domain, so keep it aligned
# with SMTP_SENDER's domain by default.
SMTP_HELO_DOMAIN = SMTP_SENDER.split("@", 1)[-1]

# Standard SMTP port. 587/465 are for authenticated submission, not
# unauthenticated verification, so 25 is almost always correct here.
SMTP_PORT = 25

# Seconds to wait for an SMTP connection / each socket operation before
# timing out.
SMTP_TIMEOUT: float = 10

# How many times to retry a failed SMTP connection (transient failures only;
# definitive rejections such as 550 are never retried).
SMTP_RETRIES = 2

# Seconds to wait between retries (avoids hammering the server).
SMTP_RETRY_DELAY: float = 2

# If True, each retry waits SMTP_RETRY_DELAY * (attempt_number) instead of a
# flat delay, which is gentler on servers that are temporarily rate-limiting.
SMTP_RETRY_BACKOFF = True

# ── DNS Settings ──────────────────────────────────────────────────────────────

# Seconds to wait for a DNS response.
DNS_TIMEOUT: float = 5

# How many times to retry a failed/timed-out DNS lookup.
DNS_RETRIES = 1

# ── Threading / Performance ───────────────────────────────────────────────────

# Number of parallel worker threads for bulk validation.
MAX_WORKERS = 10

# ── Catch-All Detection ───────────────────────────────────────────────────────

# Fixed prefix used to probe for catch-all behaviour. A random suffix is
# appended at call time (see catch_all_detector.py) so the exact probe
# address differs on every check, preventing servers from memoizing or
# blacklisting one specific probe address.
CATCH_ALL_PROBE = "zz_catchall_probe_"

# Length of the random alphanumeric suffix appended to CATCH_ALL_PROBE.
CATCH_ALL_PROBE_SUFFIX_LENGTH = 8

# ── File Paths ────────────────────────────────────────────────────────────────
# Defined with pathlib so path handling is correct on Windows/macOS/Linux
# alike. Directories are created automatically if missing.

BASE_DIR = Path(__file__).resolve().parent

# Folder where log files are written.
LOG_DIR = BASE_DIR / "logs"

# Folder where output Excel / reports are written.
OUTPUT_DIR = BASE_DIR / "output"

# Name of the validated results workbook.
OUTPUT_EXCEL = OUTPUT_DIR / "validation_results.xlsx"

# Name of the workbook containing only failed emails.
FAILED_EXCEL = OUTPUT_DIR / "failed_emails.xlsx"

# Plain-text summary report.
SUMMARY_REPORT = OUTPUT_DIR / "summary_report.txt"

# Ensure output/log directories exist so downstream modules never have to
# remember to do this themselves.
for _dir in (LOG_DIR, OUTPUT_DIR):
    _dir.mkdir(parents=True, exist_ok=True)

# ── Validation Status Labels ──────────────────────────────────────────────────
# These are the canonical strings written to the "Mail Status" column.
# Other modules should always import these constants rather than hardcoding
# the raw strings, so a typo anywhere is caught at import/lint time instead
# of silently producing a status that never matches.

STATUS_VALID             = "VALID"
STATUS_INVALID_FORMAT    = "INVALID_FORMAT"
STATUS_INVALID_DOMAIN    = "INVALID_DOMAIN"
STATUS_INVALID_MAILBOX   = "INVALID_MAILBOX"
STATUS_ACCESS_DENIED     = "ACCESS_DENIED"
STATUS_CATCH_ALL         = "CATCH_ALL"
STATUS_TEMPORARY_FAILURE = "TEMPORARY_FAILURE"
STATUS_UNKNOWN           = "UNKNOWN"

# Complete set of valid statuses, useful for validation/tests, e.g.:
#   assert result["status"] in config.ALL_STATUSES
ALL_STATUSES = frozenset({
    STATUS_VALID,
    STATUS_INVALID_FORMAT,
    STATUS_INVALID_DOMAIN,
    STATUS_INVALID_MAILBOX,
    STATUS_ACCESS_DENIED,
    STATUS_CATCH_ALL,
    STATUS_TEMPORARY_FAILURE,
    STATUS_UNKNOWN,
})

# Statuses that represent a *definitive* rejection of a mailbox (as opposed
# to a transient/ambiguous outcome like STATUS_TEMPORARY_FAILURE or
# STATUS_UNKNOWN). catch_all_detector.py uses this to decide whether a probe
# result safely proves a domain is NOT catch-all.
DEFINITIVE_REJECT_STATUSES = frozenset({
    STATUS_INVALID_MAILBOX,
    STATUS_INVALID_DOMAIN,
})

# ── Sanity checks (fail fast on bad configuration) ────────────────────────────

if SMTP_TIMEOUT <= 0:
    raise ValueError("SMTP_TIMEOUT must be positive")
if DNS_TIMEOUT <= 0:
    raise ValueError("DNS_TIMEOUT must be positive")
if SMTP_RETRIES < 0:
    raise ValueError("SMTP_RETRIES cannot be negative")
if SMTP_RETRY_DELAY < 0:
    raise ValueError("SMTP_RETRY_DELAY cannot be negative")
if MAX_WORKERS < 1:
    raise ValueError("MAX_WORKERS must be at least 1")
if not (0 < SMTP_PORT < 65536):
    raise ValueError("SMTP_PORT must be a valid TCP port (1-65535)")
if CATCH_ALL_PROBE_SUFFIX_LENGTH < 4:
    raise ValueError("CATCH_ALL_PROBE_SUFFIX_LENGTH should be at least 4 to avoid collisions")
if "@" not in SMTP_SENDER:
    raise ValueError(f"SMTP_SENDER must be a valid email address, got: {SMTP_SENDER!r}")