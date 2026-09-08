"""
Centralized logging setup.

WHY: The spec requires logging every stage (upload, parsing, prompt
generation, Gemini calls, version creation, errors, execution time).
Rather than each module configuring its own logger, we expose a single
`get_logger(name)` factory so all logs land in one rotating file with a
consistent format, while still tagging *which* module produced each line.
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.utils.config import settings
from app.utils.run_context import current_run_id

# Phase 13A: every record carries the active pipeline `run_id` (or "-" outside a
# run) so any log line can be correlated to the run that produced it.
_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | run_id=%(run_id)s | %(message)s"


class _RunIdFilter(logging.Filter):
    """Attach the current `run_id` to every record so `_LOG_FORMAT` always
    resolves, with zero changes to any existing `logger.*(...)` call site."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "run_id"):
            try:
                record.run_id = current_run_id()
            except Exception:  # pragma: no cover - logging must never break
                record.run_id = "-"
        return True


_RUN_ID_FILTER = _RunIdFilter()


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger. Safe to call repeatedly (won't duplicate handlers)."""
    logger = logging.getLogger(name)

    if logger.handlers:
        # Already configured (e.g. Streamlit re-imports modules on rerun).
        return logger

    logger.setLevel(settings.log_level.upper())

    log_dir: Path = settings.resolved_log_dir()
    file_handler = RotatingFileHandler(
        log_dir / "app.log",
        maxBytes=5 * 1024 * 1024,  # 5 MB per file
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    file_handler.addFilter(_RUN_ID_FILTER)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    console_handler.addFilter(_RUN_ID_FILTER)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.propagate = False

    return logger
