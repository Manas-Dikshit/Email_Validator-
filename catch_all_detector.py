"""
catch_all_detector.py
---------------------
Stage 4 - Catch-All Domain Detection.

A "catch-all" domain accepts mail for ANY local part, including completely
random ones. If a domain accepts our deliberately nonsensical probe address
(e.g. zz_catchall_probe_a1b2c3@domain.com) with a 250 response, we know the
domain is catch-all and we cannot trust a 250 for the real address either.

Detection strategy:
  1. Build a probe email using a random-looking local part that can't
     plausibly exist (derived from config.CATCH_ALL_PROBE plus a random
     suffix, to avoid collisions with cached/blacklisted probe addresses).
  2. Run the same SMTP check used in smtp_validator.
  3. If the probe returns VALID (250)      -> domain is CATCH_ALL.
  4. If the probe is cleanly rejected      -> domain is NOT catch-all, so a
     previous VALID result for the real address is trustworthy.
  5. If the probe result is ambiguous (timeout, greylist, temp-fail, no MX
     hosts, etc.) -> we cannot safely conclude either way, so we report
     "unknown" rather than silently guessing.
"""

from __future__ import annotations

import random
import string
import threading
from typing import Dict, List, Optional

from config import CATCH_ALL_PROBE, STATUS_CATCH_ALL
from logger import get_logger
from smtp_validator import verify_mailbox

log = get_logger(__name__)

# Statuses returned by verify_mailbox that we treat as a *definitive* rejection
# (i.e. safe to conclude "not catch-all"). Anything else (timeouts, temp
# failures, greylisting, unknown errors) is treated as ambiguous.
_DEFINITIVE_REJECT_STATUSES = frozenset({"INVALID", "REJECTED", "NOT_FOUND"})

# Simple in-process cache so we don't re-probe the same domain repeatedly
# within a single run (e.g. when validating many addresses at the same
# domain). Thread-safe via a lock since validators may run concurrently.
_cache_lock = threading.Lock()
_catch_all_cache: Dict[str, Optional[bool]] = {}


def _random_probe_local_part() -> str:
    """
    Build a randomized, implausible local part for the probe address.

    Using a random suffix (rather than a fixed CATCH_ALL_PROBE string every
    time) prevents a mail server from recognizing and specifically
    blacklisting/caching our fixed probe address, which could otherwise
    skew results across repeated runs.
    """
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"{CATCH_ALL_PROBE}{suffix}"


def is_catch_all(
    domain: str,
    mx_hosts: List[str],
    use_cache: bool = True,
) -> Optional[bool]:
    """
    Probe the domain with a nonsense address to detect catch-all behaviour.

    Parameters
    ----------
    domain : str
        The domain to probe, e.g. "example.com".
    mx_hosts : list[str]
        Ordered MX hosts for the domain (from dns_checker).
    use_cache : bool
        If True (default), reuse a previously computed result for this
        domain within the current process instead of probing again.

    Returns
    -------
    Optional[bool]
        True  -> domain accepts all addresses (catch-all).
        False -> domain rejects unknown addresses (not catch-all).
        None  -> could not determine (no MX hosts, ambiguous/erroring
                 probe result). Callers should treat this as "unknown"
                 and avoid overriding an existing VALID result.
    """
    if not domain:
        log.warning("is_catch_all called with empty domain")
        return None

    domain = domain.strip().lower()

    if not mx_hosts:
        log.debug("Cannot probe for catch-all | %s | no MX hosts", domain)
        return None

    if use_cache:
        with _cache_lock:
            if domain in _catch_all_cache:
                cached = _catch_all_cache[domain]
                log.debug("CATCH-ALL cache hit | %s | %s", domain, cached)
                return cached

    probe_email = f"{_random_probe_local_part()}@{domain}"

    try:
        result = verify_mailbox(probe_email, mx_hosts)
    except Exception as exc:  # noqa: BLE001 - defensive: never let a probe crash the pipeline
        log.warning("Catch-all probe raised an exception | %s | %s", domain, exc)
        return None

    status = result.get("status") if isinstance(result, dict) else None

    if status == "VALID":
        log.debug("CATCH-ALL detected | %s | probe accepted", domain)
        outcome: Optional[bool] = True
    elif status in _DEFINITIVE_REJECT_STATUSES:
        log.debug("NOT catch-all | %s | probe rejected (%s)", domain, status)
        outcome = False
    else:
        # Ambiguous outcome (timeout, temp-fail, greylist, unknown status, etc.)
        log.debug(
            "Catch-all status UNKNOWN | %s | ambiguous probe result (%s)",
            domain,
            status,
        )
        outcome = None

    if use_cache and outcome is not None:
        with _cache_lock:
            _catch_all_cache[domain] = outcome

    return outcome


def mark_catch_all(existing_result: dict) -> dict:
    """
    Override a VALID result's status to CATCH_ALL.

    Parameters
    ----------
    existing_result : dict
        A result dict (from smtp_validator or dns_checker) that was
        previously VALID.

    Returns
    -------
    dict
        A new dict (the original is left untouched) with status and
        reason updated to reflect catch-all detection.
    """
    if not isinstance(existing_result, dict):
        raise TypeError(f"existing_result must be a dict, got {type(existing_result).__name__}")

    updated = existing_result.copy()
    updated["status"] = STATUS_CATCH_ALL
    updated["reason"] = "Domain accepts all addresses (catch-all)"
    return updated


def clear_cache() -> None:
    """Clear the in-process catch-all detection cache (mainly useful for tests)."""
    with _cache_lock:
        _catch_all_cache.clear()