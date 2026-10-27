"""Removing a tree node (tree-skill development runs, 27 Oct): the lead retired nodes it
had merged by setting them aside, which left duplicates in tree.json and would list
them as "set aside" in a deliverable. `tree_upsert({"id", "remove": <reason>})` removes
a node nothing holds; anything that holds it is named in the refusal. Set-aside stays
for "cannot change the headline"."""

from __future__ import annotations

import json
import multiprocessing as mp
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from test_store import (
    LEAD,
    WORKER,
    FakeEvidence,
    checker_for,
    launched,
    ready_run,
    reviewer_for,
    write_record,
)

from stemma.register.filing import Refused
from stemma.register.store import SET_ASIDE_MAX_CHARS, Caller, Register

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


def test_a_longer_id_in_an_order_is_not_the_node(reg: Register) -> None:
    # Codex review #5: q9._child and q9.-child are other ids, not q9 and punctuation.
    (reg.run_dir / "branches/b1").mkdir(parents=True)
    (reg.run_dir / "branches/b1/order.md").write_text("Answer q9._child, then q9.-child.\n")
    reg.tree_upsert(LEAD, node("q9"))
    reg.tree_upsert(LEAD, {"id": "q9", "remove": MERGED})
    (reg.run_dir / "branches/b1/order.md").write_text("Answer q8.\n")
    reg.tree_upsert(LEAD, node("q8"))
    _refused(reg, "q8", "branches/b1/order.md names it")


def test_a_node_id_never_ends_in_a_full_stop(reg: Register) -> None:
    # Codex re-check #2: the order matcher reads "Answer q9." as q9, so an id q9. could
    # never be held by an order; no node id ends in a full stop.
    for bad in ("q9.", "q9..", "a.b."):
        with pytest.raises(Refused, match="ends in a full stop"):
            reg.tree_upsert(LEAD, node(bad))
    reg.tree_upsert(LEAD, node("q9.1"))
    assert ids(reg) == ["q9.1"]


def test_any_recorded_fact_holding_the_id_holds_the_node(reg: Register) -> None:
    # Codex re-check #3: the general fact-value holder, alone. An assumption whose text
    # is exactly the node's id names it; nothing else does.
    reg.tree_upsert(LEAD, node("n1", branch_id="b1"))
    reg.tree_upsert(LEAD, node("q9"))
    write_record(reg, "b1")
    reg.propose_branch_done(WORKER, "b1")
    reg.record_branch_verdict(reviewer_for(reg, "b1"), "b1", "ask_user", "which one?",
                              points=[{"point": "p", "material": True, "criterion": "C1"}])
    reg.proceed_on_assumption(LEAD, "b1", "q9")
    with pytest.raises(Refused, match=r"node q9 is not removed: a recorded assumption "
                                      r"names it\.$"):
        reg.tree_upsert(LEAD, {"id": "q9", "remove": MERGED})


def test_a_narrative_naming_the_node_does_not_hold_it(reg: Register) -> None:
    # Contract 0c: only the register's records and the order files are read. The
    # framer's critique saying q9 duplicates q3 is why the lead removes q9.
    reg.tree_upsert(LEAD, node("q3"))
    reg.tree_upsert(LEAD, node("q9"))
    (reg.run_dir / "framer").mkdir()
    (reg.run_dir / "framer/critique.md").write_text("q9 duplicates q3.\n")
    (reg.run_dir / "branches/b1").mkdir(parents=True)
    (reg.run_dir / "branches/b1/record.md").write_text("See q9.\n")
    reg.tree_upsert(LEAD, {"id": "q9", "remove": MERGED})
    assert ids(reg) == ["q3"]


def test_a_node_that_ever_had_a_branch_is_not_removed(reg: Register) -> None:
    # Codex review #2: a node detached from its branch (branch cleared, status reset)
    # may still be named by the branch's record and claims; it stays.
    reg.tree_upsert(LEAD, node("q9", branch_id="b9"))
    reg.tree_upsert(LEAD, {"id": "q9", "branch_id": None, "status": "open"})
    _refused(reg, "q9", "it was assigned branch b9")


def test_a_node_its_own_branch_verdict_names_is_not_removed(reg: Register) -> None:
    # Codex review #4: reviewed, detached and set aside, the node still has its verdict.
    reg.tree_upsert(LEAD, node("q9", branch_id="b9"))
    write_record(reg, "b9")
    reg.propose_branch_done(WORKER, "b9")
    reg.record_branch_verdict(reviewer_for(reg, "b9"), "b9", "answered", "done")
    reg.tree_upsert(LEAD, {"id": "q9", "branch_id": None, "set_aside": MERGED})
    _refused(reg, "q9", "a branch verdict was recorded on it")


def test_a_node_a_revoked_reviewers_point_names_is_not_removed(reg: Register) -> None:
    # Codex review #3: a revoked session's verdict does not count toward release, but
    # its review is still recorded and still names the node.
    ready_run(reg)
    reg.tree_upsert(LEAD, node("q9"))
    (reg.run_dir / "draft/answer.v2.md").write_text("# Revised draft\n")
    reg.submit_draft(LEAD, 2)
    who = launched(reg, "deciders-eye-reviewer", "draft:2", "s-p")
    reg.record_review(who, 2, "text", [{"point": "p", "material": True, "node": "q9"}])
    reg.revoke_session("s-p", "wrong model")
    _refused(reg, "q9", "review point v2.de.1 names it")


def test_a_final_review_point_names_the_node(reg: Register) -> None:
    ready_run(reg)
    reg.tree_upsert(LEAD, node("q9"))
    (reg.run_dir / "draft/answer.v2.md").write_text("# Revised draft\n")
    reg.submit_draft(LEAD, 2)
    who = launched(reg, "deciders-eye-reviewer", "draft:2", "s-p")
    reg.record_review(who, 2, "text", [{"point": "p", "material": True, "node": "q9"}])
    _refused(reg, "q9", "review point v2.de.1 names it")


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
    with pytest.raises(Refused, match="longer than"):
        reg.tree_upsert(LEAD, {"id": "q1", "remove": "x" * (SET_ASIDE_MAX_CHARS + 1)})
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


def test_a_run_releases_after_a_removal(reg: Register) -> None:
    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    reg.tree_upsert(LEAD, node("q9", set_aside=MERGED))
    reg.tree_upsert(LEAD, {"id": "q9", "remove": MERGED})
    assert reg.finish_run(LEAD, 1)["finished"]
    rec = json.loads((reg.run_dir / "deliverable/record.json").read_text())
    assert rec["set_aside"] == []
    assert [n["id"] for n in json.loads((reg.run_dir / "tree.json").read_text())["removed"]] == [
        "q9"]


# --- A review racing a removal (Codex review #1) -------------------------------------
# The points are checked before the lock; the removal lands between that check and the
# write. Driven in that order here, through the reviewer's own lock entry.

def _removal_lands_first(reviewer_reg: Register, lead_reg: Register, nid: str) -> None:
    original = reviewer_reg._exclusive

    @contextmanager
    def lead_first() -> Iterator[None]:
        reviewer_reg._exclusive = original  # type: ignore[method-assign]  # once
        lead_reg.tree_upsert(LEAD, {"id": nid, "remove": MERGED})
        with original():
            yield

    reviewer_reg._exclusive = lead_first  # type: ignore[method-assign]


def test_a_branch_review_point_on_a_node_removed_meanwhile_is_refused(reg: Register) -> None:
    reg.tree_upsert(LEAD, node("n1", branch_id="b1"))
    reg.tree_upsert(LEAD, node("q9"))
    write_record(reg, "b1")
    reg.propose_branch_done(WORKER, "b1")
    who = reviewer_for(reg, "b1")
    _removal_lands_first(reg, Register(reg.run_dir, evidence=FakeEvidence()), "q9")
    with pytest.raises(Refused, match="point 1 names node 'q9', which is not in the tree"):
        reg.record_branch_verdict(who, "b1", "bound", "unbounded",
                                  points=[{"point": "no range", "material": True,
                                           "node": "q9"}])
    assert reg.run_facts("branch_verdict_recorded") == []
    assert "q9" not in ids(reg)


def test_a_final_review_point_on_a_node_removed_meanwhile_is_refused(reg: Register) -> None:
    ready_run(reg)
    reg.tree_upsert(LEAD, node("q9"))
    (reg.run_dir / "draft/answer.v2.md").write_text("# Revised draft\n")
    reg.submit_draft(LEAD, 2)
    who = launched(reg, "deciders-eye-reviewer", "draft:2", "s-p")
    _removal_lands_first(reg, Register(reg.run_dir, evidence=FakeEvidence()), "q9")
    with pytest.raises(Refused, match="point 1 names node 'q9', which is not in the tree"):
        reg.record_review(who, 2, "text", [{"point": "p", "material": True, "node": "q9"}])
    assert [r["version"] for r in reg.run_facts("review_recorded")] == [1, 1, 1]
    assert not (reg.run_dir / "reviews/v2").exists()


# --- A removal racing a branch assignment (separate processes, as MCP servers are) -----

# Resource limit: race rounds, each a fresh run with one removal and one assignment.
_ROUNDS = 12


_RIVALS: dict[str, dict[str, Any]] = {
    "assign": {"id": "q3", "branch_id": "b3"},
    "child": node("q31", parent="q3"),
}


def _race(run: str, barrier: Any, which: str, out: Any) -> None:
    reg = Register(Path(run), evidence=FakeEvidence())
    try:
        barrier.wait()
        if which == "remove":
            reg.tree_upsert(LEAD, {"id": "q3", "remove": MERGED})
        else:
            reg.tree_upsert(LEAD, _RIVALS[which])
        out.put((which, "ok"))
    except Refused as exc:
        out.put((which, str(exc)))
    except BaseException:
        barrier.abort()  # release the other racer: no hang
        raise


@pytest.mark.parametrize("rival,held_by,refused_after_removal", [
    ("assign", "branch b3 is assigned to it", "q3 was removed"),
    ("child", "child nodes q31", "parent q3 is not a node"),
])
def test_a_removal_racing_a_change_leaves_one_consistent_outcome(
        tmp_path: Path, rival: str, held_by: str, refused_after_removal: str) -> None:
    # Each round ends in exactly one of two consistent states; which one is the
    # scheduler's (the review-race tests above drive one order deterministically).
    ctx = mp.get_context("spawn")
    for i in range(_ROUNDS):
        run = tmp_path / f"run{i}"
        run.mkdir()
        Register(run, evidence=FakeEvidence()).tree_upsert(LEAD, node("q3"))
        barrier, out = ctx.Barrier(2), ctx.Queue()
        procs = [ctx.Process(target=_race, args=(str(run), barrier, w, out))
                 for w in ("remove", rival)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
        assert [p.exitcode for p in procs] == [0, 0]
        got = dict(out.get(timeout=5) for _ in procs)
        reg = Register(run, evidence=FakeEvidence())
        if got["remove"] == "ok":
            # Removal first: the change is refused and the tree is empty.
            assert refused_after_removal in got[rival]
            assert ids(reg) == [] and reg.run_facts("node_removed")
        else:
            # The change first: the removal is refused, naming what now holds the node.
            assert got[rival] == "ok" and held_by in got["remove"]
            assert "q3" in ids(reg) and not reg.run_facts("node_removed")
