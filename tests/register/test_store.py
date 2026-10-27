"""The claim register, the tree and the release gate, including hostile callers."""

from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path
from typing import Any

import pytest

from stemma.register import checks_api, jsonl
from stemma.register.evidence_port import QuoteCheckFailed
from stemma.register.filing import Refused
from stemma.register.store import RECORD_HEADINGS, Caller, Register

TEXT = "Acme Widgets reported revenue of 1.2bn in fiscal 2025, up from 1.0bn."
LEAD = Caller("stemma:lead", None, "s-lead")
WORKER = Caller("stemma:worker", "a1", "s-lead")


class FakeEvidence:
    def check_quote(self, run_dir: Path, receipt_id: str, quote: str) -> bool:
        if receipt_id not in ("r1", "r2"):
            raise QuoteCheckFailed(f"unknown receipt {receipt_id}")
        return quote in TEXT

    def read_text(self, run_dir: Path, receipt_id: str) -> str:
        return TEXT


def claim(**over: Any) -> dict[str, Any]:
    c: dict[str, Any] = {
        "statement": "Acme Widgets' fiscal 2025 revenue was 1.2bn.",
        "value": {"number": 1.2e9, "unit": "USD", "period": "FY2025", "kind": "level",
                  "as_written": "1.2bn"},
        "tier": "observed",
        "support": [{"receipt_id": "r1", "quote": "revenue of 1.2bn in fiscal 2025"}],
        "branch_id": "b1",
        "entities": ["Acme Widgets"],
    }
    c.update(over)
    return c


@pytest.fixture
def reg(tmp_path: Path) -> Register:
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "criteria.md").write_text("- C1: The answer says whether the target is met.\n")
    return Register(tmp_path / "run", evidence=FakeEvidence())


def _lists(reg: Register, cid: str, prompt: str, rnd: int) -> bool:
    if prompt == f"claim:{cid}":
        return True
    return prompt.startswith("doc:") and cid in reg._doc_lists().get((prompt[4:], rnd), [])


def checker_for(reg: Register, cid: str, session: str = "s-check") -> Caller:
    """The checker the driver would launch for the check that lists `cid` (D7), asking for
    that check first if no submitted draft has."""
    reg.wm.refresh()
    if not any(_lists(reg, cid, r.prompt, r.round) for r in checks_api.pending_checks(reg.run_dir)):
        from stemma.register import jsonl as _j

        with _j.exclusive(reg._lock):
            reg.request_claim_checks([cid], "test")
    req = next(r for r in checks_api.pending_checks(reg.run_dir)
               if _lists(reg, cid, r.prompt, r.round))
    checks_api.mark_launched(reg.run_dir, req.request_id, session, "claude-opus-5-5", "high")
    checks_api.mark_verified(reg.run_dir, session, "claude-opus-5-5", "high")  # as the driver
    return Caller("stemma:claim-checker", None, session)


def test_file_claim_records_author_from_caller_and_requests_no_check(reg: Register) -> None:
    out = reg.file_claim(WORKER, claim())
    rec = reg.get_claim(out["claim_id"][:12])
    assert rec["author"] == "stemma:worker"
    assert rec["verdict"] is None
    assert checks_api.pending_checks(reg.run_dir) == []  # D7: nothing is checked on filing


def test_refiling_is_idempotent(reg: Register) -> None:
    a = reg.file_claim(WORKER, claim())
    log = (reg.dir / "log.jsonl").read_bytes()
    b = reg.file_claim(LEAD, claim())
    assert (a["claim_id"], b["already_filed"]) == (b["claim_id"], True)
    assert (reg.dir / "log.jsonl").read_bytes() == log  # nothing appended


@pytest.mark.parametrize("who", ["stemma:claim-checker", "stemma:branch-reviewer",
                                 "stemma:blind-framer", "stemma:deciders-eye-reviewer", ""])
def test_non_authors_cannot_file(reg: Register, who: str) -> None:
    with pytest.raises(Refused, match="do not file claims"):
        reg.file_claim(Caller(who, None, "s"), claim())


def test_failed_check_files_nothing(reg: Register) -> None:
    with pytest.raises(Refused, match="quotation is not in receipt r1"):
        reg.file_claim(WORKER, claim(support=[{"receipt_id": "r1", "quote": "revenue of 9bn"}]))
    assert reg.find_claims() == []
    assert checks_api.pending_checks(reg.run_dir) == []


def test_only_the_checker_started_for_this_claim_records_its_verdict(reg: Register) -> None:
    cid = reg.file_claim(WORKER, claim())["claim_id"]
    other = reg.file_claim(WORKER, claim(branch_id="b2"))["claim_id"]
    for who in (LEAD, WORKER, Caller("stemma:branch-reviewer", None, "s-check")):
        with pytest.raises(Refused, match="do not record claim verdicts"):
            reg.record_claim_verdict(who, cid, "holds", "quote matches")
    # A checker session that was not started for this claim (or not started at all).
    with pytest.raises(Refused, match="not started for"):
        reg.record_claim_verdict(Caller("stemma:claim-checker", None, "s-x"), cid, "holds", "x")
    checker_other = checker_for(reg, other, "s-other")
    with pytest.raises(Refused, match="not started for"):
        reg.record_claim_verdict(checker_other, cid, "holds", "x")
    checker = checker_for(reg, cid)
    with pytest.raises(Refused, match="carries its evidence"):
        reg.record_claim_verdict(checker, cid, "holds", "  ")
    with pytest.raises(Refused, match="verdict must be one of"):
        reg.record_claim_verdict(checker, cid, "approved", "x")
    reg.record_claim_verdict(checker, cid, "holds", "the passage says 1.2bn for FY2025")
    assert reg.get_claim(cid)["verdict"]["verdict"] == "holds"


def test_correction_supersedes_and_never_deletes(reg: Register) -> None:
    cid = reg.file_claim(WORKER, claim())["claim_id"]
    checker = checker_for(reg, cid)
    corrected = claim(statement="Acme Widgets' fiscal 2025 revenue was 1.2bn (reported).")
    out = reg.record_claim_verdict(checker, cid, "holds_with_correction", "wording", corrected)
    new = out["corrected_claim_id"]
    old_rec, new_rec = reg.get_claim(cid), reg.get_claim(new)
    assert old_rec["superseded_by"] == new
    assert old_rec["verdict"]["verdict"] == "holds_with_correction"
    assert new_rec["supersedes"] == cid
    assert new_rec["verdict"]["verdict"] == "holds" and new_rec["verdict"]["at_filing"]
    assert new_rec["author"] == "stemma:claim-checker"
    # The old lineage edge is closed, not deleted.
    rels = {e["payload"]["id"] for e in reg.wm.events() if e["kind"] == "relate"}
    assert len(rels) == 2  # the old edge and the new one; closing re-emits the old edge
    edges = reg.wm.view("graph").edges(f"lineage:stemma:{cid}",
                                       {"via": "current", "includeClosed": True})
    closed = [e for e in edges if e.get("validTo")]
    assert len(closed) == 1 and closed[0]["to"] == f"claim:stemma:{cid}"
    assert [c["claim_id"] for c in reg.find_claims()] == [new]
    with pytest.raises(Refused, match="already superseded"):
        reg.record_claim_verdict(checker, cid, "holds", "again")


def test_correction_must_pass_filing_checks(reg: Register) -> None:
    cid = reg.file_claim(WORKER, claim())["claim_id"]
    checker = checker_for(reg, cid)
    bad = claim(support=[{"receipt_id": "r1", "quote": "not in the text"}])
    with pytest.raises(Refused, match="quotation is not in"):
        reg.record_claim_verdict(checker, cid, "holds_with_correction", "x", bad)
    assert reg.get_claim(cid)["superseded_by"] is None


def test_echo_candidates_list_claims_on_one_document(reg: Register) -> None:
    a = reg.file_claim(WORKER, claim())["claim_id"]
    b = reg.file_claim(WORKER, claim(branch_id="b2"))["claim_id"]
    reg.file_claim(WORKER, claim(branch_id="b3", support=[
        {"receipt_id": "r2", "quote": "revenue of 1.2bn in fiscal 2025"}]))
    assert reg.echo_candidates() == [{"document": "receipt:r1", "claim_ids": sorted([a, b])}]


def test_entities_go_through_the_review_queue(reg: Register) -> None:
    reg.file_claim(WORKER, claim(entities=["Northwind Traders"]))
    reg.file_claim(WORKER, claim(branch_id="b2", entities=["Northwind Trading"]))  # 0.824
    pending = reg.entity_review(WORKER, "list")["pending"]
    assert len(pending) == 1
    with pytest.raises(Refused, match="do not decide entity reviews"):
        reg.entity_review(WORKER, "approve", proposal_id=pending[0]["id"], reason="same")
    reg.entity_review(LEAD, "approve", proposal_id=pending[0]["id"], reason="same firm")
    assert reg.entity_review(LEAD, "list")["pending"] == []


def test_parent_mapping_needs_two_sources(reg: Register) -> None:
    one = [{"receipt_id": "r1", "quote": "Acme Widgets"}]
    with pytest.raises(Refused, match="two sources"):
        reg.entity_review(LEAD, "parent", name="Acme Widgets", parent="Acme Holdings",
                          support=one)
    with pytest.raises(Refused, match="two sources"):
        reg.entity_review(LEAD, "parent", name="Acme Widgets", parent="Acme Holdings",
                          support=one + one)
    two = one + [{"receipt_id": "r2", "quote": "Acme Widgets reported"}]
    out = reg.entity_review(LEAD, "parent", name="Acme Widgets", parent="Acme Holdings",
                            support=two)
    assert out["relationship"]["type"] == "parent"


# --- Tree ---------------------------------------------------------------------------


def write_record(reg: Register, branch: str) -> None:
    d = reg.run_dir / "branches" / branch
    d.mkdir(parents=True, exist_ok=True)
    (d / "record.md").write_text("\n".join(f"## {h}\nx\n" for h in RECORD_HEADINGS))


def reviewer_for(reg: Register, branch: str, session: str = "s-rev") -> Caller:
    req = [r for r in checks_api.pending_checks(reg.run_dir)
           if r.prompt == f"branch:{branch}"][-1]
    checks_api.mark_launched(reg.run_dir, req.request_id, session, "claude-opus-5-5", "high")
    checks_api.mark_verified(reg.run_dir, session, "claude-opus-5-5", "high")  # as the driver
    return Caller("stemma:branch-reviewer", None, session)


def test_branch_reaches_answered_only_by_a_verdict(reg: Register) -> None:
    reg.tree_upsert(LEAD, {"id": "n1", "kind": "question", "statement": "Demand holds",
                           "branch_id": "b1", "crux": True})
    for status in ("answered", "gap_accepted", "proposed_done", "stopped_by_limit"):
        with pytest.raises(Refused, match="not set by tree_upsert"):
            reg.tree_upsert(LEAD, {"id": "n1", "status": status})
    reg.tree_upsert(LEAD, {"id": "n1", "status": "in_progress"})
    with pytest.raises(Refused, match="has no folder"):
        reg.propose_branch_done(WORKER, "b1")
    (reg.run_dir / "branches/b1").mkdir(parents=True)
    with pytest.raises(Refused, match="no record.md"):
        reg.propose_branch_done(WORKER, "b1")
    write_record(reg, "b1")
    reg.propose_branch_done(WORKER, "b1")
    assert reg.tree()[0]["status"] == "proposed_done"
    with pytest.raises(Refused, match="do not record branch verdicts"):
        reg.record_branch_verdict(LEAD, "b1", "answered", "looks done")
    rev = reviewer_for(reg, "b1")
    with pytest.raises(Refused, match="names the method"):
        reg.record_branch_verdict(rev, "b1", "find_more", "thin")
    reg.record_branch_verdict(rev, "b1", "find_more", "thin", method="primary filings",
                              points=[{"point": "thin", "material": True, "criterion": "C1"}])
    assert reg.tree()[0]["status"] == "in_progress"
    # Second round: a new check request, and the old reviewer session cannot answer it.
    with pytest.raises(Refused, match="no revision order"):
        reg.propose_branch_done(WORKER, "b1")
    (reg.run_dir / "branches/b1/revision-1.md").write_text(
        "# Finding\nb1.r1.1 thin\n# Raised by\nbranch reviewer\n# Is it right?\nyes\n# Fix\nmore filings\n")
    reg.propose_branch_done(WORKER, "b1")
    with pytest.raises(Refused, match="not started for"):
        reg.record_branch_verdict(Caller("stemma:branch-reviewer", None, "s-z"), "b1",
                                  "answered", "x")
    rev2 = reviewer_for(reg, "b1", "s-rev2")
    with pytest.raises(Refused, match="b1.r1.1 was sent back"):
        reg.record_branch_verdict(rev2, "b1", "answered", "the record carries the weight")
    reg.record_branch_verdict(rev2, "b1", "answered", "the record carries the weight",
                              resolutions=[{"point_id": "b1.r1.1", "status": "resolved"}])
    assert reg.tree()[0]["status"] == "answered"
    files = sorted((reg.run_dir / "branches/b1/verdicts").glob("*.json"))
    assert [json.loads(f.read_text())["verdict"] for f in files] == ["find_more", "answered"]
    tree = json.loads((reg.run_dir / "tree.json").read_text())
    assert tree["nodes"][0]["status"] == "answered"


def test_record_without_contract_headings_refused(reg: Register) -> None:
    reg.tree_upsert(LEAD, {"id": "n1", "kind": "question", "statement": "x", "branch_id": "b1"})
    d = reg.run_dir / "branches/b1"
    d.mkdir(parents=True)
    (d / "record.md").write_text("## Conclusion and confidence\nDone.\n")
    with pytest.raises(Refused, match="lacks the heading 'Claims \\(ids\\)'"):
        reg.propose_branch_done(WORKER, "b1")


@pytest.mark.parametrize("node, msg", [
    ({"id": "../x", "kind": "question", "statement": "x"}, "plain id"),
    ({"id": "n2", "kind": "topic", "statement": "x"}, "kind must be one of"),
    ({"id": "n2", "kind": "question", "statement": ""}, "needs a statement"),
    ({"id": "n2", "kind": "question", "statement": "x", "parent": "zz"}, "not a node"),
    ({"id": "n2", "kind": "question", "statement": "x", "verdict": "answered"}, "unknown"),
])
def test_hostile_tree_changes_refused(reg: Register, node: dict[str, Any], msg: str) -> None:
    with pytest.raises(Refused, match=msg):
        reg.tree_upsert(LEAD, node)


# --- Release gate -------------------------------------------------------------------


def launched(reg: Register, role: str, prompt: str, session: str) -> Caller:
    """Mark the latest request for (role, prompt) launched as `session`, as the driver does."""
    req = [r for r in checks_api.pending_checks(reg.run_dir)
           if r.role == role and r.prompt == prompt][-1]
    checks_api.mark_launched(reg.run_dir, req.request_id, session, "claude-opus-5-5", "high")
    checks_api.mark_verified(reg.run_dir, session, "claude-opus-5-5", "high")  # as the driver
    return Caller(f"stemma:{role}", None, session)


def frames_and_reviews(reg: Register, version: int = 1) -> None:
    """The framer's frame and critique, and the three reviews, through their tools."""
    if not reg._recorded_frames("own"):
        checks_api.request_check(reg.run_dir, "blind-framer", "frame:own", "run_start")
        reg.record_frame(launched(reg, "blind-framer", "frame:own", "s-f1"), "own", "own")
        (reg.run_dir / "frame.md").write_text("frame")
        reg.request_frame_critique(LEAD)
        reg.record_frame(launched(reg, "blind-framer", "frame:critique", "s-f2"), "critique",
                         "critique")
        (reg.run_dir / "framer/lead-response.md").write_text("response")
    for i, role in enumerate(("deciders-eye-reviewer", "evidence-auditor",
                              "mechanical-reader")):
        who = launched(reg, role, f"draft:{version}", f"s-r{version}-{i}")
        reg.record_review(who, version, f"review by {role}", [])


def ready_run(reg: Register, body: str | None = None) -> str:
    reg.tree_upsert(LEAD, {"id": "n1", "kind": "question", "statement": "x", "branch_id": "b1"})
    write_record(reg, "b1")
    reg.propose_branch_done(WORKER, "b1")
    reg.record_branch_verdict(reviewer_for(reg, "b1"), "b1", "answered", "done")
    cid = reg.file_claim(WORKER, claim())["claim_id"]
    (reg.run_dir / "draft").mkdir()
    (reg.run_dir / "draft/answer.v1.md").write_text(body if body is not None else (
        f"# Answer\n\nRevenue was 1.2bn [[c:{cid[:12]}]] in the year to 2025 [[nc:period]].\n"))
    reg.submit_draft(LEAD, 1)
    frames_and_reviews(reg, 1)
    return cid


def test_finish_run_refused_until_a_verdict_exists(reg: Register) -> None:
    cid = ready_run(reg)
    with pytest.raises(Refused, match=f"claim {cid[:12]} has verdict none"):
        reg.finish_run(LEAD, 1)
    assert not (reg.run_dir / "deliverable/answer.md").exists()
    reg.record_claim_verdict(checker_for(reg, cid), cid, "could_not_check", "source offline")
    out = reg.finish_run(LEAD, 1)
    assert out["finished"]
    released = (reg.run_dir / "deliverable/answer.md").read_bytes()
    assert released == (reg.run_dir / "register/drafts/answer.v1.md").read_bytes()
    reader = (reg.run_dir / "deliverable/answer.reader.md").read_text()
    assert "[1, not checked]" in reader and "[[" not in reader
    assert "(not checked)" in reader


def test_finish_run_refuses_not_supported_and_untagged(reg: Register) -> None:
    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid), cid, "not_supported", "wrong period")
    with pytest.raises(Refused, match="has verdict not_supported"):
        reg.finish_run(LEAD, 1)
    (reg.run_dir / "draft/answer.v2.md").write_text("Sales were 40 units.\n")
    reg.submit_draft(LEAD, 2)
    frames_and_reviews(reg, 2)
    with pytest.raises(Refused, match="untagged number"):
        reg.finish_run(LEAD, 2)


def test_finish_run_lists_every_missing_producer(reg: Register) -> None:
    with pytest.raises(Refused) as e:
        reg.finish_run(LEAD, 1)
    msg = str(e.value)
    for part in ("the tree has no nodes", "critique is not recorded",
                 "framer/lead-response.md", "version 1 was not submitted"):
        assert part in msg


def test_finish_run_only_by_the_lead(reg: Register) -> None:
    with pytest.raises(Refused, match="do not finish the run"):
        reg.finish_run(WORKER, 1)


def test_await_checks_never_invents(reg: Register) -> None:
    cid = reg.file_claim(WORKER, claim())["claim_id"]
    out = reg.await_checks([cid[:12]], 0)
    assert out["landed"] == {}
    assert out["not_landed"][0]["status"] == "not_requested"  # no draft cites it yet
    checker = checker_for(reg, cid)
    waiting = reg.await_checks([cid[:12]], 0)["not_landed"]
    assert [(w["status"], w["role"]) for w in waiting] == [("running", "claim-checker")]
    reg.record_claim_verdict(checker, cid, "holds", "matches")
    out = reg.await_checks([cid, "frame:own"], 0)
    assert out["landed"][f"claim:{cid}"]["verdict"] == "holds"
    assert out["not_landed"] == [{"id": "frame:own", "status": "not_requested"}]


def test_dependents_found_from_tags(reg: Register) -> None:
    cid = reg.file_claim(WORKER, claim())["claim_id"]
    (reg.run_dir / "branches/b1").mkdir(parents=True)
    (reg.run_dir / "branches/b1/record.md").write_text(f"Revenue 1.2bn [[c:{cid[:12]}]]")
    jsonl.append(reg.run_dir / "events.jsonl", {"event": "file_write",
                                                "path": "branches/b1/record.md",
                                                "agent_type": "stemma:worker"})
    assert reg.dependents(cid) == [{"path": "branches/b1/record.md", "owner": "stemma:worker"}]


# --- Two processes writing at once --------------------------------------------------


def _file_many(run_dir: str, worker: int, barrier: Any, n: int) -> None:
    reg = Register(Path(run_dir), evidence=FakeEvidence())
    barrier.wait()
    for i in range(n):
        reg.file_claim(Caller("stemma:worker", f"w{worker}", "s"),
                       claim(branch_id=f"w{worker}-{i}"))
        reg.file_claim(Caller("stemma:worker", f"w{worker}", "s"), claim(branch_id=f"same-{i}"))


def test_two_processes_filing_at_once(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    Register(run_dir, evidence=FakeEvidence())
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(2)
    n = 25  # enough claims that the two writers interleave
    procs = [ctx.Process(target=_file_many, args=(str(run_dir), w, barrier, n)) for w in (0, 1)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    reg = Register(run_dir, evidence=FakeEvidence())
    branches = sorted(c["branch_id"] for c in reg.find_claims())
    # Each process's own claims, plus one copy of each claim both filed.
    expected = sorted([f"w{w}-{i}" for w in (0, 1) for i in range(n)] +
                      [f"same-{i}" for i in range(n)])
    assert branches == expected
    assert checks_api.states(run_dir) == {}  # D7: filing starts no check
    seqs = [e["seq"] for e in reg.wm.events()]
    assert seqs == sorted(set(seqs))


# --- Contract v0.2.4 ----------------------------------------------------------------


def test_checker_subagent_inside_a_launched_session_is_refused(reg: Register) -> None:
    cid = reg.file_claim(WORKER, claim())["claim_id"]
    checker_for(reg, cid, "s-check")
    # Same session id as the launched checker, but a subagent (agent_id set).
    sub = Caller("stemma:claim-checker", "a-sub", "s-check")
    with pytest.raises(Refused, match="not started for"):
        reg.record_claim_verdict(sub, cid, "holds", "x")
    # A checker a worker started inside the lead's session: refused the same way.
    with pytest.raises(Refused, match="not started for"):
        reg.record_claim_verdict(Caller("stemma:claim-checker", "a-w", "s-lead"), cid,
                                 "holds", "x")


def test_waiting_user_branch_proceeds_on_assumption(reg: Register) -> None:
    reg.tree_upsert(LEAD, {"id": "n1", "kind": "question", "statement": "x", "branch_id": "b1"})
    write_record(reg, "b1")
    reg.propose_branch_done(WORKER, "b1")
    reg.record_branch_verdict(reviewer_for(reg, "b1"), "b1", "ask_user",
                              "Which region does the decision cover?")
    assert reg.tree()[0]["status"] == "waiting_user"
    with pytest.raises(Refused, match="do not proceed on an assumption"):
        reg.proceed_on_assumption(WORKER, "b1", "Europe only")
    with pytest.raises(Refused, match="state the assumption"):
        reg.proceed_on_assumption(LEAD, "b1", " ")
    out = reg.proceed_on_assumption(LEAD, "b1", "We assume Europe only.")
    assert out["question"] == "Which region does the decision cover?"
    assert reg.tree()[0]["status"] == "in_progress"
    with pytest.raises(Refused, match="not waiting on the user"):
        reg.proceed_on_assumption(LEAD, "b1", "again")
    reg.propose_branch_done(WORKER, "b1")
    rev = reviewer_for(reg, "b1", "s-rev2")
    with pytest.raises(Refused, match="names its gap type"):
        reg.record_branch_verdict(rev, "b1", "accept_gap", "the user holds it")
    with pytest.raises(Refused, match="gap type must be one of"):
        reg.record_branch_verdict(rev, "b1", "accept_gap", "x", gap_type="lazy")
    reg.record_branch_verdict(rev, "b1", "accept_gap", "the user holds it", gap_type="user_held")
    assert reg.tree()[0]["status"] == "gap_accepted"
    (reg.run_dir / "draft").mkdir()
    (reg.run_dir / "draft/answer.v1.md").write_text("# Answer\n\nThe answer holds.\n")
    reg.submit_draft(LEAD, 1)
    assert "does not list the assumption taken for branch b1" in " ".join(reg.finish_failures(1))
    (reg.run_dir / "draft/answer.v2.md").write_text(
        "# Answer\n\nThe answer holds. We assume Europe only.\n")
    reg.submit_draft(LEAD, 2)
    assert "assumption" not in " ".join(reg.finish_failures(2))


def test_user_held_needs_a_recorded_assumption(reg: Register) -> None:
    reg.tree_upsert(LEAD, {"id": "n1", "kind": "question", "statement": "x", "branch_id": "b1"})
    write_record(reg, "b1")
    reg.propose_branch_done(WORKER, "b1")
    with pytest.raises(Refused, match="needs an assumption"):
        reg.record_branch_verdict(reviewer_for(reg, "b1"), "b1", "accept_gap", "x",
                                  gap_type="user_held")


def test_await_checks_accepts_bare_branch_ids_and_draft_versions(reg: Register) -> None:
    reg.tree_upsert(LEAD, {"id": "n1", "kind": "question", "statement": "x", "branch_id": "b1"})
    (reg.run_dir / "branches/b1").mkdir(parents=True)
    out = reg.await_checks(["b1", 1, "2"], 0)
    assert [w["id"] for w in out["not_landed"]] == ["branch:b1", "draft:1", "draft:2"]


def test_await_checks_waits_for_a_check_not_yet_requested(reg: Register) -> None:
    import threading
    import time as _time

    def land_later() -> None:
        _time.sleep(1.0)
        other = Register(reg.run_dir, evidence=FakeEvidence())
        checks_api.request_check(reg.run_dir, "blind-framer", "frame:own", "run_start")
        other.record_frame(launched(other, "blind-framer", "frame:own", "s-f"), "own", "x")

    t = threading.Thread(target=land_later)
    t.start()
    started = _time.monotonic()
    out = reg.await_checks(["frame:own"], 20)
    t.join()
    assert "frame:own" in out["landed"]
    assert _time.monotonic() - started < 15
