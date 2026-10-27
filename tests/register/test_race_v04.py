"""v0.4 race: the lead submits a draft (listing claims for document checks) while two
document checkers record verdicts, each in its own process, released by one barrier."""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path
from typing import Any

from test_store import LEAD, WORKER, FakeEvidence, claim, finish_reviews

from stemma.register import checks_api, jsonl
from stemma.register.store import Caller, Register

N = 12  # claims per document: enough that the writers interleave


def _submit(run_dir: str, barrier: Any) -> None:
    reg = Register(Path(run_dir), evidence=FakeEvidence())
    barrier.wait()
    reg.submit_draft(LEAD, 2)


def _judge(run_dir: str, barrier: Any, session: str, ids: list[str]) -> None:
    reg = Register(Path(run_dir), evidence=FakeEvidence())
    barrier.wait()
    for cid in ids:
        reg.record_claim_verdict(Caller("stemma:claim-checker", None, session), cid, "holds",
                                 "the quotation is in the source")


def test_submission_and_verdicts_at_once(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "criteria.md").write_text("- C1: a\n")
    reg = Register(run, evidence=FakeEvidence())
    ids = {r: [reg.file_claim(WORKER, claim(
        statement=f"Claim {r} {i}: revenue of 1.2bn.",
        support=[{"receipt_id": r, "quote": "revenue of 1.2bn in fiscal 2025"}]))["claim_id"]
        for i in range(N)] for r in ("r1", "r2")}
    (run / "draft").mkdir()
    body = "".join(f"F [[c:{c[:12]}]].\n" for r in ids for c in ids[r])
    (run / "draft/answer.v1.md").write_text(body)
    reg.submit_draft(LEAD, 1)
    for r in ids:
        req = next(x for x in checks_api.pending_checks(run) if x.prompt == f"doc:{r}")
        checks_api.mark_launched(run, req.request_id, f"s-{r}", "claude-opus-5-5", "high")
    (run / "draft/answer.v2.md").write_text(body)
    finish_reviews(reg, 1)
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(3)
    procs = [ctx.Process(target=_submit, args=(str(run), barrier))] + [
        ctx.Process(target=_judge, args=(str(run), barrier, f"s-{r}", ids[r])) for r in ids]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    reg = Register(run, evidence=FakeEvidence())
    assert reg.submitted_versions() == [1, 2]
    # Every claim holds; the running round-1 checks listed them all, so version 2 asked
    # for no second round of either document, whatever order the writers took.
    assert all(reg.get_claim(c)["verdict"]["verdict"] == "holds" for r in ids for c in ids[r])
    for r in ids:
        assert len(checks_api.for_prompt(run, "claim-checker", f"doc:{r}")) == 1
    seqs = [e["seq"] for e in reg.wm.events()]
    assert seqs == sorted(set(seqs))
    assert jsonl.read_all(checks_api.log_path(run))  # the checks log reads whole
