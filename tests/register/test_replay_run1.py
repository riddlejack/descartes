"""Run 1's recorded tree (tests/e2e/recorded/run1-tree.json, copied from the run folder)
replayed under contracts v0.4. The full replay, claims and reviews included, is
tests/e2e/replay_run1_v04.py; its numbers are in docs/notes/register-driver-v2.md."""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1] / "e2e"
sys.path.insert(0, str(HERE))

from replay_run1_v04 import tree_replay


def test_run1_tree_under_v04() -> None:
    nodes = json.loads((HERE / "recorded/run1-tree.json").read_text())["nodes"]
    r = tree_replay(nodes)
    # Every option, condition and rival is refused by kind: only the open lane fits.
    assert r["nodes"] == 23 and len(r["refused_by_kind"]) == 22
    assert sum("(option)" in x for x in r["refused_by_kind"]) == 5
    assert "R1 (rival)" in r["refused_by_kind"]
    # Rewritten as sub-questions with the options dropped, every node closes, but six
    # roots remain: the question asked (T) beside the option subtrees (K1, K2, L), the
    # gate (T0) and the open lane. Code cannot tell a gate or a branch about an unasked
    # option from a sub-question: that is the lead's and the blind framer's judgment.
    assert r["roots_after_drop"] == ["K1", "K2", "L", "OL", "T", "T0"]
    assert r["open"] == [] and "TB" in r["closed"]
