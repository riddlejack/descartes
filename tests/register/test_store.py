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
    return Register(tmp_path / "run", evidence=FakeEvidence())


def checker_for(reg: Register, cid: str, session: str = "s-check") -> Caller:
    req = next(r for r in checks_api.pending_checks(reg.run_dir) if r.prompt == f"claim:{cid}")
    checks_api.mark_launched(reg.run_dir, req.request_id, session, "claude-opus-5-5", "high")
    return Caller("stemma:claim-checker", None, session)


def test_file_claim_records_author_from_caller_and_requests_check(reg: Register) -> None:
    out = reg.file_claim(WORKER, claim())
    rec = reg.get_claim(out["claim_id"][:12])
    assert rec["author"] == "stemma:worker"
    assert rec["verdict"] is None
    pending = checks_api.pending_checks(reg.run_dir)
    assert [(p.role, p.prompt) for p in pending] == [("claim-checker", f"claim:{out['claim_id']}")]


def test_refiling_is_idempotent(reg: Register) -> None:
    a = reg.file_claim(WORKER, claim())
    log = (reg.dir / "log.jsonl").read_bytes()
    b = reg.file_claim(LEAD, claim())
    assert (a["claim_id"], b["already_filed"]) == (b["claim_id"], True)
    assert (reg.dir / "log.jsonl").read_bytes() == log  # nothing appended
    assert len(checks_api.pending_checks(reg.run_dir)) == 1


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
    return Caller("stemma:branch-reviewer", None, session)


def test_branch_reaches_answered_only_by_a_verdict(reg: Register) -> None:
    reg.tree_upsert(LEAD, {"id": "n1", "kind": "condition", "statement": "Demand holds",
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
    reg.record_branch_verdict(rev, "b1", "find_more", "thin", method="primary filings")
    assert reg.tree()[0]["status"] == "in_progress"
    # Second round: a new check request, and the old reviewer session cannot answer it.
    reg.propose_branch_done(WORKER, "b1")
    with pytest.raises(Refused, match="not started for"):
        reg.record_branch_verdict(Caller("stemma:branch-reviewer", None, "s-z"), "b1",
                                  "answered", "x")
    rev2 = reviewer_for(reg, "b1", "s-rev2")
    reg.record_branch_verdict(rev2, "b1", "answered", "the record carries the weight")
    assert reg.tree()[0]["status"] == "answered"
    files = sorted((reg.run_dir / "branches/b1/verdicts").glob("*.json"))
    assert [json.loads(f.read_text())["verdict"] for f in files] == ["find_more", "answered"]
    tree = json.loads((reg.run_dir / "tree.json").read_text())
    assert tree["nodes"][0]["status"] == "answered"


def test_record_without_contract_headings_refused(reg: Register) -> None:
    reg.tree_upsert(LEAD, {"id": "n1", "kind": "condition", "statement": "x", "branch_id": "b1"})
    d = reg.run_dir / "branches/b1"
    d.mkdir(parents=True)
    (d / "record.md").write_text("## Conclusion and confidence\nDone.\n")
    with pytest.raises(Refused, match="lacks the heading 'Claims \\(ids\\)'"):
        reg.propose_branch_done(WORKER, "b1")


@pytest.mark.parametrize("node, msg", [
    ({"id": "../x", "kind": "condition", "statement": "x"}, "plain id"),
    ({"id": "n2", "kind": "topic", "statement": "x"}, "kind must be one of"),
    ({"id": "n2", "kind": "condition", "statement": ""}, "needs a statement"),
    ({"id": "n2", "kind": "condition", "statement": "x", "parent": "zz"}, "not a node"),
    ({"id": "n2", "kind": "condition", "statement": "x", "verdict": "answered"}, "unknown"),
])
def test_hostile_tree_changes_refused(reg: Register, node: dict[str, Any], msg: str) -> None:
    with pytest.raises(Refused, match=msg):
        reg.tree_upsert(LEAD, node)


# --- Release gate -------------------------------------------------------------------


def ready_run(reg: Register) -> str:
    reg.tree_upsert(LEAD, {"id": "n1", "kind": "condition", "statement": "x", "branch_id": "b1"})
    write_record(reg, "b1")
    reg.propose_branch_done(WORKER, "b1")
    reg.record_branch_verdict(reviewer_for(reg, "b1"), "b1", "answered", "done")
    (reg.run_dir / "framer").mkdir()
    for f in ("own-frame.md", "critique.md", "lead-response.md"):
        (reg.run_dir / "framer" / f).write_text("x")
    (reg.run_dir / "draft").mkdir()
    (reg.run_dir / "draft/answer.v1.md").write_text("draft")
    reg.submit_draft(LEAD, 1)
    (reg.run_dir / "reviews/v1").mkdir(parents=True)
    for f in ("deciders-eye.md", "evidence-audit.md", "mechanical.md"):
        (reg.run_dir / "reviews/v1" / f).write_text("x")
    cid = reg.file_claim(WORKER, claim())["claim_id"]
    (reg.run_dir / "deliverable").mkdir()
    (reg.run_dir / "deliverable/answer.md").write_text(
        f"# Answer\n\nRevenue was 1.2bn [[c:{cid[:12]}]] in the year to 2025 [[nc:period]].\n")
    return cid


def test_finish_run_refused_until_a_verdict_exists(reg: Register) -> None:
    cid = ready_run(reg)
    with pytest.raises(Refused, match=f"claim {cid[:12]} has verdict none"):
        reg.finish_run(LEAD)
    reg.record_claim_verdict(checker_for(reg, cid), cid, "could_not_check", "source offline")
    out = reg.finish_run(LEAD)
    assert out["finished"]
    reader = (reg.run_dir / "deliverable/answer.reader.md").read_text()
    assert "[1, not checked]" in reader and "[[" not in reader
    assert "(not checked)" in reader


def test_finish_run_refuses_not_supported_and_untagged(reg: Register) -> None:
    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid), cid, "not_supported", "wrong period")
    with pytest.raises(Refused, match="has verdict not_supported"):
        reg.finish_run(LEAD)
    (reg.run_dir / "deliverable/answer.md").write_text("Sales were 40 units.\n")
    with pytest.raises(Refused, match="untagged number"):
        reg.finish_run(LEAD)


def test_finish_run_lists_every_missing_producer(reg: Register) -> None:
    with pytest.raises(Refused) as e:
        reg.finish_run(LEAD)
    msg = str(e.value)
    for part in ("the tree has no nodes", "framer/critique.md", "framer/lead-response.md",
                 "no draft was submitted", "deliverable/answer.md"):
        assert part in msg


def test_finish_run_only_by_the_lead(reg: Register) -> None:
    with pytest.raises(Refused, match="do not finish the run"):
        reg.finish_run(WORKER)


def test_await_checks_never_invents(reg: Register) -> None:
    cid = reg.file_claim(WORKER, claim())["claim_id"]
    out = reg.await_checks([cid[:12]], 0)
    assert out["landed"] == {}
    assert out["not_landed"][0]["status"] == "pending"
    checker = checker_for(reg, cid)
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
    requests = checks_api.states(run_dir)
    assert len(requests) == len(expected)  # one check per claim, none twice
    seqs = [e["seq"] for e in reg.wm.events()]
    assert seqs == sorted(set(seqs))
