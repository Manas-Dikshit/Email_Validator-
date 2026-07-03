"""
logger.py
---------
Centralised logging setup for the Email Validation System.

Creates two handlers:
  - File handler    -> logs/email_validator_YYYY-MM-DD.log (rotates by size)
  - Console handler -> INFO+ messages on stdout

Usage:
    from logger import get_logger
    log = get_logger(__name__)
    log.info("Starting validation...")
"""

from __future__ import annotations

import logging
import sys
import threading
from datetime import date
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from config import LOG_DIR

# Guards logger creation so concurrent get_logger() calls from worker
# threads (this system uses MAX_WORKERS threads) can never race and attach
# duplicate handlers to the same logger.
_lock = threading.Lock()

# Cache the log file path per calendar day so all loggers created on the
# same day share one file, and a long-running process rolls over to a new
# file after midnight without needing a restart.
_current_log_date: Optional[date] = None
_current_log_file: Optional[Path] = None

# Max size per log file before rotating, and how many rotated backups to
# keep, so a large bulk run can't silently fill up the disk.
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_BACKUP_COUNT = 5

_FORMAT = "%(asctime)s | %(levelname)-8s | %(threadName)-10s | %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _get_log_file() -> Path:
    """
    Return today's log file path, creating LOG_DIR if needed. Recomputes
    the path once per calendar day so long-running processes roll over
    naturally at midnight.
    """
    global _current_log_date, _current_log_file

    today = date.today()
    if _current_log_file is not None and _current_log_date == today:
        return _current_log_file

    log_dir = Path(LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)

    _current_log_date = today
    _current_log_file = log_dir / f"email_validator_{today}.log"
    return _current_log_file


def get_logger(
    name: str,
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
) -> logging.Logger:
    """
    Return a configured Logger instance.

    Parameters
    ----------
    name : str
        Typically __name__ of the calling module.
    console_level : int
        Minimum level printed to stdout (default INFO).
    file_level : int
        Minimum level written to the log file (default DEBUG).

    Returns
    -------
    logging.Logger
        Logger with both file and console handlers attached exactly once,
        safe to call repeatedly (including concurrently from multiple
        threads) without producing duplicate log lines.
    """
    logger = logging.getLogger(name)

    # Fast path: already configured, no lock needed.
    if logger.handlers:
        return logger

    with _lock:
        # Re-check inside the lock: another thread may have configured
        # this exact logger while we were waiting for the lock.
        if logger.handlers:
            return logger

        logger.setLevel(logging.DEBUG)
        # Prevent messages from also being emitted by the root logger's
        # default handler (which would otherwise duplicate console output
        # if something else configures logging.basicConfig() elsewhere).
        logger.propagate = False

        fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

        try:
            log_file = _get_log_file()
            fh = RotatingFileHandler(
                log_file,
                maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            )
            fh.setLevel(file_level)
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError as exc:
            # If the log directory/file can't be created (permissions,
            # read-only filesystem, disk full, etc.) don't let logging
            # setup itself crash the whole pipeline - fall back to
            # console-only logging and report the problem loudly.
            sys.stderr.write(f"WARNING: could not open log file for '{name}': {exc}\n")

        ch = logging.StreamHandler(stream=sys.stdout)
        ch.setLevel(console_level)
        ch.setFormatter(fmt)
        logger.addHandler(ch)

    return logger