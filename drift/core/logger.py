"""Logger configuration for Drift SDK."""

from __future__ import annotations

import logging
from typing import Literal

LogLevel = Literal["silent", "error", "warn", "info", "debug"]

# Map SDK log levels to Python logging levels
LOG_LEVEL_MAP = {
    "silent": logging.CRITICAL + 1,  # Higher than CRITICAL = no logs
    "error": logging.ERROR,
    "warn": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}


def configure_logger(log_level: LogLevel = "info", prefix: str = "TuskDrift") -> None:
    """
    Configure the Drift SDK logger.

    Args:
        log_level: Log level (silent, error, warn, info, debug). Default: info
        prefix: Logger prefix. Default: "TuskDrift"
    """
    # Get the root drift logger
    drift_logger = logging.getLogger("drift")

    # Set level
    level = LOG_LEVEL_MAP.get(log_level, logging.INFO)
    drift_logger.setLevel(level)

    # Configure handler if not already configured
    if not drift_logger.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(level)

        # Format: timestamp [TuskDrift] message
        formatter = logging.Formatter(f"%(asctime)s [{prefix}] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
        handler.setFormatter(formatter)

        drift_logger.addHandler(handler)
    else:
        # Update existing handler levels
        for handler in drift_logger.handlers:
            handler.setLevel(level)

    # Prevent propagation to root logger
    drift_logger.propagate = False


def set_log_level(log_level: LogLevel) -> None:
    """
    Change the SDK log level at runtime.

    Args:
        log_level: New log level (silent, error, warn, info, debug)
    """
    drift_logger = logging.getLogger("drift")
    level = LOG_LEVEL_MAP.get(log_level, logging.INFO)
    drift_logger.setLevel(level)

    for handler in drift_logger.handlers:
        handler.setLevel(level)


def get_log_level() -> LogLevel:
    """
    Get current log level.

    Returns:
        Current log level
    """
    drift_logger = logging.getLogger("drift")
    current_level = drift_logger.level

    for name, level in LOG_LEVEL_MAP.items():
        if level == current_level:
            return name  # type: ignore

    return "info"  # Default fallback
