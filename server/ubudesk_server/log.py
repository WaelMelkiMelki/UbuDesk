"""Logging setup for the UbuDesk server."""

from __future__ import annotations

import logging
import sys


def setup(level: str = "info") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # zeroconf is chatty at DEBUG
    logging.getLogger("zeroconf").setLevel(logging.WARNING)
