"""Removing a tree node (tree-skill development runs, 27 Oct): the lead retired nodes it
had merged by setting them aside, which left duplicates in tree.json and would list
them as "set aside" in a deliverable. `tree_upsert({"id", "remove": <reason>})` removes
a node nothing holds; anything that holds it is named in the refusal. Set-aside stays
for "cannot change the headline"."""

from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path
from typing import Any

import pytest
from test_store import LEAD, WORKER, FakeEvidence, reviewer_for, write_record

from stemma.register.filing import Refused
from stemma.register.store import Caller, Register

MERGED = "Merged into q2: now asked of every unit alike"


@pytest.fixture
def reg(tmp_path: Path) -> Register:
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "criteria.md").write_text("- C1: Says whether the target is met.\n")
    return Register(tmp_path / "run", evidence=FakeEvidence())


def node(nid: str, **extra: Any) -> dict[str, Any]:
    return {"id": nid, "kind": "question", "statement": f"Question {nid}?", **extra}


def ids(reg: Register) -> list[str]:
    return [str(n["id"]) for n in reg.tree()]


def test_the_lead_removes_a_node_nothing_holds(reg: Register) -> None:
    reg.tree_upsert(LEAD, node("q1"))
    reg.tree_upsert(LEAD, node("q2", parent="q1"))
    reg.tree_upsert(LEAD, node("q3", parent="q1"))
    out = reg.tree_upsert(LEAD, {"id": "q3", "remove": MERGED})
    assert out["removed"]["reason"] == MERGED
    assert ids(reg) == ["q1", "q2"]
    tree = json.loads((reg.run_dir / "tree.json").read_text())
    assert [n["id"] for n in tree["nodes"]] == ["q1", "q2"]
    assert tree["removed"] == [{"id": "q3", "statement": "Question q3?", "reason": MERGED}]
    assert "q3" not in (reg.run_dir / "tree.md").read_text()
    assert reg.set_aside_nodes() == []  # never listed as set aside
    assert [f["node"] for f in reg.run_facts("node_removed")] == ["q3"]
    assert reg.run_facts("node_removed")[0]["reason"] == MERGED


def test_a_set_aside_duplicate_can_be_removed(reg: Register) -> None:
    # The development runs' case: merged nodes were set aside for want of a removal.
    reg.tree_upsert(LEAD, node("q1"))
    reg.tree_upsert(LEAD, node("q4", set_aside=MERGED))
    reg.tree_upsert(LEAD, {"id": "q4", "remove": MERGED})
    assert ids(reg) == ["q1"] and reg.set_aside_nodes() == []


def _refused(reg: Register, nid: str, match: str) -> None:
    before = (reg.run_dir / "register/log.jsonl").read_bytes()
    with pytest.raises(Refused, match=match):
        reg.tree_upsert(LEAD, {"id": nid, "remove": MERGED})
    assert (reg.run_dir / "register/log.jsonl").read_bytes() == before
    assert nid in ids(reg)


def test_a_node_with_children_is_not_removed(reg: Register) -> None:
    reg.tree_upsert(LEAD, node("q1"))
    reg.tree_upsert(LEAD, node("q11", parent="q1"))
    reg.tree_upsert(LEAD, node("q12", parent="q1"))
    _refused(reg, "q1", "child nodes q11, q12")


def test_a_node_with_branch_work_is_not_removed(reg: Register) -> None:
    reg.tree_upsert(LEAD, node("q1", branch_id="b1"))
    _refused(reg, "q1", "branch b1 is assigned to it")
    reg.tree_upsert(LEAD, node("q2", status="in_progress"))
    _refused(reg, "q2", "its status is in_progress")


def test_a_node_an_order_names_is_not_removed(reg: Register) -> None:
    reg.tree_upsert(LEAD, node("q3"))
    reg.tree_upsert(LEAD, node("q31"))
    (reg.run_dir / "branches/b1").mkdir(parents=True)
    (reg.run_dir / "branches/b1/order.md").write_text("Answer q31 for every unit.\n")
    reg.tree_upsert(LEAD, {"id": "q3", "remove": MERGED})  # q31 is not q3
    _refused(reg, "q31", "branches/b1/order.md names it")
    (reg.run_dir / "branches/b2/sub/b21").mkdir(parents=True)
    (reg.run_dir / "branches/b2/sub/b21/revision-1.md").write_text("Redo (q32).\n")
    reg.tree_upsert(LEAD, node("q32"))
    _refused(reg, "q32", "branches/b2/sub/b21/revision-1.md names it")


def test_a_node_a_review_point_names_is_not_removed(reg: Register) -> None:
    reg.tree_upsert(LEAD, node("n1", branch_id="b1"))
    reg.tree_upsert(LEAD, node("q9"))
    write_record(reg, "b1")
    reg.propose_branch_done(WORKER, "b1")
    reg.record_branch_verdict(reviewer_for(reg, "b1"), "b1", "bound", "unbounded",
                              points=[{"point": "no range for q9", "material": True,
                                       "node": "q9"}])
    _refused(reg, "q9", "review point b1.r1.1 names it")


def test_only_the_lead_removes_and_only_with_a_reason(reg: Register) -> None:
    reg.tree_upsert(LEAD, node("q1"))
    for who in (WORKER, Caller("stemma:lead", "a-sub", "s-lead")):
        with pytest.raises(Refused, match="remove nodes|lead"):
            reg.tree_upsert(who, {"id": "q1", "remove": MERGED})
    for bad in ("", "   ", None, 3):
        with pytest.raises(Refused, match="remove is the reason"):
            reg.tree_upsert(LEAD, {"id": "q1", "remove": bad})
    with pytest.raises(Refused, match="nothing but the node's id"):
        reg.tree_upsert(LEAD, {"id": "q1", "remove": MERGED, "statement": "x"})
    with pytest.raises(Refused, match="not a node"):
        reg.tree_upsert(LEAD, {"id": "q7", "remove": MERGED})
    assert ids(reg) == ["q1"]


def test_a_removed_id_stays_removed(reg: Register) -> None:
    reg.tree_upsert(LEAD, node("q1"))
    reg.tree_upsert(LEAD, node("q3"))
    reg.tree_upsert(LEAD, {"id": "q3", "remove": MERGED})
    with pytest.raises(Refused, match="q3 was removed"):
        reg.tree_upsert(LEAD, node("q3"))
    with pytest.raises(Refused, match="q3 was removed"):
        reg.tree_upsert(LEAD, {"id": "q3", "remove": MERGED})
    with pytest.raises(Refused, match="parent q3 is not a node"):
        reg.tree_upsert(LEAD, node("q4", parent="q3"))
    assert ids(reg) == ["q1"]


# --- A removal racing a branch assignment (separate processes, as MCP servers are) -----

# Resource limit: race rounds, each a fresh run with one removal and one assignment.
_ROUNDS = 12


def _race(run: str, barrier: Any, which: str, out: Any) -> None:
    reg = Register(Path(run), evidence=FakeEvidence())
    barrier.wait()
    try:
        if which == "remove":
            reg.tree_upsert(LEAD, {"id": "q3", "remove": MERGED})
        else:
            reg.tree_upsert(LEAD, {"id": "q3", "branch_id": "b3"})
        out.put((which, "ok"))
    except Refused as exc:
        out.put((which, str(exc)))


def test_a_removal_racing_a_branch_assignment_leaves_one_consistent_outcome(
        tmp_path: Path) -> None:
    ctx = mp.get_context("spawn")
    outcomes: set[str] = set()
    for i in range(_ROUNDS):
        run = tmp_path / f"run{i}"
        run.mkdir()
        Register(run, evidence=FakeEvidence()).tree_upsert(LEAD, node("q3"))
        barrier, out = ctx.Barrier(2), ctx.Queue()
        procs = [ctx.Process(target=_race, args=(str(run), barrier, w, out))
                 for w in ("remove", "assign")]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
        assert [p.exitcode for p in procs] == [0, 0]
        got = dict(out.get(timeout=5) for _ in procs)
        reg = Register(run, evidence=FakeEvidence())
        if got["remove"] == "ok":
            # Removal first: the assignment is refused and no node carries the branch.
            assert "q3 was removed" in got["assign"]
            assert ids(reg) == [] and reg.run_facts("node_removed")
            outcomes.add("removed")
        else:
            # Assignment first: the removal is refused, naming the branch.
            assert got["assign"] == "ok" and "branch b3 is assigned to it" in got["remove"]
            assert [n.get("branch_id") for n in reg.tree()] == ["b3"]
            assert not reg.run_facts("node_removed")
            outcomes.add("assigned")
    assert outcomes  # each round ended in exactly one of the two consistent states
