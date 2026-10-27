"""What "done" means for the question (decision D4): the criteria in `criteria.md`.

The driver writes `criteria.md` from `task.json` when the user gave criteria (then no
agent may change it); otherwise the lead writes it during intake. Either way each
criterion is one line `- C<n>: <text>` (a leading `*`, bold markers around the id, or
`.` or `)` after it are accepted). Code reads the ids so a review point can name one;
whether a criterion is met is always a reviewer's judgment, never code's.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final

from stemma.register import safeio

CRITERIA_FILE: Final = "criteria.md"
_LINE: Final = re.compile(r"^\s*(?:[-*+]\s+)?\**(C[1-9]\d{0,2})\**\s*[:.)]\s*\**\s*(\S.*?)\s*$")


class CriteriaInvalid(Exception):
    """criteria.md repeats an id or skips one."""


def parse(text: str) -> dict[str, str]:
    """Criterion id to text, in file order. Ids run C1, C2, ... without gaps or repeats."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        cid, body = m.group(1), m.group(2)
        if cid in out:
            raise CriteriaInvalid(f"criteria.md names {cid} twice")
        out[cid] = body
    want = [f"C{i}" for i in range(1, len(out) + 1)]
    if list(out) != want:
        raise CriteriaInvalid(f"criteria.md ids must run C1, C2, ... in order; found "
                              f"{', '.join(out) or 'none'}")
    return out


def read(run_dir: Path) -> dict[str, str]:
    """The run's criteria; empty when criteria.md does not exist yet."""
    try:
        text = safeio.read_text(run_dir, CRITERIA_FILE)
    except FileNotFoundError:
        return {}
    return parse(text)


def from_task(run_dir: Path) -> bool:
    """True when the user's task gave the criteria (the driver wrote criteria.md)."""
    try:
        task = json.loads(safeio.read_text(run_dir, "task/task.json"))
    except (FileNotFoundError, ValueError, safeio.UnsafePath, NotADirectoryError):
        return False
    return isinstance(task, dict) and bool(task.get("criteria"))
