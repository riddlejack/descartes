"""Contracts v0.4 (the owner's decisions D2, D4, D5, D9 after run 1): question nodes only,
set-aside nodes, criteria, review points tied to a node or criterion, a branch sent back
once, and the frame-only check. Hostile cases try to get round each."""

from __future__ import annotations

import os
import time
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

from stemma.register import checks_api, criteria
from stemma.register.filing import Refused
from stemma.register.store import Register, closure

C = "- C1: Says whether the target is met.\n- C2: Says which count the target uses.\n"


@pytest.fixture
def reg(tmp_path: Path) -> Register:
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "criteria.md").write_text(C)
    return Register(tmp_path / "run", evidence=FakeEvidence())


def mat(text: str = "the count is a different universe", **where: Any) -> dict[str, Any]:
    return {"point": text, "material": True, **(where or {"criterion": "C1"})}


def revision(reg: Register, branch: str, n: int, names: str) -> None:
    (reg.run_dir / "branches" / branch / f"revision-{n}.md").write_text(
        f"# Finding\n{names}\n# Raised by\nbranch reviewer\n# Is it right?\nyes\n# Fix\nx\n")


# --- D2: the tree holds sub-questions only ------------------------------------------

@pytest.mark.parametrize("kind", ["option", "condition", "rival", "gate", "check", "", None])
def test_options_rivals_and_checks_are_never_nodes(reg: Register, kind: Any) -> None:
    with pytest.raises(Refused, match="kind must be one of question, open_lane"):
        reg.tree_upsert(LEAD, {"id": "n1", "kind": kind, "statement": "x"})
    assert reg.tree() == []


# --- D4: criteria -------------------------------------------------------------------

def test_criteria_are_read_from_their_lines() -> None:
    text = "# Criteria\n\n- C1: one\n* **C2**: two\nC3) three\nnot a criterion\n- C4. four\n"
    assert criteria.parse(text) == {"C1": "one", "C2": "two", "C3": "three", "C4": "four"}


@pytest.mark.parametrize("text", ["- C1: a\n- C1: b\n", "- C2: a\n", "- C1: a\n- C3: b\n"])
def test_criteria_ids_run_in_order_without_repeats(text: str) -> None:
    with pytest.raises(criteria.CriteriaInvalid):
        criteria.parse(text)


def test_release_needs_criteria(reg: Register) -> None:
    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    (reg.run_dir / "criteria.md").write_text("# Criteria\n\nnone yet\n")
    assert reg.finish_failures(1) == ["criteria.md names no criterion (- C1: ...)"]
    (reg.run_dir / "criteria.md").unlink()
    assert reg.finish_failures(1) == ["criteria.md names no criterion (- C1: ...)"]
    (reg.run_dir / "criteria.md").write_text("- C1: a\n- C1: b\n")
    assert reg.finish_failures(1) == ["criteria.md names C1 twice"]


# --- D4: review points name a node or a criterion -----------------------------------

@pytest.mark.parametrize("point", [
    {"point": "p", "material": True},
    {"point": "p", "material": True, "node": None, "criterion": None},
    {"point": "p", "material": True, "node": "n9"},
    {"point": "p", "material": True, "criterion": "C3"},
    {"point": "p", "material": True, "criterion": "c1"},
    {"point": "p", "material": True, "node": ["n1"]},
])
def test_a_review_point_names_an_existing_node_or_criterion(reg: Register,
                                                            point: dict[str, Any]) -> None:
    ready_run(reg)
    (reg.run_dir / "draft/answer.v2.md").write_text("# v2\n")
    reg.submit_draft(LEAD, 2)
    who = launched(reg, "deciders-eye-reviewer", "draft:2", "s-p")
    with pytest.raises(Refused, match="point 1"):
        reg.record_review(who, 2, "text", [point])
    out = reg.record_review(who, 2, "text", [mat(node="n1"), mat(criterion="C2")])
    assert [p["id"] for p in out["points"]] == ["v2.de.1", "v2.de.2"]


# --- D5: set-aside nodes ------------------------------------------------------------

def test_setting_a_node_aside_needs_the_lead_and_a_reason(reg: Register) -> None:
    reg.tree_upsert(LEAD, {"id": "q1", "kind": "question", "statement": "x"})
    with pytest.raises(Refused, match="do not set nodes aside"):
        reg.tree_upsert(WORKER, {"id": "q1", "set_aside": "not needed"})
    for bad in ("", "   ", 3, ["x"]):
        with pytest.raises(Refused, match="set_aside is the reason"):
            reg.tree_upsert(LEAD, {"id": "q1", "set_aside": bad})
    with pytest.raises(Refused, match="giving set_aside its reason"):
        reg.tree_upsert(LEAD, {"id": "q1", "status": "set_aside"})
    assert reg.tree()[0]["status"] == "open"


def test_a_set_aside_subtree_is_closed_listed_and_shown(reg: Register) -> None:
    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    reg.tree_upsert(LEAD, {"id": "q2", "kind": "question", "statement": "Which universe?"})
    reg.tree_upsert(LEAD, {"id": "q21", "kind": "question", "statement": "a", "parent": "q2",
                           "branch_id": "b2"})
    assert any(f.startswith("node q21 is open") for f in reg.finish_failures(1))
    reg.tree_upsert(LEAD, {"id": "q2", "set_aside": "neither answer moves the headline"})
    view = {n["id"]: n for n in reg.tree_view()}
    assert view["q2"]["closes_by"] == "set_aside" and view["q2"]["closed"]
    assert view["q21"]["closes_by"] == "ancestor_set_aside" and view["q21"]["set_aside_by"] == "q2"
    assert reg.finish_failures(1) == []
    g = reg.gate_status(LEAD, 1)
    assert g["set_aside"] == [
        {"id": "q2", "statement": "Which universe?",
         "reason": "neither answer moves the headline"},
        {"id": "q21", "statement": "a", "under": "q2"}]
    assert "(set aside: neither answer moves the headline)" in \
        (reg.run_dir / "tree.md").read_text()
    # A branch under a set-aside node cannot be proposed: it needs no review.
    write_record(reg, "b2")
    with pytest.raises(Refused, match="no tree node that is not set aside"):
        reg.propose_branch_done(WORKER, "b2")
    # Brought back, it starts again and blocks until a branch verdict closes it.
    reg.tree_upsert(LEAD, {"id": "q2", "set_aside": None})
    assert any(f.startswith("node q21 is open") for f in reg.finish_failures(1))
    with pytest.raises(Refused, match="node q21 is open"):
        reg.finish_run(LEAD, 1)


def test_setting_aside_a_reviewed_node_and_bringing_it_back_needs_a_new_verdict(
        reg: Register) -> None:
    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    reg.tree_upsert(LEAD, {"id": "n1", "set_aside": "moot"})
    reg.tree_upsert(LEAD, {"id": "n1", "set_aside": None})
    assert reg.tree()[0]["status"] == "in_progress"
    assert "node n1 is in_progress (question, branch b1)" in reg.finish_failures(1)


def test_closure_of_set_aside_nodes() -> None:
    def n(nid: str, parent: str | None = None, **kw: Any) -> dict[str, Any]:
        return {"id": nid, "kind": "question", "parent": parent, "status": "open",
                "branch_id": None, "statement": nid, **kw}

    c = closure([n("R"), n("A", "R", set_aside="moot"), n("A1", "A"), n("B", "R",
                 status="answered", branch_id="b")])
    assert c["R"] == {"closes_by": "children", "status": "answered", "closed": True}
    assert c["A1"]["closes_by"] == "ancestor_set_aside" and c["A1"]["closed"]
    only_aside = closure([n("R"), n("A", "R", set_aside="moot")])
    assert only_aside["R"]["status"] == "set_aside" and only_aside["R"]["closed"]


# --- D9: a branch is sent back once --------------------------------------------------

def _proposed(reg: Register, branch: str = "b1") -> None:
    if not reg.tree():
        reg.tree_upsert(LEAD, {"id": "n1", "kind": "question", "statement": "x",
                               "branch_id": branch})
    write_record(reg, branch)
    reg.propose_branch_done(WORKER, branch)


def test_a_send_back_needs_a_material_point(reg: Register) -> None:
    _proposed(reg)
    rev = reviewer_for(reg, "b1")
    for points in (None, [], [{"point": "nit", "material": False, "criterion": "C1"}]):
        with pytest.raises(Refused, match="needs at least one material point"):
            reg.record_branch_verdict(rev, "b1", "analyse", "thin", points=points)


def test_a_branch_is_sent_back_once_then_closes_with_every_point_resolved(
        reg: Register) -> None:
    _proposed(reg)
    reg.record_branch_verdict(reviewer_for(reg, "b1", "s-r1"), "b1", "bound", "unbounded",
                              points=[mat("no range given", node="n1"),
                                      {"point": "style", "material": False, "node": "n1"}])
    assert [p["id"] for p in reg.sent_back("b1")] == ["b1.r1.1"]
    # Re-proposing needs a revision order naming the point.
    write_record(reg, "b1")
    with pytest.raises(Refused, match="point b1.r1.1 was sent back; no revision order"):
        reg.propose_branch_done(WORKER, "b1")
    (reg.run_dir / "branches/b1/revision-1.md").write_text("# Finding\nb1.r1.1\n")  # headings
    with pytest.raises(Refused, match="no revision order"):
        reg.propose_branch_done(WORKER, "b1")
    revision(reg, "b1", 1, "b1.r1.1 no range given")
    reg.propose_branch_done(WORKER, "b1")
    rev2 = reviewer_for(reg, "b1", "s-r2")
    # Hostile: sending it back again, with the same point or a new one, in any form.
    for verdict, kw in (("find_more", {"method": "more"}), ("analyse", {}), ("bound", {})):
        with pytest.raises(Refused, match="a branch is sent back once"):
            reg.record_branch_verdict(rev2, "b1", verdict, "still thin",
                                      points=[mat("a new point", criterion="C2")], **kw)
    # Closing without accounting for the point is refused; so is an invented point.
    with pytest.raises(Refused, match="point b1.r1.1 was sent back on this branch"):
        reg.record_branch_verdict(rev2, "b1", "answered", "ok")
    with pytest.raises(Refused, match="no point b1.r9.9"):
        reg.record_branch_verdict(rev2, "b1", "answered", "ok", resolutions=[
            {"point_id": "b1.r1.1", "status": "resolved"},
            {"point_id": "b1.r9.9", "status": "resolved"}])
    # Bounded needs the record to name the point.
    with pytest.raises(Refused, match="the record does not name it"):
        reg.record_branch_verdict(rev2, "b1", "answered", "ok", resolutions=[
            {"point_id": "b1.r1.1", "status": "bounded"}])
    out = reg.record_branch_verdict(rev2, "b1", "answered", "ok", resolutions=[
        {"point_id": "b1.r1.1", "status": "disputed", "note": "the lead upheld the worker"}])
    assert out["resolutions"][0]["status"] == "disputed"
    assert reg.tree()[0]["status"] == "answered"


def test_conditional_needs_the_record_to_name_the_point(reg: Register) -> None:
    _proposed(reg)
    reg.record_branch_verdict(reviewer_for(reg, "b1", "s-r1"), "b1", "analyse", "x",
                              points=[mat()])
    revision(reg, "b1", 1, "b1.r1.1")
    (reg.run_dir / "branches/b1/record.md").write_text(
        (reg.run_dir / "branches/b1/record.md").read_text()
        + "\nConditional on b1.r1.1: holds only for the 2024 series.\n")
    reg.propose_branch_done(WORKER, "b1")
    reg.record_branch_verdict(reviewer_for(reg, "b1", "s-r2"), "b1", "accept_gap", "x",
                              gap_type="method_exhausted",
                              resolutions=[{"point_id": "b1.r1.1", "status": "conditional"}])
    assert reg.tree()[0]["status"] == "gap_accepted"


def test_a_revoked_send_back_does_not_count(reg: Register) -> None:
    _proposed(reg)
    reg.record_branch_verdict(reviewer_for(reg, "b1", "s-bad"), "b1", "analyse", "x",
                              points=[mat()])
    reg.revoke_session("s-bad", "wrong model")
    assert reg.sent_back("b1") == []


def test_ask_user_is_not_a_send_back(reg: Register) -> None:
    _proposed(reg)
    reg.record_branch_verdict(reviewer_for(reg, "b1", "s-r1"), "b1", "ask_user",
                              "which series?", points=[mat()])
    assert reg.sent_back("b1") == []


# --- Frame-only runs: what the frame lacks ------------------------------------------

def test_frame_failures_name_what_is_missing_until_the_frame_is_done(reg: Register) -> None:
    (reg.run_dir / "criteria.md").unlink()
    assert reg.frame_failures() == [
        "intake.md does not exist", "frame.md does not exist", "criteria.md names no criterion",
        "the tree has no nodes", "the blind framer's critique is not recorded"]
    (reg.run_dir / "intake.md").write_text("i")
    (reg.run_dir / "criteria.md").write_text(C)
    reg.tree_upsert(LEAD, {"id": "q1", "kind": "question", "statement": "x"})
    (reg.run_dir / "framer").mkdir()
    checks_api.request_check(reg.run_dir, "blind-framer", "frame:own", "run_start")
    reg.record_frame(launched(reg, "blind-framer", "frame:own", "s-f1"), "own", "own")
    (reg.run_dir / "frame.md").write_text("frame")
    reg.request_frame_critique(LEAD)
    (reg.run_dir / "framer/lead-response.md").write_text("an old response")
    old = time.time() - 600
    os.utime(reg.run_dir / "framer/lead-response.md", (old, old))
    reg.record_frame(launched(reg, "blind-framer", "frame:critique", "s-f2"), "critique", "c")
    assert reg.frame_failures() == ["framer/lead-response.md predates the latest critique"]
    (reg.run_dir / "framer/lead-response.md").write_text("answered")
    assert reg.frame_failures() == []

