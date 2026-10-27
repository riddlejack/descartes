"""What a recorded calculation script in `analysis/` uses to read figures and report.

A script reads its inputs only through `inputs()`, a mapping from claim ids to their
values, and reports with `result(number)`. So a figure crosses into analysis as a
claim id, never retyped (CONTRACTS section 5, "Derivations").

Where the values come from is behind one interface, `ClaimValues`:
- when `file_claim` re-runs the script, the register writes the inputs to a file and
  names it in `STEMMA_ANALYSIS_INPUTS`; the script sees exactly those claims;
- when an agent runs the script while working, values are read from the run's
  register (`STEMMA_RUN_DIR`);
- a later part adds a backend for a firm's own data (Zetaris).

    from stemma.analysis import inputs, result
    v = inputs("3f2a9c0d1b4e", "77aa01c2d3e4")
    result(v["3f2a9c0d1b4e"] / v["77aa01c2d3e4"])

A script may also read a whole captured document (a dataset, a table) with
`document("<receipt_id>")`; name each such receipt in the derivation's `documents`:
`{"script": "analysis/x.py", "inputs": [claim ids], "documents": [receipt ids]}`.
The re-run copies the script's bundle: the script and the modules under its own folder
in analysis/ that it imports (`import helper`, `from pkg import util`, relative imports
inside a package), found by reading the code, transitively. Nothing else is copied: no
data file, no module reached through a link or above the script's folder, no module
named only at run time (`importlib.import_module`). At filing the bundle is stored by
its hash, and a filed claim is re-run from that copy, so editing analysis/ afterwards
changes nothing filed. Run a script exactly as filing will with the `run_analysis`
tool, or with "$STEMMA_PYTHON".
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

__all__ = ["AnalysisError", "ClaimValues", "document", "inputs", "result", "set_backend"]

INPUTS_ENV = "STEMMA_ANALYSIS_INPUTS"
RESULT_ENV = "STEMMA_ANALYSIS_RESULT"
DOCUMENTS_ENV = "STEMMA_ANALYSIS_DOCUMENTS"


class AnalysisError(Exception):
    """An input is missing, unknown or has no number, or a result is not a number."""


class ClaimValues(Protocol):
    def values(self, ids: Sequence[str]) -> dict[str, float]:
        """The numbers of the named claims, keyed by the ids as given."""
        ...


def _match(given: str, available: Mapping[str, float]) -> str:
    hits = [k for k in available if k == given or k.startswith(given)]
    if len(hits) != 1:
        raise AnalysisError(f"claim {given} is not among this script's inputs")
    return hits[0]


class FileValues:
    """The inputs file the register writes for a re-run (`{claim_id: number}`)."""

    def __init__(self, path: Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise AnalysisError("the inputs file is not a mapping")
        self.available: dict[str, float] = {str(k): float(v) for k, v in data.items()}

    def values(self, ids: Sequence[str]) -> dict[str, float]:
        if not ids:
            return dict(self.available)
        return {i: self.available[_match(i, self.available)] for i in ids}


class RegisterValues:
    """Values read from the run's register while an agent works."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir

    def values(self, ids: Sequence[str]) -> dict[str, float]:
        if not ids:
            raise AnalysisError("name the claim ids this script reads")
        from stemma.register.store import open_register  # the register is optional here

        reg = open_register(self.run_dir)
        out: dict[str, float] = {}
        for i in ids:
            claim = reg.get_claim(i)
            number = (claim.get("value") or {}).get("number")
            if not isinstance(number, int | float):
                raise AnalysisError(f"claim {i} has no number")
            out[i] = float(number)
        return out


_backend: ClaimValues | None = None
_reported = False


def set_backend(backend: ClaimValues | None) -> None:
    """Choose where values come from (a later part plugs in other sources)."""
    global _backend
    _backend = backend


def _current() -> ClaimValues:
    if _backend is not None:
        return _backend
    if os.environ.get(INPUTS_ENV):
        return FileValues(Path(os.environ[INPUTS_ENV]))
    if os.environ.get("STEMMA_RUN_DIR"):
        return RegisterValues(Path(os.environ["STEMMA_RUN_DIR"]))
    raise AnalysisError("no source of claim values: run inside a Stemma run")


def inputs(*ids: str) -> dict[str, float]:
    """The values of the named claims (all inputs when re-run and none are named)."""
    return _current().values(list(ids))


def document(receipt_id: str) -> str:
    """The whole stored text of a captured document (a dataset, a table, a filing), by
    receipt id. Name each receipt in the derivation's "documents"; when file_claim re-runs
    the script it gets exactly those texts."""
    folder = os.environ.get(DOCUMENTS_ENV)
    if folder:
        path = Path(folder) / f"{receipt_id}.txt"
        if "/" in receipt_id or not path.is_file():
            raise AnalysisError(f"receipt {receipt_id} is not among this script's documents")
        return path.read_text(encoding="utf-8")
    if os.environ.get("STEMMA_RUN_DIR"):
        from stemma.evidence.api import read_text  # the evidence service (part A)

        return read_text(os.environ["STEMMA_RUN_DIR"], receipt_id)
    raise AnalysisError("no source of documents: run inside a Stemma run")


def result(number: float) -> None:
    """Report the script's one result. A second call is an error."""
    global _reported
    if isinstance(number, bool) or not isinstance(number, int | float):
        raise AnalysisError("the result is not a number")
    if not math.isfinite(number):
        raise AnalysisError("the result is not finite")
    if _reported:
        raise AnalysisError("result() was already called")
    _reported = True
    target = os.environ.get(RESULT_ENV)
    if target:
        Path(target).write_text(json.dumps({"number": number}), encoding="utf-8")
    else:
        print(f"result: {number!r}")
