"""Contracts v0.3 (from the walking-skeleton run, which never released): finish_run shows
the whole gate; leaves close by branch verdict, parents derive; review rounds are a
recorded resource limit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_store import (
    LEAD,
    WORKER,
    FakeEvidence,
    checker_for,
    frames_and_reviews,
    ready_run,
    reviewer_for,
    write_record,
)

from stemma.register.filing import Refused
from stemma.register.store import Register, closure


@pytest.fixture
def reg(tmp_path: Path) -> Register:
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "criteria.md").write_text("- C1: The answer says whether the target is met.\n")
    return Register(tmp_path / "run", evidence=FakeEvidence())


def node(nid: str, kind: str, parent: str | None = None, status: str = "open",
         branch_id: str | None = None) -> dict[str, Any]:
    return {"id": nid, "kind": kind, "parent": parent, "status": status,
            "branch_id": branch_id, "statement": nid}


# --- 1. finish_run returns the whole gate, readable by an agent --------------------

def test_finish_run_refusal_lists_every_unmet_condition_numbered(reg: Register) -> None:
    reg.tree_upsert(LEAD, {"id": "T3", "kind": "question", "statement": "x"})
    reg.tree_upsert(LEAD, {"id": "R1", "kind": "question", "statement": "y", "branch_id": "b1"})
    with pytest.raises(Refused) as exc:
        reg.finish_run(LEAD, 1)  # nothing submitted: still the whole list
    msg = str(exc.value)
    expected = reg.finish_failures(1)
    assert msg.startswith(f"Refused: finish_run: {len(expected)} unmet conditions:\n")
    lines = msg.splitlines()[1:]
    assert lines == [f"{i}. {f}" for i, f in enumerate(expected, start=1)]
    assert "1. node R1 is open (question, branch b1)" in lines
    assert "2. node T3 is open (question, in no branch; only a branch verdict closes it)" \
        in lines
    assert any("critique is not recorded" in line for line in lines)
    assert any("version 1 was not submitted" in line for line in lines)


# --- 2. Leaves close by branch verdict; parents derive; an empty open lane needs nothing

def test_closure_leaves_parents_and_open_lanes() -> None:
    nodes = [node("O1", "question"),  # no children and no branch: a leaf nobody holds
             node("O2", "question"), node("C2", "question", "O2", "answered", "b1"),
             node("O3", "question"), node("C3", "question", "O3", "open", "b2"),
             node("T", "question", status="open"),  # a parent: its stored status is ignored
             node("T1", "question", "T", "answered", "b1"),
             node("T2", "question", "T", "gap_accepted", "b1"),
             node("OL", "open_lane"),  # empty
             node("OL2", "open_lane", branch_id="b9"),  # holds work: a leaf
             node("T3", "question")]  # a leaf in no branch
    c = closure(nodes)
    assert c["O1"] == {"closes_by": "branch", "status": "open", "closed": False}
    assert c["O2"]["closed"] and c["O2"]["status"] == "answered"
    assert not c["O3"]["closed"] and c["O3"]["status"] == "open"
    assert c["T"] == {"closes_by": "children", "status": "gap_accepted", "closed": True}
    assert c["OL"] == {"closes_by": "nothing", "status": "not_needed", "closed": True}
    assert c["OL2"] == {"closes_by": "branch", "status": "open", "closed": False}
    assert c["T3"] == {"closes_by": "branch", "status": "open", "closed": False}


def test_parent_status_is_derived_never_the_stored_one(reg: Register) -> None:
    reg.tree_upsert(LEAD, {"id": "O1", "kind": "question", "statement": "carry on"})
    reg.tree_upsert(LEAD, {"id": "OL", "kind": "open_lane", "statement": "unplanned"})
    reg.tree_upsert(LEAD, {"id": "n1", "kind": "question", "statement": "x",
                           "branch_id": "b1", "parent": "O1"})
    cid = ready_run(reg)  # n1 answered by its branch verdict
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    # The option and the empty open lane are stored open; neither blocks the release.
    view = {n["id"]: n for n in reg.tree_view()}
    assert view["O1"]["stored_status"] == "open" and view["O1"]["status"] == "answered"
    assert view["OL"]["status"] == "not_needed"
    tree = json.loads((reg.run_dir / "tree.json").read_text())
    assert {n["id"]: n["closes_by"] for n in tree["nodes"]} == {
        "O1": "children", "OL": "nothing", "n1": "branch"}
    out = reg.finish_run(LEAD, 1)
    assert out["finished"]


def test_a_parent_waits_on_an_open_child_and_names_only_the_leaf(reg: Register) -> None:
    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    reg.tree_upsert(LEAD, {"id": "O1", "kind": "question", "statement": "act now"})
    reg.tree_upsert(LEAD, {"id": "T3", "kind": "question", "statement": "pace holds",
                           "parent": "O1"})
    failures = reg.finish_failures(1)
    assert [f for f in failures if f.startswith("node ")] == [
        "node T3 is open (question, in no branch; only a branch verdict closes it)"]
    # Filed as a branch and closed by its reviewer, the leaf closes and so does O1.
    reg.tree_upsert(LEAD, {"id": "T3", "branch_id": "b3"})
    write_record(reg, "b3")
    reg.propose_branch_done(WORKER, "b3")
    reg.record_branch_verdict(reviewer_for(reg, "b3", "s-rev3"), "b3", "accept_gap",
                              "not researched: out of scope", gap_type="out_of_scope")
    assert not [f for f in reg.finish_failures(1) if f.startswith("node ")]


# --- 3. Review rounds are a recorded resource limit --------------------------------

def write_limits(reg: Register, limits: dict[str, Any]) -> None:
    reg.run_dir.mkdir(parents=True, exist_ok=True)
    (reg.run_dir / "run.json").write_text(json.dumps({"limits": limits}))


def submit(reg: Register, version: int, cid: str) -> None:
    (reg.run_dir / f"draft/answer.v{version}.md").write_text(
        f"# Answer\n\nRevenue was 1.2bn [[c:{cid[:12]}]] in the year to 2025 "
        f"[[nc:period]].\n")
    reg.submit_draft(LEAD, version)
    frames_and_reviews(reg, version)


def test_after_the_last_round_submit_refuses_and_release_lists_its_criticisms(
        reg: Register) -> None:
    write_limits(reg, {"max_review_rounds": 2})
    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    submit(reg, 2, cid)
    (reg.run_dir / "draft/answer.v3.md").write_text("# v3\n")
    with pytest.raises(Refused, match="review-round limit is reached .2 rounds"):
        reg.submit_draft(LEAD, 3)
    out = reg.finish_run(LEAD, 2)
    assert out["stopped_by_limit"] == ["review_rounds"]
    text = (reg.run_dir / out["completion"]).read_text()
    assert "stopped_by_limit" in text and "2 of 2" in text
    for f in ("deciders-eye.md", "evidence-audit.md", "mechanical.md"):
        review = (reg.run_dir / "reviews/v2" / f).read_text().strip()
        assert review in text
    facts = reg._run_facts("stopped_by_limit")
    assert facts == [{"what": "review_rounds", "limit": 2, "version": 2}]


def test_within_the_limit_no_completion_statement_is_written(reg: Register) -> None:
    cid = ready_run(reg)  # no run.json: the default, 3 rounds
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    assert reg.max_review_rounds() == 3
    out = reg.finish_run(LEAD, 1)
    assert "completion" not in out
    assert not (reg.run_dir / "deliverable/completion.md").exists()


@pytest.mark.parametrize("bad", [0, -1, True, "3", 2.5])
def test_a_bad_review_round_limit_is_loud(reg: Register, bad: Any) -> None:
    write_limits(reg, {"max_review_rounds": bad})
    with pytest.raises(Refused, match="max_review_rounds is not a positive whole number"):
        reg.max_review_rounds()


def test_the_driver_records_the_limit_in_run_json() -> None:
    from stemma.driver.config import Limits

    assert Limits().to_json()["max_review_rounds"] == 3
    assert Limits.from_json({**Limits().to_json(), "max_review_rounds": 5}).max_review_rounds == 5


