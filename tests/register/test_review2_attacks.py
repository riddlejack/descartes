"""The cross-family review's findings on 4eea3ba, each reproduced, now refused."""

from __future__ import annotations

import os
import threading
from pathlib import Path

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

from stemma.hooks import guards
from stemma.register import checks_api
from stemma.register.derivation import DerivationError, rerun
from stemma.register.filing import Refused
from stemma.register.store import Caller, Register


@pytest.fixture
def reg(tmp_path: Path) -> Register:
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "criteria.md").write_text("- C1: The answer says whether the target is met.\n")
    return Register(tmp_path / "run", evidence=FakeEvidence())


def proposed(reg: Register) -> None:
    reg.tree_upsert(LEAD, {"id": "n1", "kind": "question", "statement": "x", "branch_id": "b1"})
    write_record(reg, "b1")
    reg.propose_branch_done(WORKER, "b1")


# --- 1. Authorization and write under one lock ------------------------------------

def test_stale_round_cannot_slip_in_between_authorization_and_write(reg: Register) -> None:
    proposed(reg)
    old = reviewer_for(reg, "b1", "s-old")
    real = reg._session_prompt_is

    def raced(caller: Caller, role: str, prompt: str) -> None:
        real(caller, role, prompt)
        # Between the old session's authorization and its write, the author reopens
        # the node and proposes round 2 from another process.
        if caller.session_id == "s-old" and not getattr(raced, "done", False):
            raced.done = True  # type: ignore[attr-defined]
            other = Register(reg.run_dir, evidence=FakeEvidence())
            t = threading.Thread(target=lambda: (
                other.tree_upsert(WORKER, {"id": "n1", "statement": "changed"}),
                other.propose_branch_done(WORKER, "b1")))
            t.start()
            t.join(2)  # blocked on the register lock, which the write still holds

    reg._session_prompt_is = raced  # type: ignore[method-assign]
    reg.record_branch_verdict(old, "b1", "answered", "fine")
    # Whatever order the lock allowed, the old reviewer's verdict never answers round 2.
    rounds = checks_api.for_prompt(reg.run_dir, "branch-reviewer", "branch:b1")
    status = Register(reg.run_dir, evidence=FakeEvidence()).tree()[0]["status"]
    assert not (len(rounds) == 2 and status == "answered")


# --- 2. A branch verdict binds to the frozen record it was given ------------------

def test_verdict_binds_to_the_frozen_record_not_the_live_one(reg: Register) -> None:
    proposed(reg)
    frozen = (reg.run_dir / "register/branches/b1/record.r1.md").read_bytes()
    rev = reviewer_for(reg, "b1")
    # The worker swaps the record after proposing it, before the review lands.
    (reg.run_dir / "branches/b1/record.md").write_text("A different conclusion.\n")
    out = reg.record_branch_verdict(rev, "b1", "answered", "fine")
    import hashlib
    assert out["record_digest"] == hashlib.sha256(frozen).hexdigest()
    # The live record no longer matches what was reviewed, so the node reopens.
    reg.tree_upsert(LEAD, {"id": "n2", "kind": "question", "statement": "y"})
    node = next(n for n in reg.tree() if n["id"] == "n1")
    assert node["status"] == "in_progress" and node["reopened_from"] == "answered"


def test_record_change_after_answer_blocks_release(reg: Register) -> None:
    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    (reg.run_dir / "branches/b1/record.md").write_text("Rewritten after the review.\n")
    with pytest.raises(Refused, match="node n1 is in_progress"):
        reg.finish_run(LEAD, 1)


# --- 3. A revoked session loses its authority --------------------------------------

def test_revoked_checker_verdict_is_discounted_and_further_ones_refused(reg: Register) -> None:
    cid = reg.file_claim(WORKER, claim())["claim_id"]
    chk = checker_for(reg, cid, "s-wrong-model")
    reg.record_claim_verdict(chk, cid, "holds", "ok")
    reg.revoke_session("s-wrong-model", "resolved model differs from run.json")
    assert reg.get_claim(cid)["verdict"] is None
    with pytest.raises(Refused, match="not started for"):
        reg.record_claim_verdict(chk, cid, "holds", "again")
    # The driver relaunches the check; the new session's verdict counts.
    req = checks_api.for_prompt(reg.run_dir, "claim-checker", f"claim:{cid}")[-1].request
    checks_api.mark_launched(reg.run_dir, req.request_id, "s-right", "claude-opus-5-5", "high",
                             relaunch=True)
    reg.record_claim_verdict(Caller("stemma:claim-checker", None, "s-right"), cid, "holds", "ok")
    assert reg.get_claim(cid)["verdict"]["session"] == "s-right"


def test_revoked_branch_reviewer_answer_goes_back_to_proposed(reg: Register) -> None:
    proposed(reg)
    reg.record_branch_verdict(reviewer_for(reg, "b1", "s-bad"), "b1", "answered", "x")
    reg.revoke_session("s-bad", "effort differs")
    assert reg.tree()[0]["status"] == "proposed_done"
    assert reg.await_checks(["branch:b1"], 0)["landed"] == {}


def test_revoked_reviews_and_frames_do_not_count(reg: Register) -> None:
    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    reg.revoke_session("s-r1-0", "model differs")  # the decider's-eye review session
    reg.revoke_session("s-f2", "model differs")  # the critique session
    fails = " ".join(reg.finish_failures(1))
    assert "deciders-eye-reviewer review of version 1 is not recorded" in fails
    assert "critique is not recorded" in fails


# --- 4. Derivation output is contained ---------------------------------------------

@pytest.mark.parametrize("plant", ["fifo", "zero", "huge", "link"])
def test_rerun_result_is_a_capped_regular_file(tmp_path: Path, plant: str) -> None:
    script = tmp_path / "analysis" / "evil.py"
    script.parent.mkdir()
    body = {
        "fifo": "os.mkfifo(r)",
        "zero": "os.symlink('/dev/zero', r)",
        "huge": "open(r, 'w').write('{\"number\": 1' + ' ' * (2 << 20) + '}')",
        "link": "open('real.json','w').write('{\"number\": 1}'); os.symlink('real.json', r)",
    }[plant]
    script.write_text(f"import os\nr = os.environ['STEMMA_ANALYSIS_RESULT']\n{body}\n")
    with pytest.raises(DerivationError, match="result"):
        rerun(script, {}, time_limit_s=20)


def test_rerun_does_not_wait_for_a_lingering_child(tmp_path: Path) -> None:
    import time
    script = tmp_path / "analysis" / "fork.py"
    script.parent.mkdir()
    script.write_text(
        "import os, time\nfrom stemma.analysis import result\n"
        "if os.fork() == 0:\n    time.sleep(15)\n    os._exit(0)\nresult(1)\n")
    started = time.monotonic()
    assert rerun(script, {}, time_limit_s=20).number == 1.0
    assert time.monotonic() - started < 8  # the child holding the pipes is killed


# --- 6. A missing or unsafe frozen draft fails closed ------------------------------

@pytest.mark.parametrize("damage", ["delete", "symlink", "empty"])
def test_damaged_frozen_draft_is_refused(reg: Register, damage: str) -> None:
    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    frozen = reg.run_dir / "register/drafts/answer.v1.md"
    frozen.unlink()
    if damage == "symlink":
        (reg.run_dir / "decoy.md").write_text("")
        os.symlink(reg.run_dir / "decoy.md", frozen)
    if damage == "empty":
        frozen.write_text("")
    with pytest.raises(Refused, match="frozen draft"):
        reg.finish_run(LEAD, 1)
    assert not (reg.run_dir / "deliverable/answer.md").exists()


# --- Suspected: agent writes outside the run folder --------------------------------

@pytest.mark.parametrize("target", ["/tmp/x.py", "../outside.md", "~/.claude/settings.json"])
def test_writes_outside_the_run_folder_refused(tmp_path: Path, target: str) -> None:
    rd = tmp_path / "run"
    rd.mkdir()
    hook = {"tool_name": "Write", "tool_input": {"file_path": target}, "agent_type": "stemma:lead",
            "session_id": "s", "tool_use_id": "t", "cwd": str(rd)}
    d = guards.guard_write(hook, rd)
    assert not d.allow and "outside the run folder" in d.refusal


def test_launched_helper_still_used(reg: Register) -> None:
    checks_api.request_check(reg.run_dir, "blind-framer", "frame:own", "run_start")
    assert launched(reg, "blind-framer", "frame:own", "s").session_id == "s"


def test_driver_revoke_path_reaches_the_register(reg: Register) -> None:
    from stemma.driver.runner import register_revoke

    cid = reg.file_claim(WORKER, claim())["claim_id"]
    reg.record_claim_verdict(checker_for(reg, cid, "s-x"), cid, "holds", "ok")
    register_revoke(reg.run_dir, "s-x", "effort low, run.json says high")
    assert Register(reg.run_dir, evidence=FakeEvidence()).get_claim(cid)["verdict"] is None
    assert "s-x" in checks_api.revoked_sessions(reg.run_dir)
