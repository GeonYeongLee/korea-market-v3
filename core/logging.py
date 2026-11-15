"""Logging configuration with Rich formatting."""
from __future__ import annotations

import logging
from rich.logging import RichHandler


def setup_logger(name: str = "korea_market", level: str = "INFO") -> logging.Logger:
    """Configures and returns a structured rich logger."""
    logger = logging.getLogger(name)
    logger.setLevel(level.upper())
    
    if not logger.handlers:
        console_handler = RichHandler(
            rich_tracebacks=True,
            markup=True,
            show_time=True,
            show_path=False,
        )
        formatter = logging.Formatter("%(message)s", datefmt="[%X]")
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        
    return logger

