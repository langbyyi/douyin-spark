"""Logging to the console, the current run folder and resident services."""

from __future__ import annotations

import contextlib
import copy
import logging
import os
import sys
from datetime import datetime, tzinfo
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

from dyspark import __version__
from dyspark.log_text import localize_message, mask_log_secrets

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_STDOUT = object()  # default console stream, resolved at call time
SERVICE_LOG_MAX_BYTES = 1_000_000
SERVICE_LOG_BACKUPS = 5


def brief_exc(exc: BaseException, limit: int = 200) -> str:
    """First line of an exception, capped for logs and job payloads."""
    text = str(exc).strip()
    line = text.splitlines()[0] if text else type(exc).__name__
    return line[:limit]


def harden_console() -> None:
    """Make console output unable to crash the program.

    * ``pythonw.exe`` (Task Scheduler) has ``sys.stdout``/``sys.stderr`` set to ``None``;
      ``print`` then silently does nothing and no console handler is installed.
    * A redirected stream on a Chinese Windows uses GBK, which cannot encode e.g. ``✔``
      or emoji; unencodable characters are replaced instead of raising.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(OSError, ValueError):
                reconfigure(errors="replace")


class ZonedFormatter(logging.Formatter):
    """Timestamps in the configured zone, so logs match the ledger whatever the host TZ."""

    def __init__(self, fmt: str, datefmt: str | None, zone: tzinfo | None) -> None:
        super().__init__(fmt, datefmt)
        self.zone = zone

    def format(self, record: logging.LogRecord) -> str:
        localized = copy.copy(record)
        localized.msg = localize_message(record.name, record.getMessage())
        localized.args = ()
        return mask_log_secrets(super().format(localized))

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        stamp = datetime.fromtimestamp(record.created, self.zone)
        if datefmt:
            return stamp.strftime(datefmt)
        return f"{stamp:%Y-%m-%d %H:%M:%S},{int(record.msecs):03d}"


def setup_logging(
    run_dir: Path | None,
    verbose: bool = False,
    zone: tzinfo | None = None,
    stream: TextIO | object | None = _STDOUT,
) -> None:
    """Console handler on ``stream`` (stdout by default, ``None`` = none) plus ``run.log``."""
    stream = sys.stdout if stream is _STDOUT else stream
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.DEBUG)
    if stream is not None:
        console = logging.StreamHandler(stream)
        console.setLevel(logging.DEBUG if verbose else logging.INFO)
        console.setFormatter(ZonedFormatter(FORMAT, "%H:%M:%S", zone))
        root.addHandler(console)
    if run_dir is not None:
        file_handler = logging.FileHandler(run_dir / "run.log", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(ZonedFormatter(FORMAT, None, zone))
        root.addHandler(file_handler)
    for noisy in ("asyncio", "httpcore", "httpx", "mcp", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _private_dir(path: Path) -> Path:
    """mkdir + restrict like :func:`dyspark.store.ensure_private_dir`, without that import."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        with contextlib.suppress(OSError):
            path.chmod(0o700)
    return path


def attach_service_log(data_dir: Path, name: str, zone: tzinfo | None = None) -> None:
    """Persistently log a resident service (``gui``, ``mcp``) to ``data/logs/<name>.log``.

    The stderr/console stream alone loses output when the service is started by
    Task Scheduler (``pythonw`` has no streams) or daemonized, so post-mortem
    debugging needs the file. Rotation is size-based (about 6 MB per service),
    so the folder needs no ``keep_days`` cleanup. The banner makes restarts
    greppable across rotations.
    """
    path = _private_dir(data_dir / "logs") / f"{name}.log"
    root = logging.getLogger()
    key = os.path.normcase(os.path.abspath(path))
    for existing in root.handlers:
        if (
            isinstance(existing, RotatingFileHandler)
            and os.path.normcase(os.path.abspath(existing.baseFilename)) == key
        ):
            return  # already attached (e.g. reload during tests); keep the stream
    handler = RotatingFileHandler(
        path, maxBytes=SERVICE_LOG_MAX_BYTES, backupCount=SERVICE_LOG_BACKUPS, encoding="utf-8"
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(ZonedFormatter(FORMAT, None, zone))
    root.addHandler(handler)
    logging.getLogger("dyspark").info("service log attached: %s (dyspark %s)", path, __version__)
