"""
dns_checker.py
--------------
Stage 2 - Domain / MX Record Validation.

Responsibilities:
  * Extract the domain part from the email address
  * Look up MX records to confirm the domain accepts mail
  * Fall back to A/AAAA records per RFC 5321 implicit-MX rules when no
    MX record exists
  * Return the list of mail-exchanger hostnames (used later by the SMTP
    validator)

Uses dnspython for all DNS queries.

IMPORTANT distinction this module makes carefully:
  - "This domain does not exist / has no mail records"  -> STATUS_INVALID_DOMAIN
    (NXDOMAIN / NoAnswer - the DNS system gave us a definitive answer)
  - "We could not get an answer at all"                  -> STATUS_DNS_ERROR
    (NoNameservers / repeated timeouts - a resolver/network problem, not
    proof the domain is bad. These should be reviewed/retried, not treated
    as dead leads.)
Conflating the two used to cause every domain to be marked invalid
whenever the local resolver was unreachable (e.g. VPN/firewall blocking
raw UDP:53 queries) - it looked like "all emails are invalid" when it was
actually "DNS queries aren't getting answered at all".
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional, Tuple, Union

import dns.exception
import dns.resolver

from config import DNS_TIMEOUT, DNS_RETRIES, STATUS_INVALID_DOMAIN, STATUS_VALID
from logger import get_logger

log = get_logger(__name__)

# config.py may not define this yet - fall back to a sane default so this
# module still works without requiring an immediate config.py edit.
try:
    from config import STATUS_DNS_ERROR
except ImportError:
    STATUS_DNS_ERROR = "DNS_ERROR"

# ── Resolvers ────────────────────────────────────────────────────────────
# Primary: whatever the OS/network has configured (VPN DNS, ISP DNS, etc.)
_primary_resolver = dns.resolver.Resolver()
_primary_resolver.lifetime = DNS_TIMEOUT
_primary_resolver.timeout = DNS_TIMEOUT

# Fallback: known-good public resolvers. Used only when the primary
# resolver fails to produce an answer at all (NoNameservers / timeout),
# so a broken or blocked local resolver doesn't get misread as "every
# domain is invalid".
_fallback_resolver = dns.resolver.Resolver(configure=False)
_fallback_resolver.nameservers = ["8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1"]
_fallback_resolver.lifetime = DNS_TIMEOUT
_fallback_resolver.timeout = DNS_TIMEOUT

# Definitive "this record does not exist" - safe to trust from either resolver.
_NO_RECORD_EXCEPTIONS = (
    dns.resolver.NXDOMAIN,
    dns.resolver.NoAnswer,
)

# "We couldn't get a real answer" - a resolver/connectivity problem, NOT
# proof the domain is invalid. Worth retrying / falling back.
_RESOLUTION_FAILURE_EXCEPTIONS = (
    dns.resolver.NoNameservers,
    dns.exception.Timeout,
)

# Sentinel distinguishing "confirmed no record" (None) from
# "could not determine" (this) - callers must not treat them the same.
_UNRESOLVED = object()

# In-process cache: domain -> check_domain() result dict. Bulk validation
# runs frequently hit the same domain (e.g. many @gmail.com addresses), so
# caching avoids redundant DNS round-trips. Thread-safe for MAX_WORKERS.
# NOTE: DNS_ERROR results are intentionally NOT cached - a transient
# resolver failure for one email shouldn't poison every other email on
# the same domain within this run.
_cache_lock = threading.Lock()
_domain_cache: Dict[str, dict] = {}


def get_domain(email: str) -> str:
    """
    Extract the domain portion from a normalised email address.

    Parameters
    ----------
    email : str
        A normalised email, e.g. "user@example.com".

    Returns
    -------
    str
        The lowercased domain part, e.g. "example.com".

    Raises
    ------
    ValueError
        If the email has no '@' or no domain portion.
    """
    if "@" not in email:
        raise ValueError(f"Not a valid email address (missing '@'): {email!r}")
    domain = email.split("@", 1)[1].strip().rstrip(".").lower()
    if not domain:
        raise ValueError(f"Not a valid email address (empty domain): {email!r}")
    return domain


def check_domain(email: str, use_cache: bool = True) -> dict:
    """
    Verify that the email's domain accepts mail (has MX records, or at
    least an A/AAAA record to fall back on per RFC 5321).

    Parameters
    ----------
    email : str
        Normalised email address.
    use_cache : bool
        If True (default), reuse a previously computed result for this
        domain within the current process instead of re-querying DNS.

    Returns
    -------
    dict with keys:
        email         - passed through unchanged
        status        - STATUS_VALID | STATUS_INVALID_DOMAIN | STATUS_DNS_ERROR
        reason        - human-readable explanation
        smtp_response - empty at this stage
        mx_hosts      - list of mail-exchanger hostnames sorted by
                        priority (empty on fail)
    """
    try:
        domain = get_domain(email)
    except ValueError as exc:
        log.debug("DNS FAIL (bad email) | %s | %s", email, exc)
        return _result(email, STATUS_INVALID_DOMAIN, str(exc), [])

    try:
        ascii_domain = _to_ascii(domain)
    except UnicodeError:
        reason = f"Domain '{domain}' is not a valid (IDNA-encodable) hostname"
        log.debug("DNS FAIL (bad IDNA) | %s | %s", email, reason)
        return _result(email, STATUS_INVALID_DOMAIN, reason, [])

    if use_cache:
        with _cache_lock:
            cached = _domain_cache.get(ascii_domain)
        if cached is not None:
            log.debug("DNS cache hit | %s | %s", email, cached["status"])
            return _result(email, cached["status"], cached["reason"], cached["mx_hosts"])

    # ── Step 1: Prefer MX records - this is how mail is actually routed. ──────
    mx_hosts = _get_mx_hosts(ascii_domain)
    if mx_hosts is _UNRESOLVED:
        # Couldn't get an answer for MX at all. Try A/AAAA before giving up -
        # if those resolve fine, the resolver is working and MX genuinely
        # has no records; if those ALSO fail to resolve, it's a resolver
        # problem, not an invalid domain.
        exists = _domain_exists(ascii_domain)
        if exists is _UNRESOLVED:
            reason = (
                f"Could not verify domain '{domain}': DNS queries failed to get "
                f"any response (network/resolver issue, not a confirmed invalid "
                f"domain). Re-run this address later."
            )
            log.warning("DNS ERROR | %s | %s", email, reason)
            # Not cached - see note above _domain_cache.
            return _result(email, STATUS_DNS_ERROR, reason, [])
        if exists:
            reason = "No MX records; using domain's A/AAAA record as implicit MX"
            result = _result(email, STATUS_VALID, reason, [domain])
            log.debug("DNS OK (implicit MX) | %s | %s", email, reason)
            _store_cache(ascii_domain, result)
            return result
        reason = f"Domain '{domain}' does not exist or accepts no mail (no MX, no A/AAAA)"
        log.debug("DNS FAIL | %s | %s", email, reason)
        result = _result(email, STATUS_INVALID_DOMAIN, reason, [])
        _store_cache(ascii_domain, result)
        return result

    if mx_hosts:
        result = _result(email, STATUS_VALID, "Domain has valid MX records", mx_hosts)
        log.debug("DNS OK | %s | MX: %s", email, mx_hosts)
        _store_cache(ascii_domain, result)
        return result

    # ── Step 2: MX confirmed absent -> fall back to A/AAAA (RFC 5321 implicit MX). ──
    exists = _domain_exists(ascii_domain)
    if exists is _UNRESOLVED:
        reason = (
            f"Could not verify domain '{domain}': DNS queries failed to get "
            f"any response (network/resolver issue, not a confirmed invalid "
            f"domain). Re-run this address later."
        )
        log.warning("DNS ERROR | %s | %s", email, reason)
        return _result(email, STATUS_DNS_ERROR, reason, [])

    if exists:
        reason = "No MX records; using domain's A/AAAA record as implicit MX"
        result = _result(email, STATUS_VALID, reason, [domain])
        log.debug("DNS OK (implicit MX) | %s | %s", email, reason)
        _store_cache(ascii_domain, result)
        return result

    reason = f"Domain '{domain}' does not exist or accepts no mail (no MX, no A/AAAA)"
    log.debug("DNS FAIL | %s | %s", email, reason)
    result = _result(email, STATUS_INVALID_DOMAIN, reason, [])
    _store_cache(ascii_domain, result)
    return result


# ── Private helpers ───────────────────────────────────────────────────────────

def _to_ascii(domain: str) -> str:
    """
    Convert an internationalized domain name to its ASCII/punycode form.
    Domains that are already plain ASCII pass through unchanged.
    """
    try:
        return domain.encode("idna").decode("ascii")
    except UnicodeError:
        try:
            domain.encode("ascii")
            return domain
        except UnicodeError:
            raise


def _resolve_with_retry(domain: str, record_type: str):
    """
    Run a DNS query against the primary resolver, retrying on transient
    failures up to DNS_RETRIES times. If the primary resolver never
    manages to get an answer (NoNameservers / repeated timeouts), fall
    back to public resolvers before giving up entirely.

    Returns
    -------
    - the answer set, if records were found
    - None, if the DNS system definitively said "no such record"
      (NXDOMAIN / NoAnswer)
    - _UNRESOLVED, if no resolver could produce any answer at all -
      this must NOT be treated as "record doesn't exist" by callers.
    """
    for resolver, label in ((_primary_resolver, "primary"), (_fallback_resolver, "fallback")):
        attempts = DNS_RETRIES + 1
        for attempt in range(1, attempts + 1):
            try:
                return resolver.resolve(domain, record_type)
            except _NO_RECORD_EXCEPTIONS:
                return None
            except _RESOLUTION_FAILURE_EXCEPTIONS as exc:
                log.debug(
                    "DNS resolution failure | %s | %s | %s resolver | attempt %d/%d | %s",
                    domain, record_type, label, attempt, attempts, exc,
                )
                continue
            except Exception as exc:  # noqa: BLE001 - defensive catch-all
                log.warning("Unexpected DNS error | %s | %s | %s", domain, record_type, exc)
                return _UNRESOLVED
        log.debug(
            "%s resolver exhausted retries for %s | %s - trying next resolver",
            label, domain, record_type,
        )

    log.warning(
        "DNS lookup completely unresolved (all resolvers failed) | %s | %s",
        domain, record_type,
    )
    return _UNRESOLVED


def _domain_exists(domain: str) -> Union[bool, object]:
    """
    Check whether the domain has at least one A or AAAA record.

    Returns
    -------
    True / False if definitively determined, or _UNRESOLVED if no
    resolver could get an answer for either record type.
    """
    saw_unresolved = False
    for record_type in ("A", "AAAA"):
        answer = _resolve_with_retry(domain, record_type)
        if answer is _UNRESOLVED:
            saw_unresolved = True
            continue
        if answer is not None:
            return True
    return _UNRESOLVED if saw_unresolved else False


def _get_mx_hosts(domain: str) -> Union[List[str], object]:
    """
    Return MX hostnames sorted by preference (lowest = highest priority),
    with ties broken alphabetically for deterministic ordering.

    Returns
    -------
    list[str]   - possibly empty, if MX lookup was definitive
    _UNRESOLVED - if no resolver could get any answer for MX
    """
    answers = _resolve_with_retry(domain, "MX")
    if answers is _UNRESOLVED:
        return _UNRESOLVED
    if not answers:
        return []

    def _sort_key(record) -> Tuple[int, str]:
        return (record.preference, str(record.exchange).rstrip("."))

    sorted_mx = sorted(answers, key=_sort_key)
    hosts = [str(r.exchange).rstrip(".") for r in sorted_mx]

    # Some misconfigured domains publish a null MX ("." with preference 0)
    # to explicitly signal "this domain accepts no mail" (RFC 7505).
    hosts = [h for h in hosts if h and h != ""]
    return hosts


def _store_cache(domain: str, result: dict) -> None:
    with _cache_lock:
        _domain_cache[domain] = result


def _result(email: str, status: str, reason: str, mx_hosts: list) -> dict:
    """Build the standard result dict for this module."""
    return {
        "email": email,
        "status": status,
        "reason": reason,
        "smtp_response": "",
        "mx_hosts": mx_hosts,
    }


def clear_cache() -> None:
    """Clear the in-process DNS result cache (mainly useful for tests)."""
    with _cache_lock:
        _domain_cache.clear()