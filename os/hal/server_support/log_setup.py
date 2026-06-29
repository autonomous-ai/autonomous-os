"""HAL logging configuration — colored stdout + rotating file + optional GELF.

Extracted verbatim from server.py so the boot module stays focused on the
hardware/route wiring. `setup_logging()` carries the exact same side effects
(root-logger handlers) and must be called once, early, before any driver import
emits a warning. Returns the `hal.server` logger the boot module logs through.
"""

import logging
import logging.handlers
import os
from pathlib import Path

_LEVEL_COLORS = {
    logging.DEBUG: "\033[37m",  # gray
    logging.INFO: "\033[32m",  # green
    logging.WARNING: "\033[33m",  # yellow
    logging.ERROR: "\033[31m",  # red
    logging.CRITICAL: "\033[1;31m",  # bold red
}
_RESET = "\033[0m"


class _ColorFormatter(logging.Formatter):
    """Adds ANSI colors to levelname for console output."""

    _fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"

    def format(self, record):
        color = _LEVEL_COLORS.get(record.levelno, "")
        record.levelname = f"{color}{record.levelname}{_RESET}"
        formatter = logging.Formatter(self._fmt)
        return formatter.format(record)


def setup_logging() -> logging.Logger:
    """Configure root logging (console + rotating file + GELF) and return the
    `hal.server` logger. Idempotent enough for a single boot call."""
    log_dir = Path(os.environ.get("HAL_LOG_DIR", "/var/log/hal"))
    log_dir.mkdir(parents=True, exist_ok=True)

    _root = logging.getLogger()
    _log_level = os.environ.get("HAL_LOG_LEVEL", "INFO").upper()
    _root.setLevel(getattr(logging, _log_level, logging.INFO))

    # Console handler (colored)
    _console = logging.StreamHandler()
    _console.setFormatter(_ColorFormatter())
    _root.addHandler(_console)

    # File handler: 1 MB per file, keep 3 backups (~4 MB max) -- no color codes
    _file = logging.handlers.RotatingFileHandler(
        log_dir / "server.log",
        maxBytes=20 * 1024 * 1024,
        backupCount=3,
    )
    _file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _root.addHandler(_file)

    # Dedicated realtime token/cost log — its own file, kept OUT of server.log
    # and the console (propagate=False) so per-turn "[realtime] Gemini usage"
    # lines don't drown the main log and can be tailed/parsed on their own.
    _usage = logging.getLogger("hal.realtime.usage")
    _usage.setLevel(logging.DEBUG)
    _usage_file = logging.handlers.RotatingFileHandler(
        log_dir / "gemini_usage.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
    )
    _usage_file.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _usage.addHandler(_usage_file)
    _usage.propagate = False

    # GELF handler: send INFO+ logs to centralized Graylog
    try:
        from hal.drivers.gelf_handler import GELFHandler
        from hal.config import _os_cfg_get

        _gelf = GELFHandler()
        _gelf.setFormatter(logging.Formatter("%(message)s"))
        _device_id = _os_cfg_get("device_id")
        if _device_id:
            _gelf.set_host(_device_id)
        _root.addHandler(_gelf)
    except Exception:
        pass

    logger = logging.getLogger("hal.server")
    logger.info("Logging to %s/server.log", log_dir)
    return logger
