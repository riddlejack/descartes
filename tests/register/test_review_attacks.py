"""The merge-gate review's attacks on 09e15da, each reproduced and now refused."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from test_store import (
    LEAD,
    WORKER,
    FakeEvidence,
    checker_for,
    claim,
    launched,
    ready_run,
    reviewer_for,
    write_record,
)

from stemma.driver import settings as st
from stemma.hooks import guards, paths
from stemma.register import checks_api, jsonl, safeio
from stemma.register.filing import Refused
from stemma.register.store import Caller, Register


@pytest.fixture
def reg(tmp_path: Path) -> Register:
    return Register(tmp_path / "run", evidence=FakeEvidence())


def verified(reg: Register, cid: str) -> None:
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "matches")


# --- 1. Malformed figure tags ------------------------------------------------------

@pytest.mark.parametrize("tag", ["[[c:ABCDEF123456]]", "[[c:zzzzzzzzzzzz]]", "[[c:abc]]",
                                 "[[c: abcdef123456]]", "[[c:]]", "[[c:abcdef1234567]]",
                                 "[[C:abcdef123456]]"])
def test_malformed_c_tag_refused_at_release(reg: Register, tag: str) -> None:
    ready_run(reg, f"# Answer\n\nRevenue was 9.9bn {tag}.\n")
    fails = " ".join(reg.finish_failures(1))
    assert "malformed tag" in fails and "untagged number" in fails


# --- 2. Host writes through planted symlinks ---------------------------------------

@pytest.mark.parametrize("name", ["tree.md", "tree.json", "tree.json.tmp"])
def test_symlink_at_a_host_written_name_never_redirects_the_write(tmp_path: Path,
                                                                 name: str) -> None:
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "CLAUDE.md").write_text("STANDING RULES\n")
    os.symlink("CLAUDE.md", rd / name)
    Register(rd).tree_upsert(LEAD, {"id": "n1", "kind": "condition", "statement": "INJECTED"})
    assert (rd / "CLAUDE.md").read_text() == "STANDING RULES\n"
    assert not (rd / "tree.md").is_symlink() and not (rd / "tree.json").is_symlink()


def test_symlinked_branch_folder_is_refused(reg: Register, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (reg.run_dir / "branches").mkdir(parents=True)
    os.symlink(outside, reg.run_dir / "branches/b1")
    reg.tree_upsert(LEAD, {"id": "n1", "kind": "condition", "statement": "x", "branch_id": "b1"})
    (outside / "record.md").write_text("x")
    with pytest.raises(Refused, match="not a plain folder"):
        reg.propose_branch_done(WORKER, "b1")


def test_symlinked_verdicts_folder_cannot_take_a_verdict_elsewhere(reg: Register,
                                                                   tmp_path: Path) -> None:
    reg.tree_upsert(LEAD, {"id": "n1", "kind": "condition", "statement": "x", "branch_id": "b1"})
    write_record(reg, "b1")
    reg.propose_branch_done(WORKER, "b1")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, reg.run_dir / "branches/b1/verdicts")
    with pytest.raises(Refused, match="cannot be written safely"):
        reg.record_branch_verdict(reviewer_for(reg, "b1"), "b1", "answered", "x")
    assert list(elsewhere.iterdir()) == []


def test_symlinked_draft_is_refused_at_submit(reg: Register) -> None:
    (reg.run_dir / "draft").mkdir()
    (reg.run_dir / "secret.md").write_text("not a draft")
    os.symlink(reg.run_dir / "secret.md", reg.run_dir / "draft/answer.v1.md")
    with pytest.raises(Refused, match="not a plain file"):
        reg.submit_draft(LEAD, 1)


def test_planted_fifo_does_not_hang_reads(reg: Register) -> None:
    (reg.run_dir / "draft").mkdir()
    os.mkfifo(reg.run_dir / "draft/answer.v1.md")
    with pytest.raises(Refused, match="not a plain file"):
        reg.submit_draft(LEAD, 1)
    os.mkfifo(reg.run_dir / "notes.md")
    assert reg.dependents("a" * 64) == []


def test_safeio_refuses_symlinked_parent_and_dotdot(tmp_path: Path) -> None:
    rd = tmp_path / "run"
    (rd / "real").mkdir(parents=True)
    os.symlink(tmp_path, rd / "link")
    with pytest.raises(safeio.UnsafePath):
        safeio.write_text(rd, "link/x.md", "x")
    with pytest.raises(safeio.UnsafePath):
        safeio.write_text(rd, "../x.md", "x")
    assert not (tmp_path / "x.md").exists()


def test_host_written_names_are_tool_only() -> None:
    for rel in ("tree.md", "tree.json.tmp", "deliverable/answer.md", "reviews/v1/mechanical.md",
                "framer/own-frame.md", "framer/critique.md", "events.jsonl.lock"):
        assert paths.is_tool_only(PurePosixPath(rel.casefold())), rel
    assert not paths.is_tool_only(PurePosixPath("framer/lead-response.md"))


# --- 3. Frames and reviews are tool-written by the launched session ----------------

def test_authors_cannot_write_reviews_or_framer_outputs(tmp_path: Path) -> None:
    rd = tmp_path / "run"
    rd.mkdir()
    for rel in ("reviews/v1/deciders-eye.md", "reviews/v1/evidence-audit.md",
                "reviews/v1/mechanical.md", "framer/critique.md", "framer/own-frame.md",
                "deliverable/answer.md"):
        hook = {"tool_name": "Write", "tool_input": {"file_path": str(rd / rel)},
                "agent_type": "stemma:lead", "session_id": "s", "tool_use_id": "t1",
                "cwd": str(rd)}
        assert not guards.guard_write(hook, rd).allow, rel
    extra = st.stemma_settings_extra(rd, "lead", "", st.ToolSets(("Write",), ("Write",)),
                                     tmp_path / "cfg")
    deny_write = extra["sandbox"]["filesystem"]["denyWrite"]
    for rel in ("reviews", "deliverable", "framer/own-frame.md", "framer/critique.md",
                "tree.md"):
        assert str(rd / rel) in deny_write, rel


def test_review_files_written_by_hand_do_not_count(reg: Register) -> None:
    cid = ready_run(reg)
    verified(reg, cid)
    reg2 = Register(reg.run_dir, evidence=FakeEvidence())
    (reg.run_dir / "draft/answer.v2.md").write_text("No figures.\n")
    reg2.submit_draft(LEAD, 2)
    (reg.run_dir / "reviews/v2").mkdir(parents=True)
    for f in ("deciders-eye.md", "evidence-audit.md", "mechanical.md"):
        (reg.run_dir / "reviews/v2" / f).write_text("forged")
    fails = " ".join(reg2.finish_failures(2))
    assert "review of version 2 is not recorded" in fails


def test_record_review_only_from_the_session_launched_for_that_version(reg: Register) -> None:
    cid = ready_run(reg)
    verified(reg, cid)
    (reg.run_dir / "draft/answer.v2.md").write_text("No figures.\n")
    reg.submit_draft(LEAD, 2)
    v1_session = Caller("stemma:mechanical-reader", None, "s-r1-2")  # launched for draft:1
    with pytest.raises(Refused, match="not started for draft:2"):
        reg.record_review(v1_session, 2, "x", [])
    with pytest.raises(Refused, match="do not record reviews"):
        reg.record_review(LEAD, 2, "x", [])
    with pytest.raises(Refused, match="already recorded"):
        reg.record_review(v1_session, 1, "again", [])


def test_record_frame_only_by_the_launched_framer(reg: Register) -> None:
    checks_api.request_check(reg.run_dir, "blind-framer", "frame:own", "run_start")
    with pytest.raises(Refused, match="do not record frames"):
        reg.record_frame(LEAD, "own", "x")
    with pytest.raises(Refused, match="not started for frame:own"):
        reg.record_frame(Caller("stemma:blind-framer", None, "s-unknown"), "own", "x")
    framer = launched(reg, "blind-framer", "frame:own", "s-f")
    with pytest.raises(Refused, match="not started for frame:critique"):
        reg.record_frame(framer, "critique", "x")
    reg.record_frame(framer, "own", "mine")
    assert (reg.run_dir / "framer/own-frame.md").read_text() == "mine"


# --- 4. The deliverable is the frozen, reviewed draft ------------------------------

def test_release_uses_the_frozen_reviewed_bytes(reg: Register) -> None:
    cid = ready_run(reg)
    verified(reg, cid)
    frozen = (reg.run_dir / "register/drafts/answer.v1.md").read_bytes()
    # The lead edits the draft and plants a deliverable after review: neither is released.
    (reg.run_dir / "draft/answer.v1.md").write_text("An unreviewed conclusion.\n")
    (reg.run_dir / "deliverable").mkdir()
    (reg.run_dir / "deliverable/answer.md").write_text("Another unreviewed conclusion.\n")
    reg.finish_run(LEAD, 1)
    assert (reg.run_dir / "deliverable/answer.md").read_bytes() == frozen
    assert "Revenue was 1.2bn [1]" in (reg.run_dir / "deliverable/answer.reader.md").read_text()


def test_release_of_a_version_without_its_reviews_refused(reg: Register) -> None:
    cid = ready_run(reg)
    verified(reg, cid)
    (reg.run_dir / "draft/answer.v2.md").write_text("No figures.\n")
    reg.submit_draft(LEAD, 2)
    with pytest.raises(Refused, match="review of version 2 is not recorded"):
        reg.finish_run(LEAD, 2)
    with pytest.raises(Refused, match="version 3 was not submitted"):
        reg.finish_run(LEAD, 3)


# --- 5. A correction cannot land on an existing claim ------------------------------

def test_correction_equal_to_another_claim_refused(reg: Register) -> None:
    a = reg.file_claim(WORKER, claim())["claim_id"]
    b_raw = claim(statement="Acme revenue was 1.0bn in fiscal 2024.",
                  value={"number": 1.0e9, "unit": "USD", "period": "FY2024", "kind": "level",
                         "as_written": "1.0bn"},
                  support=[{"receipt_id": "r1", "quote": "up from 1.0bn"}])
    b = reg.file_claim(WORKER, b_raw)["claim_id"]
    reg.record_claim_verdict(checker_for(reg, b, "s-b"), b, "not_supported", "wrong period")
    with pytest.raises(Refused, match="already in the register"):
        reg.record_claim_verdict(checker_for(reg, a, "s-a"), a, "holds_with_correction",
                                 "see b", corrected_claim=b_raw)
    assert reg.get_claim(b)["verdict"]["verdict"] == "not_supported"
    assert reg.get_claim(a)["superseded_by"] is None


# --- 7. A reviewed node cannot be rewritten in place --------------------------------

@pytest.mark.parametrize("change", [{"statement": "A different crux never reviewed"},
                                    {"branch_id": "b9"}, {"kind": "rival"}])
def test_changing_an_answered_node_reopens_it(reg: Register, change: dict[str, Any]) -> None:
    reg.tree_upsert(LEAD, {"id": "n1", "kind": "condition", "statement": "Demand holds",
                           "branch_id": "b1", "crux": True})
    write_record(reg, "b1")
    reg.propose_branch_done(WORKER, "b1")
    reg.record_branch_verdict(reviewer_for(reg, "b1"), "b1", "answered", "done")
    out = reg.tree_upsert(WORKER, {"id": "n1", **change})
    assert out["status"] == "in_progress" and out["reopened_from"] == "answered"
    assert "node n1 is in_progress" in " ".join(reg.finish_failures())


def test_changing_a_node_under_review_voids_the_review(reg: Register) -> None:
    reg.tree_upsert(LEAD, {"id": "n1", "kind": "condition", "statement": "x", "branch_id": "b1"})
    write_record(reg, "b1")
    reg.propose_branch_done(WORKER, "b1")
    rev = reviewer_for(reg, "b1")
    reg.tree_upsert(WORKER, {"id": "n1", "statement": "something else"})
    with pytest.raises(Refused, match="not proposed done"):
        reg.record_branch_verdict(rev, "b1", "answered", "x")


# --- 8. A verdict binds to its round --------------------------------------------------

def test_round1_reviewer_cannot_answer_round2(reg: Register) -> None:
    reg.tree_upsert(LEAD, {"id": "n1", "kind": "condition", "statement": "x", "branch_id": "b1"})
    write_record(reg, "b1")
    reg.propose_branch_done(WORKER, "b1")
    old = reviewer_for(reg, "b1", "s-old")
    reg.record_branch_verdict(old, "b1", "find_more", "thin", method="filings")
    reg.propose_branch_done(WORKER, "b1")
    with pytest.raises(Refused, match="earlier round"):
        reg.record_branch_verdict(old, "b1", "answered", "fine now")
    assert reg.tree()[0]["status"] == "proposed_done"


# --- 9. The claim checker cannot read the frozen drafts ----------------------------

def test_claim_checker_cannot_read_frozen_drafts(tmp_path: Path) -> None:
    rd = tmp_path / "run"
    (rd / "register/drafts").mkdir(parents=True)
    (rd / "register/drafts/answer.v1.md").write_text("draft")
    assert "register/" in st.read_denials(rd, "claim-checker", "claim:abc")
    hook = {"tool_name": "Read", "tool_input": {"file_path": str(rd / "register/drafts/answer.v1.md")},
            "agent_type": "stemma:claim-checker", "session_id": "s", "tool_use_id": "t",
            "cwd": str(rd)}
    assert not guards.guard_read(hook, rd, None).allow


# --- 10. A torn last line does not stop the run --------------------------------------

def test_torn_line_in_checks_log_is_recovered(reg: Register) -> None:
    reg.file_claim(WORKER, claim())
    log = checks_api.log_path(reg.run_dir)
    with open(log, "ab") as f:
        f.write(b'{"kind":"launched","request_id":"ab')  # a writer died mid-append
    reg.file_claim(WORKER, claim(statement="Another 1.2bn claim."))
    assert len(checks_api.pending_checks(reg.run_dir)) == 2
    assert Path(str(log) + ".torn").read_bytes() == b'{"kind":"launched","request_id":"ab\n'


def test_torn_line_in_events_log_is_recovered(reg: Register) -> None:
    ev = reg.run_dir / "events.jsonl"
    with open(ev, "ab") as f:
        f.write(b'{"event":"file_wr')
    jsonl.append(ev, {"event": "x"})
    assert [e["event"] for e in jsonl.read_all(ev)] == ["x"]
    assert reg.dependents("a" * 64) == []


def test_symlink_at_a_log_name_is_refused(tmp_path: Path) -> None:
    (tmp_path / "target").write_text("")
    os.symlink(tmp_path / "target", tmp_path / "events.jsonl")
    with pytest.raises(OSError):
        jsonl.append(tmp_path / "events.jsonl", {"event": "x"})
    assert (tmp_path / "target").read_text() == ""


def test_whole_flow_still_releases(reg: Register) -> None:
    cid = ready_run(reg)
    verified(reg, cid)
    assert reg.finish_run(LEAD, 1)["finished"]
