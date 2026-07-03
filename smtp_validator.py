"""
smtp_validator.py
-----------------
Stage 3 - SMTP Mailbox Verification.

Performs an SMTP handshake up to the RCPT TO command to verify whether
a mailbox exists. NO email is ever sent.

Sequence:
  1. Connect to the MX host on SMTP_PORT
  2. Send EHLO / HELO (using SMTP_HELO_DOMAIN as our identity)
  3. Send MAIL FROM: <verify@yourdomain.com>
  4. Send RCPT TO: <target@domain.com>
  5. Parse the 3-digit SMTP response code
  6. Immediately QUIT

Common response meanings:
  250, 251          -> Mailbox exists (VALID)
  550, 551, 553, 554 -> Mailbox does not exist (INVALID_MAILBOX)
  421, 450, 451, 452 -> Temporary failure
  503, 530, 535      -> Access denied / auth required
  anything else      -> UNKNOWN
"""

from __future__ import annotations

import smtplib
import socket
import time
from typing import List, Tuple

from config import (
    SMTP_HELO_DOMAIN,
    SMTP_PORT,
    SMTP_RETRIES,
    SMTP_RETRY_BACKOFF,
    SMTP_RETRY_DELAY,
    SMTP_SENDER,
    SMTP_TIMEOUT,
    STATUS_ACCESS_DENIED,
    STATUS_INVALID_MAILBOX,
    STATUS_TEMPORARY_FAILURE,
    STATUS_UNKNOWN,
    STATUS_VALID,
)
from logger import get_logger

log = get_logger(__name__)

# SMTP response-code -> status mapping
_CODE_MAP = {
    # Success
    250: STATUS_VALID,
    251: STATUS_VALID,
    # Hard failures (mailbox doesn't exist)
    550: STATUS_INVALID_MAILBOX,
    551: STATUS_INVALID_MAILBOX,
    553: STATUS_INVALID_MAILBOX,
    554: STATUS_INVALID_MAILBOX,
    # Access / policy rejections
    503: STATUS_ACCESS_DENIED,
    530: STATUS_ACCESS_DENIED,
    535: STATUS_ACCESS_DENIED,
    # Temporary failures
    421: STATUS_TEMPORARY_FAILURE,
    450: STATUS_TEMPORARY_FAILURE,
    451: STATUS_TEMPORARY_FAILURE,
    452: STATUS_TEMPORARY_FAILURE,
}

_REASON_MAP = {
    STATUS_VALID:             "Mailbox exists",
    STATUS_INVALID_MAILBOX:   "Mailbox does not exist",
    STATUS_ACCESS_DENIED:     "Access denied by server",
    STATUS_TEMPORARY_FAILURE: "Temporary server failure",
    STATUS_UNKNOWN:           "Unknown SMTP response",
}

# Exceptions that indicate a genuinely transient connection problem worth
# retrying against the *same* host (e.g. the server is momentarily busy or
# refusing new connections due to rate limiting).
_RETRYABLE_CONNECT_EXCEPTIONS = (smtplib.SMTPConnectError,)

# Exceptions that mean "give up on this host, move to the next MX" without
# retrying, since retrying the same host is unlikely to help.
_NEXT_HOST_EXCEPTIONS = (
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPHeloError,
    smtplib.SMTPSenderRefused,
    smtplib.SMTPNotSupportedError,
    socket.timeout,
    TimeoutError,
    OSError,
)


def verify_mailbox(email: str, mx_hosts: List[str]) -> dict:
    """
    Attempt SMTP verification against each MX host in priority order.

    Retries up to SMTP_RETRIES times per host on connection errors before
    moving to the next host.

    Parameters
    ----------
    email : str
        Normalised email address to verify.
    mx_hosts : list[str]
        Ordered list of MX hostnames (highest priority first).

    Returns
    -------
    dict with keys:
        email         - passed through unchanged
        status        - one of the STATUS_* constants
        reason        - human-readable explanation
        smtp_response - raw SMTP code + message, e.g. "250 OK"
    """
    if not email or "@" not in email:
        return _result(email, STATUS_UNKNOWN, "Malformed email address", "N/A")

    mx_hosts = [h for h in (mx_hosts or []) if h and h.strip()]
    if not mx_hosts:
        return _result(email, STATUS_TEMPORARY_FAILURE,
                        "No MX hosts provided", "N/A")

    last_error = "No connection attempt made"

    for mx in mx_hosts:
        max_attempts = SMTP_RETRIES + 1
        for attempt in range(1, max_attempts + 1):
            try:
                code, message = _smtp_check(email, mx)
                status = _CODE_MAP.get(code, STATUS_UNKNOWN)
                reason = _REASON_MAP.get(status, f"SMTP code {code}: {message}")
                smtp_resp = f"{code} {message}"
                log.debug("SMTP %s | %s | MX=%s | %s", status, email, mx, smtp_resp)
                return _result(email, status, reason, smtp_resp)

            except _RETRYABLE_CONNECT_EXCEPTIONS as exc:
                last_error = f"Connection error to {mx}: {exc}"
                log.debug("SMTP connect error (attempt %d/%d) | %s | %s",
                           attempt, max_attempts, email, last_error)
                if attempt < max_attempts:
                    _sleep_before_retry(attempt)
                # else: exhausted retries on this host, fall through to next MX

            except _NEXT_HOST_EXCEPTIONS as exc:
                last_error = f"{type(exc).__name__} on {mx}: {exc}"
                log.debug("SMTP unrecoverable on host | %s | %s", email, last_error)
                break  # No point retrying this host - move to next MX

            except smtplib.SMTPException as exc:
                # Catch-all for any other smtplib-specific failure we didn't
                # anticipate (protocol errors, unexpected replies, etc.).
                last_error = f"SMTP protocol error on {mx}: {exc}"
                log.debug("SMTP protocol error | %s | %s", email, last_error)
                break

            except Exception as exc:  # noqa: BLE001 - never let one bad probe crash a worker thread
                last_error = f"Unexpected error on {mx}: {exc}"
                log.warning("Unexpected SMTP verification error | %s | %s", email, last_error)
                break

    log.debug("SMTP exhausted all hosts | %s | %s", email, last_error)
    return _result(email, STATUS_TEMPORARY_FAILURE, last_error, "N/A")


# ── Private helpers ───────────────────────────────────────────────────────────

def _sleep_before_retry(attempt: int) -> None:
    """
    Pause before retrying the same host, to avoid hammering a server that's
    momentarily rejecting connections. Uses linear backoff (delay * attempt)
    when SMTP_RETRY_BACKOFF is enabled, otherwise a flat delay.
    """
    delay = SMTP_RETRY_DELAY * attempt if SMTP_RETRY_BACKOFF else SMTP_RETRY_DELAY
    time.sleep(delay)


def _smtp_check(email: str, mx_host: str) -> Tuple[int, str]:
    """
    Open a real SMTP connection, perform the handshake up to RCPT TO,
    then immediately QUIT. Nothing is ever sent.

    Parameters
    ----------
    email : str
        Target email address for RCPT TO.
    mx_host : str
        SMTP server hostname.

    Returns
    -------
    tuple[int, str]
        (SMTP response code, response message) - either from MAIL FROM
        (if the sender itself was rejected) or from RCPT TO.

    Raises
    ------
    smtplib.SMTPException subclasses, socket.timeout, OSError
    """
    with smtplib.SMTP(timeout=SMTP_TIMEOUT, local_hostname=SMTP_HELO_DOMAIN) as smtp:
        smtp.connect(mx_host, SMTP_PORT)
        smtp.ehlo_or_helo_if_needed()

        mail_code, mail_message = smtp.mail(SMTP_SENDER)
        if mail_code >= 400:
            # The server rejected our sender address outright - there is no
            # meaningful RCPT TO result to get from this host. Surface the
            # MAIL FROM response as-is rather than proceeding.
            _safe_quit(smtp)
            return _decode_response(mail_code, mail_message)

        code, message = smtp.rcpt(email)
        _safe_quit(smtp)

    return _decode_response(code, message)


def _safe_quit(smtp: smtplib.SMTP) -> None:
    """
    Send QUIT, swallowing any error - the server may have already closed
    the connection, and a failed QUIT shouldn't overwrite a result we
    already successfully obtained.
    """
    try:
        smtp.quit()
    except (smtplib.SMTPException, OSError):
        pass


def _decode_response(code: int, message) -> Tuple[int, str]:
    """Normalise an SMTP response message to a decoded str."""
    if isinstance(message, bytes):
        message = message.decode(errors="replace")
    return code, str(message)


def _result(email: str, status: str, reason: str, smtp_response: str) -> dict:
    """Build the standard result dict for this module."""
    return {
        "email": email,
        "status": status,
        "reason": reason,
        "smtp_response": smtp_response,
    }