"""Centralized logging configuration for SparkGrid Web Client.

Provides per-category file loggers and a shared error-only log.

Usage in modules:
    from log_config import get_logger
    logger = get_logger("automation")

    logger.info("message")
    logger.error("error message")
    logger.warning("warning message")

Categories: server, workers, automation, browser, warmup, verifier,
            background, analytics, proxy, onboarding, playwright_guard
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ── Resolve log directory ──────────────────────────────────────────
_DATA_DIR = Path(os.environ.get("SPARKGRID_DATA_DIR") or Path(__file__).resolve().parent / "data").resolve()
LOG_ROOT = _DATA_DIR / "logs"

# Sub-directories for each category
_CATEGORIES = {
    "server":       LOG_ROOT / "server",
    "workers":      LOG_ROOT / "workers",
    "automation":   LOG_ROOT / "automation",
    "browser":      LOG_ROOT / "browser",
    "warmup":       LOG_ROOT / "warmup",
    "verifier":     LOG_ROOT / "verifier",
    "background":   LOG_ROOT / "background",
    "analytics":    LOG_ROOT / "analytics",
    "proxy":         LOG_ROOT / "proxy",
    "onboarding":   LOG_ROOT / "onboarding",
    "playwright_guard": LOG_ROOT / "playwright_guard",
}

# Ensure all directories exist
for _dir in _CATEGORIES.values():
    _dir.mkdir(parents=True, exist_ok=True)

# ── Shared error log ──────────────────────────────────────────────
_ERRORS_LOG_PATH = LOG_ROOT / "errors-combined.log"

# Shared handler — only ERROR and above go to errors-combined.log
_shared_error_handler: RotatingFileHandler | None = None
_loggers: dict[str, logging.Logger] = {}


def _init_shared_handler() -> RotatingFileHandler:
    """Create the shared error-only handler with rotation (10 MB × 3)."""
    global _shared_error_handler
    if _shared_error_handler is not None:
        return _shared_error_handler
    handler = RotatingFileHandler(
        str(_ERRORS_LOG_PATH),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=3,
        encoding="utf-8",
    )
    handler.setLevel(logging.ERROR)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _shared_error_handler = handler
    return handler


def get_logger(category: str) -> logging.Logger:
    """Get a logger for the given category.

    Each logger writes:
    - INFO+ to its category file (e.g. logs/automation/automation.log)
    - ERROR+ to the shared errors-combined.log

    Args:
        category: One of: server, workers, automation, browser, warmup,
                  verifier, background, analytics, proxy, onboarding,
                  playwright_guard

    Returns:
        A configured logging.Logger instance.
    """
    if category in _loggers:
        return _loggers[category]

    log_dir = _CATEGORIES.get(category)
    if log_dir is None:
        log_dir = LOG_ROOT / category
        log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(f"sparkgrid.{category}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    # Category-specific file handler (INFO+)
    category_handler = RotatingFileHandler(
        str(log_dir / f"{category}.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    category_handler.setLevel(logging.INFO)
    category_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(category_handler)

    # Shared error handler (ERROR+ only)
    shared = _init_shared_handler()
    if shared not in (h for h in logger.handlers):
        logger.addHandler(shared)

    _loggers[category] = logger
    return logger


def log_to_file_and_print(category: str, message: str, level: str = "INFO") -> None:
    """Drop-in replacement for the existing `def log(msg, level)` pattern.

    Writes to the category log file AND prints to stdout (so existing
    console output and task-*.log capture still work).
    """
    logger = get_logger(category)
    method = getattr(logger, level.lower(), logger.info)
    method(message)
    print(f"[{level}] {message}", flush=True)


class StreamToLogger:
    """Redirect a stream (stdout/stderr) to a logger, line by line."""

    def __init__(self, logger: logging.Logger, level: int = logging.INFO) -> None:
        self._logger = logger
        self._level = level
        self._buffer = ""

    def write(self, data: str) -> int:
        if not data:
            return 0
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")
            if line.strip():
                self._logger.log(self._level, line)
        return len(data)

    def flush(self) -> None:
        if self._buffer.strip():
            self._logger.log(self._level, self._buffer.rstrip())
        self._buffer = ""

    def isatty(self) -> bool:
        return False


def redirect_server_stdout() -> None:
    """Redirect Uvicorn stdout/stderr to logs/server/server.log.

    Call once at startup, before uvicorn.run().
    """
    logger = get_logger("server")
    sys.stdout = StreamToLogger(logger, logging.INFO)  # type: ignore[assignment]
    sys.stderr = StreamToLogger(logger, logging.WARNING)  # type: ignore[assignment]
