"""Centralized logging configuration for fxtr.

This module provides a single point of configuration for all fxtr logging.
It reads from the FXTR_LOG_LEVEL environment variable and can be overridden
via CLI flags.
"""

import logging
import os
from typing import Literal

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Default format: timestamp - level - logger name - message
DEFAULT_FORMAT = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"

# Environment variable for log level
LOG_LEVEL_ENV_VAR = "FXTR_LOG_LEVEL"

# Default log level
DEFAULT_LOG_LEVEL: LogLevel = "INFO"

_configured = False


def configure_logging(level: str | None = None) -> None:
    """Configure logging for fxtr.

    This should be called once at application startup. It configures the root
    'fxtr' logger with a StreamHandler that outputs to stderr.

    Args:
        level: Override log level. If None, reads from FXTR_LOG_LEVEL env var,
               defaulting to INFO. Valid values: DEBUG, INFO, WARNING, ERROR, CRITICAL.
    """
    global _configured

    # Determine the log level
    if level is None:
        level = os.environ.get(LOG_LEVEL_ENV_VAR, DEFAULT_LOG_LEVEL)

    # Normalize level to uppercase
    level = level.upper()

    # Validate and convert to logging constant
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "WARN": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }

    log_level = level_map.get(level)
    if log_level is None:
        # Fall back to INFO for invalid levels, but warn about it
        log_level = logging.INFO
        print(f"Warning: Invalid log level '{level}', using INFO")

    # Get the root fxtr logger
    fxtr_logger = logging.getLogger("fxtr")

    # If already configured, just update the level
    if _configured:
        fxtr_logger.setLevel(log_level)
        return

    # Configure the logger
    fxtr_logger.setLevel(log_level)

    # Create handler if none exists
    if not fxtr_logger.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(logging.DEBUG)  # Handler accepts all; logger filters
        formatter = logging.Formatter(DEFAULT_FORMAT)
        handler.setFormatter(formatter)
        fxtr_logger.addHandler(handler)

    # Prevent propagation to root logger to avoid duplicate messages
    fxtr_logger.propagate = False

    _configured = True
