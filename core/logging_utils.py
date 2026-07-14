"""Central logging configuration for the extension."""

from __future__ import annotations

import logging

from .constants import ADDON_ID


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger without duplicating handlers."""
    logger = logging.getLogger(f"{ADDON_ID}.{name}")
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter("[OVMG] %(levelname)s %(name)s: %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger
