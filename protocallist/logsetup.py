"""One rotating log file. What was said in a conversation, and the titles of windows, never go here."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from protocallist import paths


def setup(name: str = "protocallist") -> logging.Logger:
    log = logging.getLogger("protocallist")
    if not any(isinstance(h, RotatingFileHandler) for h in log.handlers):
        handler = RotatingFileHandler(
            paths.logs_dir() / f"{name}.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        # The library that draws the icon logs under its own name; without this, what it says
        # when its loop or a menu handler fails goes nowhere under a windowless Python.
        logging.getLogger("pystray").addHandler(handler)
    return log
