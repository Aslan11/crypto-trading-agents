"""Shared logging utilities for standardized logging setup across agents."""

import os
import logging
from typing import Optional
from agents.constants import DEFAULT_LOG_LEVEL


def setup_logging(
    name: str = None, 
    level: Optional[str] = None, 
    format_string: Optional[str] = None
) -> logging.Logger:
    """Set up standardized logging configuration.
    
    Parameters
    ----------
    name:
        Logger name, defaults to __name__ of calling module
    level:
        Log level, defaults to LOG_LEVEL env var or DEFAULT_LOG_LEVEL
    format_string:
        Log format string, defaults to standard format
        
    Returns
    -------
    logging.Logger
        Configured logger instance
    """
    log_level = level or os.environ.get("LOG_LEVEL", DEFAULT_LOG_LEVEL)
    log_format = format_string or "[%(asctime)s] %(levelname)s: %(message)s"
    
    # Configure basic logging
    logging.basicConfig(level=log_level, format=log_format)
    
    # Get logger
    logger = logging.getLogger(name)
    
    # Suppress noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("temporalio").setLevel(logging.WARNING)
    
    return logger


def get_logger(name: str = None) -> logging.Logger:
    """Get a logger with the standard configuration.
    
    Parameters
    ----------
    name:
        Logger name, defaults to __name__ of calling module
        
    Returns
    -------
    logging.Logger
        Logger instance
    """
    return setup_logging(name)