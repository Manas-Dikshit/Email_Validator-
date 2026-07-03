"""
syntax_validator.py
-------------------
Stage 1 - Email Syntax Validation.

Responsibilities:
  - Normalise the raw email value (handle None/NaN/non-str, strip, lowercase)
  - Reject obviously malformed input cheaply (length limits, regex pre-filter)
  - Run a full RFC-compliant check via the email-validator library
  - Cache repeat lookups, since outreach spreadsheets often contain duplicates

Returns a dict describing the result so every stage shares the same
response shape:

    {
        "email": str,
        "status": STATUS_VALID | STATUS_INVALID_FORMAT,
        "reason": str,
        "smtp_response": "",
    }
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Any, Final

from email_validator import EmailNotValidError, validate_email

from config import STATUS_INVALID_FORMAT, STATUS_VALID
from logger import get_logger

log = get_logger(__name__)

# ── Constraints (RFC 5321 / 5322) ───────────────────────────────────────────
_MAX_LOCAL_LEN: Final[int] = 64
_MAX_DOMAIN_LEN: Final[int] = 255
_MAX_TOTAL_LEN: Final[int] = 254

# Simple pre-filter regex - catches obviously malformed addresses quickly,
# and also rules out a few RFC violations the naive version let through
# (consecutive dots, leading/trailing dot in the local part, domain labels
# that start/end with a hyphen or dot).
_BASIC_RE = re.compile(
    r"^(?!\.)(?!.*\.\.)[a-zA-Z0-9._%+\-]+(?<!\.)"
    r"@"
    r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?\.)+"
    r"[a-zA-Z]{2,63}$"
)

# email-validator cache size - large outreach lists commonly have duplicate
# or near-duplicate addresses (same domain, list re-runs, etc.)
_CACHE_SIZE: Final[int] = 20_000


def normalize_email(raw: Any) -> str:
    """
    Coerce arbitrary spreadsheet cell content into a clean, comparable
    email string.

    Handles the common junk values Excel/openpyxl/pandas hand back:
    None, NaN floats, numbers, surrounding whitespace, mixed case, and
    stray Unicode formatting (e.g. full-width characters, zero-width
    spaces) that would otherwise silently break the regex/RFC checks.

    Parameters
    ----------
    raw : Any
        The raw value from the spreadsheet cell. Expected to usually be
        a str, but treated defensively.

    Returns
    -------
    str
        Cleaned, lowercase email address. Empty string if the input was
        missing/blank/unusable.
    """
    if raw is None:
        return ""

    # Catch NaN floats (pandas reads empty Excel cells as float('nan'))
    if isinstance(raw, float) and raw != raw:  # NaN != NaN by definition
        return ""

    if not isinstance(raw, str):
        raw = str(raw)

    # Normalise Unicode (NFKC) so visually-identical characters compare
    # consistently, then strip zero-width/invisible characters that
    # sometimes leak in from copy-paste sources.
    cleaned = unicodedata.normalize("NFKC", raw)
    cleaned = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", cleaned)

    return cleaned.strip().lower()


def validate_syntax(raw_email: Any) -> dict:
    """
    Validate the syntax of a single email address.

    Steps
    -----
    1. Normalise (coerce type, strip, lowercase, strip invisible chars).
    2. Reject empty / oversized input cheaply.
    3. Quick regex check.
    4. Full RFC check via email-validator (cached).

    Parameters
    ----------
    raw_email : Any
        The raw email value from the Excel cell. Treated defensively -
        does not assume it is already a clean string.

    Returns
    -------
    dict with keys:
        email         - normalised email (or original on failure)
        status        - STATUS_VALID | STATUS_INVALID_FORMAT
        reason        - human-readable explanation
        smtp_response - empty string at this stage
    """
    email = normalize_email(raw_email)

    # ── Empty / missing ──────────────────────────────────────────────────
    if not email:
        reason = "Email value is missing or empty"
        log.debug("SYNTAX FAIL (empty) | raw=%r", raw_email)
        return _result("", STATUS_INVALID_FORMAT, reason)

    # ── Cheap length guard before any regex/RFC work ────────────────────
    if len(email) > _MAX_TOTAL_LEN:
        reason = f"Email exceeds max length of {_MAX_TOTAL_LEN} characters"
        log.debug("SYNTAX FAIL (too long) | %s", email)
        return _result(email, STATUS_INVALID_FORMAT, reason)

    if "@" not in email:
        reason = "Missing '@' symbol"
        log.debug("SYNTAX FAIL (no @) | %s", email)
        return _result(email, STATUS_INVALID_FORMAT, reason)

    local_part, _, domain_part = email.rpartition("@")
    if len(local_part) > _MAX_LOCAL_LEN:
        reason = f"Local part exceeds max length of {_MAX_LOCAL_LEN} characters"
        log.debug("SYNTAX FAIL (local too long) | %s", email)
        return _result(email, STATUS_INVALID_FORMAT, reason)

    if len(domain_part) > _MAX_DOMAIN_LEN:
        reason = f"Domain exceeds max length of {_MAX_DOMAIN_LEN} characters"
        log.debug("SYNTAX FAIL (domain too long) | %s", email)
        return _result(email, STATUS_INVALID_FORMAT, reason)

    # ── Regex pre-check ──────────────────────────────────────────────────
    if not _BASIC_RE.match(email):
        reason = f"Failed basic format check: '{email}'"
        log.debug("SYNTAX FAIL (regex) | %s | %s", email, reason)
        return _result(email, STATUS_INVALID_FORMAT, reason)

    # ── email-validator full check (cached) ─────────────────────────────
    ok, normalised_or_reason = _cached_rfc_check(email)
    if ok:
        log.debug("SYNTAX OK | %s", normalised_or_reason)
        return _result(normalised_or_reason, STATUS_VALID, "Syntax is valid")

    log.debug("SYNTAX FAIL (library) | %s | %s", email, normalised_or_reason)
    return _result(email, STATUS_INVALID_FORMAT, normalised_or_reason)


# ── Helpers ──────────────────────────────────────────────────────────────

@lru_cache(maxsize=_CACHE_SIZE)
def _cached_rfc_check(email: str) -> tuple[bool, str]:
    """
    Run the (relatively expensive) RFC-compliant check via email-validator,
    memoised so repeated addresses in a large spreadsheet only pay the
    cost once.

    Returns
    -------
    (True, normalised_email)  on success
    (False, reason)           on failure - covers both expected
                               EmailNotValidError cases and any
                               unexpected exception from the library, so
                               one malformed row can never crash the batch.
    """
    try:
        valid = validate_email(email, check_deliverability=False)
        return True, valid.normalized
    except EmailNotValidError as exc:
        return False, str(exc)
    except Exception as exc:  # defensive: never let one bad row kill the run
        log.warning("Unexpected error validating %s: %s", email, exc)
        return False, f"Unexpected validation error: {exc}"


def _result(email: str, status: str, reason: str) -> dict:
    """Build the standard result dict for this module."""
    return {
        "email": email,
        "status": status,
        "reason": reason,
        "smtp_response": "",
    }