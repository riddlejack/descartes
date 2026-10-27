"""W2's public register functions, looked up at run time.

The report reads a run with the register's own code where W2 provides it (the release
manifest's check, the log parser, the derivation bundle and the filed re-run). Until the
register branch is merged, each caller falls back to the report's own stop-gap, marked
as such where it is defined.
"""

from __future__ import annotations

import importlib
from typing import Any


def w2(module: str, name: str) -> Any:
    """`module.name` when the register provides it, else None."""
    try:
        return getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError):
        return None
