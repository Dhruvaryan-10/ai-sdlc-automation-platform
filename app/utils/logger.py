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

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


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

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(_LOG_FORMAT))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.propagate = False

    return logger
