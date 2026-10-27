"""Contracts v0.4 (the owner's decisions D2, D4, D5, D9 after run 1): question nodes only,
set-aside nodes, criteria, review points tied to a node or criterion, a branch sent back
once, and the frame-only check. Hostile cases try to get round each."""

from __future__ import annotations

import json
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

from stemma.register import checks_api, criteria, jsonl
from stemma.register.filing import Refused
from stemma.register.store import Caller, Register, closure

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
    changed = "criteria.md differs from the frozen criteria; only revise_criteria changes them"
    (reg.run_dir / "criteria.md").write_text("# Criteria\n\nnone yet\n")
    assert reg.finish_failures(1) == ["criteria.md names no criterion (- C1: ...)", changed]
    (reg.run_dir / "criteria.md").unlink()
    assert reg.finish_failures(1) == ["criteria.md names no criterion (- C1: ...)", changed]
    (reg.run_dir / "criteria.md").write_text("- C1: a\n- C1: b\n")
    assert reg.finish_failures(1) == ["criteria.md names C1 twice", changed]


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
    # Re-proposing needs the point named in the record or in a revision order.
    write_record(reg, "b1")
    with pytest.raises(Refused, match="points b1.r1.1 were sent back; neither record.md"):
        reg.propose_branch_done(WORKER, "b1")
    (reg.run_dir / "branches/b1/revision-1.md").write_text("# Finding\nb1.r1.1\n")  # headings
    with pytest.raises(Refused, match="neither record.md nor a revision order"):
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


def _framed(reg: Register) -> None:
    (reg.run_dir / "intake.md").write_text("i")
    reg.tree_upsert(LEAD, {"id": "q1", "kind": "question", "statement": "x"})
    (reg.run_dir / "framer").mkdir()
    checks_api.request_check(reg.run_dir, "blind-framer", "frame:own", "run_start")
    reg.record_frame(launched(reg, "blind-framer", "frame:own", "s-f1"), "own", "own")
    (reg.run_dir / "frame.md").write_text("frame")
    reg.request_frame_critique(LEAD)


def test_frame_failures_need_a_verified_critique_session(reg: Register) -> None:
    _framed(reg)
    req = [r for r in checks_api.pending_checks(reg.run_dir) if r.prompt == "frame:critique"][-1]
    checks_api.mark_launched(reg.run_dir, req.request_id, "s-f2", "claude-opus-5-5", "high")
    reg.record_frame(Caller("stemma:blind-framer", None, "s-f2"), "critique", "c")
    (reg.run_dir / "framer/lead-response.md").write_text("answered")
    unverified = ("the critique's session s-f2 is not verified (its model and effort not yet "
                  "confirmed, or revoked)")
    assert reg.frame_failures() == [unverified]
    checks_api.mark_verified(reg.run_dir, "s-f2", "claude-opus-5-5", "high")
    assert reg.frame_failures() == []
    checks_api.revoke(reg.run_dir, "s-f2", "wrong model")  # its frames stop counting
    assert reg.frame_failures() == ["the blind framer's critique is not recorded"]


def test_frame_failures_need_the_latest_critique_round(reg: Register) -> None:
    _framed(reg)
    reg.record_frame(launched(reg, "blind-framer", "frame:critique", "s-f2"), "critique", "c")
    (reg.run_dir / "framer/lead-response.md").write_text("answered")
    assert reg.frame_failures() == []
    reg.request_frame_critique(LEAD)  # the lead asks again: round 2 has no critique yet
    assert reg.frame_failures() == [
        "the recorded critique is not from the latest critique round (round 2 asked)"]
    reg.record_frame(launched(reg, "blind-framer", "frame:critique", "s-f3"), "critique", "c2")
    (reg.run_dir / "framer/lead-response.md").write_text("answered again")
    assert reg.frame_failures() == []



# --- D7: claim checks per source document, only for cited claims --------------------

def _cite(reg: Register, version: int, *cids: str) -> None:
    from test_store import finish_reviews

    if version > 1:
        finish_reviews(reg, version - 1)
    (reg.run_dir / "draft").mkdir(exist_ok=True)
    body = "".join(f"Figure {i} [[c:{c[:12]}]].\n" for i, c in enumerate(cids, start=1))
    (reg.run_dir / f"draft/answer.v{version}.md").write_text("# Answer\n\n" + body)
    reg.submit_draft(LEAD, version)


def test_submit_starts_one_checker_per_cited_document(reg: Register) -> None:
    from test_store import claim

    a = reg.file_claim(WORKER, claim())["claim_id"]
    b = reg.file_claim(WORKER, claim(statement="Revenue rose from 1.0bn.", value={
        "number": 1.0e9, "unit": "USD", "period": "FY2024", "kind": "level",
        "as_written": "1.0bn"}, support=[{"receipt_id": "r2", "quote": "up from 1.0bn"}]))["claim_id"]
    unused = reg.file_claim(WORKER, claim(statement="Unused 1.2bn."))["claim_id"]
    assumed = reg.file_claim(WORKER, {"statement": "We assume 3 more years.", "tier": "assumed",
                                      "support": [], "branch_id": "b1", "entities": []})["claim_id"]
    assert checks_api.pending_checks(reg.run_dir) == []
    _cite(reg, 1, a, b, assumed)
    prompts = sorted(r.prompt for r in checks_api.pending_checks(reg.run_dir)
                     if r.role == "claim-checker")
    assert prompts == ["derivations:v1", "doc:r1", "doc:r2"]  # no claim: route (v0.4)
    listing = json.loads((reg.run_dir / "checks/doc-r1.r1.json").read_text())
    assert listing["claims"] == [a] and listing["round"] == 1 and listing["draft_version"] == 1
    assert unused not in json.dumps(listing)
    # A document's checker judges only the claims its list names.
    chk1 = launched(reg, "claim-checker", "doc:r1", "s-d1")
    with pytest.raises(Refused, match="not started for"):
        reg.record_claim_verdict(chk1, b, "holds", "x")
    with pytest.raises(Refused, match="not started for"):
        reg.record_claim_verdict(chk1, unused, "holds", "x")
    reg.record_claim_verdict(chk1, a, "holds", "the quote is there")
    # A subagent inside that session is refused, as before.
    with pytest.raises(Refused, match="not started for"):
        reg.record_claim_verdict(Caller("stemma:claim-checker", "a1", "s-d1"), a, "holds", "x")


def test_a_later_draft_checks_only_what_has_no_verdict(reg: Register) -> None:
    from test_store import claim

    a = reg.file_claim(WORKER, claim())["claim_id"]
    c = reg.file_claim(WORKER, claim(statement="Also 1.2bn in fiscal 2025."))["claim_id"]
    _cite(reg, 1, a)
    reg.record_claim_verdict(launched(reg, "claim-checker", "doc:r1", "s-d1"), a, "holds", "ok")
    _cite(reg, 2, a, c)
    listing = json.loads((reg.run_dir / "checks/doc-r1.r2.json").read_text())
    assert listing["claims"] == [c] and listing["round"] == 2
    # Round 1's assignment is untouched (Codex review #5).
    assert json.loads((reg.run_dir / "checks/doc-r1.r1.json").read_text())["claims"] == [a]
    # The round-1 session cannot judge round 2's claim; round 2's can.
    with pytest.raises(Refused, match="not started for"):
        reg.record_claim_verdict(Caller("stemma:claim-checker", None, "s-d1"), c, "holds", "x")
    reg.record_claim_verdict(launched(reg, "claim-checker", "doc:r1", "s-d2"), c, "holds", "ok")
    # Nothing left unverdicted: a third draft asks for no claim check.
    _cite(reg, 3, a, c)
    assert not [r for r in checks_api.pending_checks(reg.run_dir) if r.role == "claim-checker"]


def test_a_claim_listed_by_a_running_check_is_not_listed_again(reg: Register) -> None:
    from test_store import claim

    a = reg.file_claim(WORKER, claim())["claim_id"]
    _cite(reg, 1, a)
    launched(reg, "claim-checker", "doc:r1", "s-d1")  # running, no verdict yet
    _cite(reg, 2, a)
    assert len(checks_api.for_prompt(reg.run_dir, "claim-checker", "doc:r1")) == 1


def test_a_planted_link_never_redirects_the_listing(reg: Register, tmp_path: Path) -> None:
    from test_store import claim

    a = reg.file_claim(WORKER, claim())["claim_id"]
    (reg.run_dir / "checks").mkdir()
    (reg.run_dir / "elsewhere.json").write_text("{}")
    (reg.run_dir / "checks/doc-r1.r1.json").symlink_to(reg.run_dir / "elsewhere.json")
    _cite(reg, 1, a)  # no check was requested for it: replaced, never followed
    assert (reg.run_dir / "elsewhere.json").read_text() == "{}"
    assert not (reg.run_dir / "checks/doc-r1.r1.json").is_symlink()
    # A linked checks folder is refused outright.
    b = reg.file_claim(WORKER, claim(statement="Also 1.2bn in fiscal 2025."))["claim_id"]
    for f in (reg.run_dir / "checks").iterdir():
        f.unlink()
    (reg.run_dir / "checks").rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (reg.run_dir / "checks").symlink_to(outside)
    with pytest.raises(Refused, match="cannot be written safely"):
        _cite(reg, 2, b)
    assert list(outside.iterdir()) == []
    assert reg.submitted_versions() == [1]  # the refused submission recorded nothing


def test_a_running_checkers_assignment_never_changes(reg: Register) -> None:
    from test_store import claim

    a = reg.file_claim(WORKER, claim())["claim_id"]
    _cite(reg, 1, a)
    chk = launched(reg, "claim-checker", "doc:r1", "s-d1")  # running
    before = (reg.run_dir / "checks/doc-r1.r1.json").read_bytes()
    b = reg.file_claim(WORKER, claim(statement="New 1.2bn claim."))["claim_id"]
    _cite(reg, 2, a, b)
    assert (reg.run_dir / "checks/doc-r1.r1.json").read_bytes() == before
    mine = reg.check_assignment(chk)
    assert (mine["prompt"], mine["round"], mine["claims"]) == ("doc:r1", 1, [a])
    two = reg.check_assignment(launched(reg, "claim-checker", "doc:r1", "s-d2"))
    assert two["round"] == 2 and two["claims"] == [b]
    with pytest.raises(Refused, match="not started for"):
        reg.record_claim_verdict(chk, b, "holds", "x")
    with pytest.raises(Refused, match="do not read check assignments"):
        reg.check_assignment(LEAD)


# --- D8: final reviewers see the previous round's points and the lead's response ----

def test_a_new_round_shows_the_previous_points_and_needs_a_response(reg: Register) -> None:
    cid = ready_run(reg)  # v1 reviewed, no points
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    (reg.run_dir / "draft/answer.v2.md").write_text("# v2\n")
    reg.submit_draft(LEAD, 2)  # v1's three reviews are recorded, with no points
    shown = (reg.run_dir / "register/drafts/previous.v2.md").read_text()
    assert "- none" in shown and "(none written)" in shown
    for i, role in enumerate(("deciders-eye-reviewer", "evidence-auditor", "mechanical-reader")):
        pts = [mat("the count mixes ports and stations", criterion="C2", node="n1"),
               {"point": "a typo", "material": False, "node": "n1"}] if i == 0 else []
        reg.record_review(launched(reg, role, "draft:2", f"s-v2-{i}"), 2, "r", pts)
    (reg.run_dir / "draft/answer.v3.md").write_text("# v3\n")
    with pytest.raises(Refused, match="answers them before version 3"):
        reg.submit_draft(LEAD, 3)
    (reg.run_dir / "draft/response.v3.md").write_text("v2.de.2: fixed the typo.")
    with pytest.raises(Refused, match="does not name points v2.de.1"):
        reg.submit_draft(LEAD, 3)
    assert reg.submitted_versions() == [1, 2]
    (reg.run_dir / "draft/response.v3.md").write_text(
        "v2.de.1: now ports throughout.\nv2.de.2: fixed the typo.")
    reg.submit_draft(LEAD, 3)
    shown = (reg.run_dir / "register/drafts/previous.v3.md").read_text()
    assert "- v2.de.1 (material; criterion C2, node n1): the count mixes" in shown
    assert "- v2.de.2 (minor; node n1): a typo" in shown
    assert "v2.de.1: now ports throughout." in shown


def test_the_deciders_eye_reviewer_reads_its_previous_round_only(reg: Register) -> None:
    from stemma.driver import settings as st
    from stemma.hooks import guards

    rd = reg.run_dir
    (rd / "register/drafts").mkdir(parents=True)
    for name in ("answer.v2.md", "previous.v2.md", "previous.v3.md", "answer.v3.md"):
        (rd / "register/drafts" / name).write_text("x")
    deny = st.read_denials(rd, "deciders-eye-reviewer", "draft:2")
    assert "register/drafts/previous.v3.md" in deny
    assert "register/drafts/previous.v2.md" not in deny

    def read(rel: str) -> bool:
        hook = {"tool_name": "Read", "tool_input": {"file_path": str(rd / rel)},
                "agent_type": "stemma:deciders-eye-reviewer", "session_id": "s",
                "tool_use_id": "t", "cwd": str(rd)}
        return guards.guard_read(hook, rd, "2").allow

    assert read("register/drafts/previous.v2.md") and read("register/drafts/answer.v2.md")
    assert not read("register/drafts/previous.v3.md")
    assert not read("register/drafts/answer.v3.md")


def test_a_resumed_reviewer_session_answers_its_new_round_once_verified(
        reg: Register) -> None:
    _proposed(reg)
    rev = reviewer_for(reg, "b1", "s-mem")
    reg.record_branch_verdict(rev, "b1", "analyse", "x", points=[mat(node="n1")])
    revision(reg, "b1", 1, "b1.r1.1")
    reg.propose_branch_done(WORKER, "b1")
    # The driver resumes the same session for round 2 (D8).
    req = next(r for r in checks_api.pending_checks(reg.run_dir) if r.prompt == "branch:b1")
    checks_api.mark_launched(reg.run_dir, req.request_id, "s-mem", "claude-opus-5-5", "high")
    assert checks_api.state_for_session(reg.run_dir, "s-mem").request.round == 2
    assert "s-mem" not in checks_api.verified_sessions(reg.run_dir)  # until verified again
    out = reg.record_branch_verdict(rev, "b1", "answered", "fixed", resolutions=[
        {"point_id": "b1.r1.1", "status": "resolved"}])
    assert out["round"] == 2
    assert reg.await_checks(["branch:b1"], 0)["landed"] == {}
    checks_api.mark_verified(reg.run_dir, "s-mem", "claude-opus-5-5", "high")
    assert reg.await_checks(["branch:b1"], 0)["landed"]["branch:b1"]["round"] == 2


def test_the_release_writes_the_runs_record(reg: Register) -> None:
    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    reg.tree_upsert(LEAD, {"id": "q9", "kind": "question", "statement": "stations?",
                           "set_aside": "the target counts ports"})
    reg.finish_run(LEAD, 1)
    rec = json.loads((reg.run_dir / "deliverable/record.json").read_text())
    assert rec["criteria"] == {"C1": "Says whether the target is met.",
                               "C2": "Says which count the target uses."}
    assert rec["set_aside"] == [{"id": "q9", "statement": "stations?",
                                 "reason": "the target counts ports"}]
    assert rec["open_material_points"]["count"] == 0
    assert rec["review_rounds"]["used"] == 1


def _estimate(reg: Register, base: str, documents: list[str] | None = None) -> str:
    from stemma.register.derivation import RerunResult

    reg._rerun = lambda _p, _i, *_a: RerunResult(2.4e9, 0, "", 60)
    (reg.run_dir / "analysis").mkdir(exist_ok=True)
    (reg.run_dir / "analysis/double.py").write_text("x = 1\n")
    deriv: dict[str, Any] = {"script": "analysis/double.py", "inputs": [base]}
    if documents is not None:
        deriv["documents"] = documents
    est = {"statement": "Twice fiscal 2025 revenue is 2.4bn.", "tier": "estimated",
           "value": {"number": 2.4e9, "unit": "USD", "period": "FY2025", "kind": "level",
                     "as_written": "2.4bn"},
           "derivation": deriv, "support": [], "branch_id": "b1", "entities": []}
    return str(reg.file_claim(WORKER, est)["claim_id"])


def test_an_estimate_on_no_document_goes_to_the_derivations_check(reg: Register) -> None:
    from test_store import claim

    base = reg.file_claim(WORKER, claim())["claim_id"]  # never cited itself
    e = _estimate(reg, base)
    assumed = reg.file_claim(WORKER, {"statement": "We assume 3 more years.",
                                      "tier": "assumed", "support": [], "branch_id": "b1",
                                      "entities": []})["claim_id"]
    (reg.run_dir / "draft").mkdir()
    (reg.run_dir / "draft/answer.v1.md").write_text(
        f"# Answer\n\nThe doubled figure is 2.4bn [[c:{e[:12]}]].\n\n"
        f"- Over 3 years [[c:{assumed[:12]}]] it holds.\n\n"
        f"Again, 3 years [[c:{assumed[:12]}]] is assumed.\n")
    reg.submit_draft(LEAD, 1)
    der = json.loads((reg.run_dir / "checks/derivations-v1.r1.json").read_text())
    by = {c["claim_id"]: c for c in der["claims"]}
    assert set(by) == {e, assumed}
    assert by[e]["tier"] == "estimated" and by[assumed]["tier"] == "assumed"
    assert by[e]["sentences"] == [f"The doubled figure is 2.4bn [[c:{e[:12]}]]."]
    assert len(by[assumed]["sentences"]) == 2  # cited twice, both sentences listed
    assert "doubled" not in json.dumps(by[assumed])  # blind to the rest of the draft
    # The estimate's input is checked through its own document, though no draft cites it.
    assert json.loads((reg.run_dir / "checks/doc-r1.r1.json").read_text())["claims"] == [base]
    chk = launched(reg, "claim-checker", "derivations:v1", "s-der")
    with pytest.raises(Refused, match="not started for"):
        reg.record_claim_verdict(chk, base, "holds", "x")
    reg.record_claim_verdict(chk, e, "holds", "the script doubles the input")


def test_every_cited_claim_lands_in_exactly_one_checker_list(reg: Register) -> None:
    from test_store import claim

    a = reg.file_claim(WORKER, claim())["claim_id"]
    b = reg.file_claim(WORKER, claim(statement="Up from 1.0bn.", value={
        "number": 1.0e9, "unit": "USD", "period": "FY2024", "kind": "level",
        "as_written": "1.0bn"}, support=[{"receipt_id": "r2", "quote": "up from 1.0bn"}]))["claim_id"]
    e = _estimate(reg, a)
    assumed = reg.file_claim(WORKER, {"statement": "Assume 3 years.", "tier": "assumed",
                                      "support": [], "branch_id": "b1",
                                      "entities": []})["claim_id"]
    cited = [a, b, e, assumed]
    _cite(reg, 1, *cited)
    lists = reg._check_lists()
    for cid in cited:
        holders = [k for k, ids in lists.items() if cid in ids]
        assert len(holders) == 1, (cid, holders)
    assert not [r for r in checks_api.states(reg.run_dir).values()
                if r.request.prompt.startswith("claim:")]


def test_a_point_named_in_the_record_allows_re_proposal(reg: Register) -> None:
    _proposed(reg)
    reg.record_branch_verdict(reviewer_for(reg, "b1", "s-r1"), "b1", "bound", "x",
                              points=[mat(), mat("second", node="n1")])
    rec = reg.run_dir / "branches/b1/record.md"
    rec.write_text(rec.read_text() + "\nb1.r1.1 resolved: the range is 1.1bn to 1.3bn.\n")
    with pytest.raises(Refused, match="points b1.r1.2 were sent back"):
        reg.propose_branch_done(WORKER, "b1")
    revision(reg, "b1", 1, "b1.r1.2 disputed: the worker holds the series is right")
    reg.propose_branch_done(WORKER, "b1")


def test_new_material_points_on_a_closing_verdict_stay_as_open_limits(reg: Register) -> None:
    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    reg.tree_upsert(LEAD, {"id": "q2", "kind": "question", "statement": "y", "branch_id": "b2"})
    write_record(reg, "b2")
    reg.propose_branch_done(WORKER, "b2")
    reg.record_branch_verdict(reviewer_for(reg, "b2", "s-r1"), "b2", "analyse", "x",
                              points=[mat(node="q2")])
    rec = reg.run_dir / "branches/b2/record.md"
    rec.write_text(rec.read_text() + "\nb2.r1.1 bounded: 1.1bn to 1.3bn.\n")
    reg.propose_branch_done(WORKER, "b2")
    reg.record_branch_verdict(
        reviewer_for(reg, "b2", "s-r2"), "b2", "answered", "bounded now",
        points=[mat("the revision double-counts depot ports", node="q2")],
        resolutions=[{"point_id": "b2.r1.1", "status": "bounded"}])
    limits = reg.gate_status(LEAD, 1)["branch_limits"]
    assert limits == [{"branch_id": "b2", "id": "b2.r2.1",
                       "point": "the revision double-counts depot ports", "node": "q2",
                       "criterion": None}]
    reg.finish_run(LEAD, 1)
    rec_json = json.loads((reg.run_dir / "deliverable/record.json").read_text())
    assert rec_json["branch_limits"] == limits


def test_a_later_version_waits_for_the_previous_round(reg: Register) -> None:
    from test_store import finish_reviews

    ready_run(reg)  # v1's reviews recorded
    (reg.run_dir / "draft/answer.v2.md").write_text("# v2\n")
    reg.submit_draft(LEAD, 2)
    (reg.run_dir / "draft/answer.v3.md").write_text("# v3\n")
    with pytest.raises(Refused, match="reviews of version 2 have not finished"):
        reg.submit_draft(LEAD, 3)
    # A review whose session the usage limit ended is rerun on resume: not finished.
    req = next(r for r in checks_api.pending_checks(reg.run_dir)
               if r.prompt == "draft:2" and r.role == "evidence-auditor")
    checks_api.mark_launched(reg.run_dir, req.request_id, "s-u", "claude-opus-5-5", "high")
    checks_api.mark_ended(reg.run_dir, req.request_id, 1, "rate_limited")
    with pytest.raises(Refused, match="evidence-auditor"):
        reg.submit_draft(LEAD, 3)
    finish_reviews(reg, 2)
    with pytest.raises(Refused, match="evidence-auditor"):
        reg.submit_draft(LEAD, 3)  # still rate_limited: rerun, not finished
    checks_api.mark_launched(reg.run_dir, req.request_id, "s-u2", "claude-opus-5-5", "high",
                             relaunch=True)
    checks_api.mark_ended(reg.run_dir, req.request_id, 0, "completed")
    reg.submit_draft(LEAD, 3)


def test_obligations_follow_a_node_to_another_branch(reg: Register) -> None:
    _proposed(reg)
    reg.record_branch_verdict(reviewer_for(reg, "b1", "s-r1"), "b1", "analyse", "x",
                              points=[mat(node="n1")])
    with pytest.raises(Refused, match="only the lead moves node n1"):
        reg.tree_upsert(WORKER, {"id": "n1", "branch_id": "b2"})
    reg.tree_upsert(LEAD, {"id": "n1", "branch_id": "b2"})
    assert [p["id"] for p in reg.sent_back("b2")] == ["b1.r1.1"]
    write_record(reg, "b2")
    with pytest.raises(Refused, match="points b1.r1.1 were sent back"):
        reg.propose_branch_done(WORKER, "b2")
    revision(reg, "b2", 1, "b1.r1.1 disputed")
    reg.propose_branch_done(WORKER, "b2")
    rev = reviewer_for(reg, "b2", "s-r2")
    with pytest.raises(Refused, match="a branch is sent back once"):
        reg.record_branch_verdict(rev, "b2", "bound", "again", points=[mat(node="n1")])
    with pytest.raises(Refused, match="b1.r1.1 was sent back on this branch"):
        reg.record_branch_verdict(rev, "b2", "answered", "ok")


def test_a_worker_cannot_hide_work_under_a_set_aside_node(reg: Register) -> None:
    reg.tree_upsert(LEAD, {"id": "q", "kind": "question", "statement": "x",
                           "set_aside": "moot"})
    with pytest.raises(Refused, match="under a node set aside"):
        reg.tree_upsert(WORKER, {"id": "w1", "kind": "question", "statement": "y",
                                 "parent": "q", "branch_id": "b1"})
    reg.tree_upsert(WORKER, {"id": "w2", "kind": "question", "statement": "z"})
    with pytest.raises(Refused, match="only the lead moves node w2"):
        reg.tree_upsert(WORKER, {"id": "w2", "parent": "q"})


# --- Codex review #3: the criteria are frozen before any review -----------------------

def test_criteria_freeze_at_the_first_review_and_reviews_bind_them(reg: Register) -> None:
    cid = ready_run(reg)  # proposing b1 froze the criteria
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    frozen = reg.frozen_criteria()
    assert frozen is not None and frozen["round"] == 1
    assert (reg.run_dir / frozen["file"]).read_text() == C
    # A hand edit after freezing blocks the release.
    (reg.run_dir / "criteria.md").write_text("- C1: Something nobody reviewed.\n")
    assert any("differs from the frozen criteria" in f for f in reg.finish_failures(1))
    (reg.run_dir / "criteria.md").write_text(C)
    assert reg.finish_failures(1) == []
    # A revision by the lead is allowed, and the reviews made before it stop counting.
    with pytest.raises(Refused, match="do not revise the criteria"):
        reg.revise_criteria(WORKER, "- C1: x\n", "why")
    with pytest.raises(Refused, match="states its reason"):
        reg.revise_criteria(LEAD, "- C1: x\n", " ")
    with pytest.raises(Refused, match="name no criterion"):
        reg.revise_criteria(LEAD, "nothing here\n", "why")
    out = reg.revise_criteria(LEAD, C + "- C3: Names its sources.\n", "the critique asked")
    assert out["round"] == 2 and "C3" in out["criteria"]
    unmet = reg.finish_failures(1)
    assert len([f for f in unmet if "judged criteria since revised" in f]) == 3


def test_the_users_criteria_are_never_revised(reg: Register) -> None:
    (reg.run_dir / "task").mkdir()
    (reg.run_dir / "task/task.json").write_text(json.dumps({"question": "q",
                                                            "criteria": ["a"]}))
    with pytest.raises(Refused, match="the user's criteria"):
        reg.revise_criteria(LEAD, "- C1: b\n", "why")


def test_no_review_starts_without_criteria(reg: Register) -> None:
    (reg.run_dir / "criteria.md").unlink()
    reg.tree_upsert(LEAD, {"id": "n1", "kind": "question", "statement": "x", "branch_id": "b1"})
    write_record(reg, "b1")
    with pytest.raises(Refused, match="write it at intake, before any review"):
        reg.propose_branch_done(WORKER, "b1")


# --- The sealed release ------------------------------------------------------------

def test_finish_run_seals_the_run_and_a_changed_byte_fails_the_check(reg: Register) -> None:
    from stemma.register import manifest

    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    out = reg.finish_run(LEAD, 1)
    assert out["manifest"] == "release/manifest.json"
    assert manifest.check(reg.run_dir, out["manifest_sha256"]) == []
    sealed = [e for e in jsonl.read_all(reg.run_dir / "events.jsonl")
              if e.get("event") == "release_sealed"]
    assert sealed[0]["manifest_sha256"] == out["manifest_sha256"]
    rec = reg.run_dir / "branches/b1/record.md"
    rec.write_bytes(rec.read_bytes()[:-1] + b"y")
    assert manifest.check(reg.run_dir, out["manifest_sha256"])


def test_a_run_holding_a_link_is_not_released(reg: Register) -> None:
    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    (reg.run_dir / "analysis").mkdir(exist_ok=True)
    (reg.run_dir / "analysis/elsewhere.py").symlink_to("/etc/hosts")
    with pytest.raises(Refused, match="could not be sealed"):
        reg.finish_run(LEAD, 1)
    assert reg.release_state() == "withdrawn"


# --- Codex review #6: a crash between a verdict's writes loses nothing ----------------

class _Crash(Exception):
    pass


@pytest.mark.parametrize("cut", ["node_fact", "node_status"])
def test_a_crash_inside_a_send_back_keeps_the_send_back(reg: Register, cut: str,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    _proposed(reg)
    rev = reviewer_for(reg, "b1", "s-r1")
    real_fact, real_put = reg.wm.assertFact, reg._put_node

    def fact(inp: Any) -> Any:
        if cut == "node_fact" and inp.get("predicate") == "branch_verdict":
            raise _Crash
        return real_fact(inp)

    def put(node: dict[str, Any], source: dict[str, Any]) -> None:
        if cut == "node_status" and node.get("status") == "in_progress":
            raise _Crash
        real_put(node, source)

    monkeypatch.setattr(reg.wm, "assertFact", fact)
    monkeypatch.setattr(reg, "_put_node", put)
    with pytest.raises(_Crash):
        reg.record_branch_verdict(rev, "b1", "bound", "unbounded", points=[mat(node="n1")])
    monkeypatch.undo()
    # The next writer repairs the node from the authoritative record.
    fresh = Register(reg.run_dir, evidence=FakeEvidence())
    fresh.gate_status(LEAD, 1) if fresh.submitted_versions() else fresh.tree_upsert(
        LEAD, {"id": "n1", "doubt": None})
    assert [n["status"] for n in fresh.tree()] == ["in_progress"]
    assert [p["id"] for p in fresh.sent_back("b1")] == ["b1.r1.1"]
    write_record(fresh, "b1")
    with pytest.raises(Refused, match="were sent back"):
        fresh.propose_branch_done(WORKER, "b1")
    revision(fresh, "b1", 1, "b1.r1.1")
    fresh.propose_branch_done(WORKER, "b1")
    with pytest.raises(Refused, match="a branch is sent back once"):
        fresh.record_branch_verdict(reviewer_for(fresh, "b1", "s-r2"), "b1", "bound", "again",
                                    points=[mat(node="n1")])


# --- A read-only register for viewers and verify -----------------------------------

def test_a_read_only_register_reads_a_sealed_run_and_writes_nothing(reg: Register) -> None:
    import os
    import stat

    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    out = reg.finish_run(LEAD, 1)

    def listing() -> set[tuple[str, int]]:
        return {(str(p.relative_to(reg.run_dir)), p.stat().st_mtime_ns)
                for p in reg.run_dir.rglob("*") if p.is_file()}

    before = listing()
    folders = [p for p in reg.run_dir.rglob("*") if p.is_dir()] + [reg.run_dir]
    modes = {p: p.stat().st_mode for p in folders}
    for p in folders:
        os.chmod(p, stat.S_IRUSR | stat.S_IXUSR)  # nothing in the run may be written
    try:
        ro = Register.read_only(reg.run_dir)
        assert ro.get_claim(cid)["verdict"]["verdict"] == "holds"
        assert ro.finish_failures(1) == []
        assert set(ro.recorded_reviews(1)) == {"stemma:deciders-eye-reviewer",
                                               "stemma:evidence-auditor",
                                               "stemma:mechanical-reader"}
        assert ro.run_facts("finished")[0]["version"] == 1
        assert ro.release_state() == "released"
        for attempt in (lambda: ro.tree_upsert(LEAD, {"id": "x", "kind": "question",
                                                      "statement": "x"}),
                        lambda: ro.finish_run(LEAD, 1), lambda: ro.rebuild_index()):
            with pytest.raises(Refused, match="open read-only"):
                attempt()
    finally:
        for p, m in modes.items():
            os.chmod(p, m)
    assert listing() == before
    from stemma.register import manifest

    assert manifest.check(reg.run_dir, out["manifest_sha256"]) == []



# --- Codex re-check: criteria bound at the request, sent-back parents, orphans ---------

def test_a_review_judges_the_criteria_it_was_asked_to(reg: Register) -> None:
    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    (reg.run_dir / "draft/answer.v2.md").write_text(
        f"# v2\n\nRevenue was 1.2bn [[c:{cid[:12]}]].\n")
    reg.submit_draft(LEAD, 2)  # reviews asked against criteria A
    reg.revise_criteria(LEAD, C + "- C3: Names its sources.\n", "the critique asked")  # B
    for i, role in enumerate(("deciders-eye-reviewer", "evidence-auditor", "mechanical-reader")):
        reg.record_review(launched(reg, role, "draft:2", f"s-c{i}"), 2, "ok", [])
    unmet = reg.finish_failures(2)
    assert len([f for f in unmet if "judged criteria since revised" in f]) == 3


def test_a_sent_back_question_does_not_close_through_children(reg: Register) -> None:
    _proposed(reg)
    reg.record_branch_verdict(reviewer_for(reg, "b1", "s-r1"), "b1", "analyse", "x",
                              points=[mat(node="n1")])
    with pytest.raises(Refused, match="only the lead adds an open lane"):
        reg.tree_upsert(WORKER, {"id": "ol", "kind": "open_lane", "statement": "later",
                                 "parent": "n1"})
    # Even a child the lead adds does not close the point sent back on its parent.
    reg.tree_upsert(LEAD, {"id": "ol", "kind": "open_lane", "statement": "later",
                           "parent": "n1"})
    view = {n["id"]: n for n in reg.tree_view()}
    assert view["n1"]["closes_by"] == "children"
    assert any("node n1: point b1.r1.1 sent back on it has no resolution" in f
               for f in reg.finish_failures())


def test_an_orphan_assignment_left_by_a_crash_does_not_block_retries(
        reg: Register, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_store import claim

    a = reg.file_claim(WORKER, claim())["claim_id"]
    real = checks_api.request_check

    def crash(*_a: Any, **_k: Any) -> Any:
        raise _Crash

    monkeypatch.setattr(checks_api, "request_check", crash)
    with pytest.raises(_Crash):
        _cite(reg, 1, a)
    monkeypatch.setattr(checks_api, "request_check", real)
    assert (reg.run_dir / "checks/doc-r1.r1.json").exists()  # the orphan
    _cite(reg, 1, a)
    assert [r.prompt for r in checks_api.pending_checks(reg.run_dir)
            if r.role == "claim-checker"] == ["doc:r1"]
    chk = launched(reg, "claim-checker", "doc:r1", "s-d1")
    assert reg.check_assignment(chk)["claims"] == [a]
