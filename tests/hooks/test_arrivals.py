"""`stemma attach` and the arrival notice the lead's next tool call receives, through the
real hook entry point."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from test_guards import call, events, pre

from stemma.hooks import arrivals
from stemma.hooks.arrivals import AttachError, attach
from stemma.register import jsonl


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "run"
    for sub in ("register", "task", ".stemma", "analysis"):
        (d / sub).mkdir(parents=True)
    (d / "run.json").write_text("{}")
    return d


@pytest.fixture
def questions(tmp_path: Path) -> Path:
    f = tmp_path / "outside" / "client-questions.csv"
    f.parent.mkdir()
    f.write_text("q,owner\nWhat is the pace?,analyst\n")
    return f


def context(stdout: str) -> str | None:
    if not stdout:
        return None
    out = json.loads(stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    return out.get("additionalContext")


def test_attach_copies_into_task_arrivals_and_records_it(run_dir: Path,
                                                         questions: Path) -> None:
    a = attach(run_dir, questions)
    b = attach(run_dir, questions)
    assert (a["n"], a["path"]) == (1, "task/arrivals/1/client-questions.csv")
    assert b["path"] == "task/arrivals/2/client-questions.csv"
    copy = run_dir / a["path"]
    assert copy.read_bytes() == questions.read_bytes() and not copy.is_symlink()
    assert not list((run_dir / arrivals.STAGING).iterdir())  # nothing left staged
    assert copy.stat().st_nlink == 1  # capture_file refuses a file with other names
    assert [e["path"] for e in events(run_dir) if e["event"] == "arrival"] == [a["path"],
                                                                               b["path"]]


def test_attach_refuses_what_is_not_a_plain_file(run_dir: Path, questions: Path,
                                                 tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    link = tmp_path / "link.csv"
    link.symlink_to(questions)
    with pytest.raises(AttachError, match="not a regular file"):
        attach(run_dir, link)
    with pytest.raises(AttachError, match="not a regular file"):
        attach(run_dir, tmp_path)
    hidden = tmp_path / ".env"
    hidden.write_text("x")
    with pytest.raises(AttachError, match="not a plain name"):
        attach(run_dir, hidden)
    monkeypatch.setattr(arrivals, "ATTACH_MAX_BYTES", 4)
    with pytest.raises(AttachError, match="larger than 4 bytes"):
        attach(run_dir, questions)
    with pytest.raises(AttachError, match="not a run folder"):
        attach(tmp_path / "outside", questions)
    assert not (run_dir / "task/arrivals").exists()


def test_the_leads_next_tool_call_receives_the_notice_once(run_dir: Path,
                                                           questions: Path) -> None:
    rec = attach(run_dir, questions)
    r = call(run_dir, "pre", pre(run_dir, "Read", {"file_path": str(run_dir / "intake.md")}))
    assert r.returncode == 0
    note = context(r.stdout)
    assert note is not None and rec["path"] in note and "capture_file" in note
    again = call(run_dir, "pre", pre(run_dir, "Read", {"file_path": str(run_dir / "x.md")}))
    assert (again.returncode, again.stdout) == (0, "")  # delivered once
    sent = [e for e in events(run_dir) if e["event"] == "arrival_notice_delivered"]
    assert [(e["n"], e["tool_use_id"]) for e in sent] == [(1, "toolu_1")]


def test_only_the_leads_own_thread_takes_it(run_dir: Path, questions: Path) -> None:
    attach(run_dir, questions)
    worker = pre(run_dir, "Read", {"file_path": str(run_dir / "intake.md")},
                 agent_type="stemma:worker", agent_id="a1")
    checker = pre(run_dir, "Read", {"file_path": str(run_dir / "intake.md")},
                  agent_type="stemma:claim-checker")
    for hook in (worker, checker):
        r = call(run_dir, "pre", hook)
        assert r.returncode == 0 and context(r.stdout) is None
    r = call(run_dir, "pre", pre(run_dir, "Read", {"file_path": str(run_dir / "intake.md")}))
    assert context(r.stdout) is not None


def test_a_refused_call_never_swallows_the_notice(run_dir: Path, questions: Path,
                                                  tmp_path: Path) -> None:
    attach(run_dir, questions)
    refused = call(run_dir, "pre", pre(run_dir, "Write", {"file_path": str(tmp_path / "x"),
                                                          "content": "x"}))
    assert refused.returncode == 2
    r = call(run_dir, "pre", pre(run_dir, "Read", {"file_path": str(run_dir / "intake.md")}))
    assert context(r.stdout) is not None


def run_hook(run_dir: Path, hook: dict[str, Any],
             monkeypatch: pytest.MonkeyPatch) -> tuple[int, str, str]:
    from stemma.hooks.__main__ import run

    monkeypatch.setenv("STEMMA_RUN_DIR", str(run_dir))
    return run("pre", json.dumps(hook))


def test_parallel_tool_calls_deliver_each_notice_once(run_dir: Path, questions: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    attach(run_dir, questions)
    attach(run_dir, questions)
    got: list[str | None] = []

    def one(i: int) -> None:
        hook = pre(run_dir, "Read", {"file_path": str(run_dir / "intake.md")},
                   tool_use_id=f"toolu_{i}")
        got.append(context(run_hook(run_dir, hook, monkeypatch)[1]))

    threads = [threading.Thread(target=one, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    notes = [g for g in got if g is not None]
    assert len(notes) == 1 and "arrivals/1/" in notes[0] and "arrivals/2/" in notes[0]
    marks = [r for r in jsonl.read_all(run_dir / arrivals.LOG) if r["kind"] == "delivered"]
    assert sorted(m["n"] for m in marks) == [1, 2]


def test_a_failed_event_write_blocks_the_call_but_keeps_the_notice_queued(
        run_dir: Path, questions: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Codex review of 2717edc: the notice was marked delivered before the event write,
    # so a failed write blocked the call and the notice was never delivered.
    from stemma.hooks import __main__ as hook_main

    attach(run_dir, questions)
    real = hook_main.jsonl.append

    def broken(log: Path, *records: dict[str, Any]) -> None:
        if log.name == "events.jsonl":
            raise OSError("disk full")
        real(log, *records)

    hook = pre(run_dir, "Read", {"file_path": str(run_dir / "intake.md")})
    monkeypatch.setattr(hook_main.jsonl, "append", broken)
    code, out, err = run_hook(run_dir, hook, monkeypatch)
    assert code == 2 and out == "" and "guard failed" in err
    monkeypatch.setattr(hook_main.jsonl, "append", real)
    code, out, _ = run_hook(run_dir, hook, monkeypatch)
    assert code == 0 and "task/arrivals/1/" in (context(out) or "")


def test_a_failed_answer_keeps_the_notice_queued(run_dir: Path, questions: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    from stemma.hooks import __main__ as hook_main

    attach(run_dir, questions)
    hook = pre(run_dir, "Read", {"file_path": str(run_dir / "intake.md")})
    real = hook_main._answer
    monkeypatch.setattr(hook_main, "_answer", lambda *_a: (_ for _ in ()).throw(ValueError()))
    assert run_hook(run_dir, hook, monkeypatch)[0] == 2
    monkeypatch.setattr(hook_main, "_answer", real)
    assert "task/arrivals/1/" in (context(run_hook(run_dir, hook, monkeypatch)[1]) or "")


def test_the_notice_rides_beside_an_input_rewrite(run_dir: Path, questions: Path) -> None:
    attach(run_dir, questions)
    hook: dict[str, Any] = pre(run_dir, "Agent", {"subagent_type": "stemma:worker",
                                                  "prompt": "go", "model": "other"})
    r = call(run_dir, "pre", hook)
    assert r.returncode == 0
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["updatedInput"] == {"subagent_type": "stemma:worker", "prompt": "go"}
    assert "task/arrivals/1/" in out["additionalContext"]


def test_the_cli_attaches_by_run_id_under_stemma_runs_only(
        tmp_path: Path, questions: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    from stemma.driver.__main__ import main

    rid = "20261026-154644-b3b359"
    runs = tmp_path / "runs"
    real = runs / rid
    for sub in ("task", ".stemma"):
        (real / sub).mkdir(parents=True)
    (real / "run.json").write_text("{}")
    # Codex review of 2717edc: a same-named folder in the working directory won.
    decoy = tmp_path / "cwd" / rid
    for sub in ("task", ".stemma"):
        (decoy / sub).mkdir(parents=True)
    (decoy / "run.json").write_text("{}")
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setenv("STEMMA_RUNS", str(runs))
    assert main(["attach", rid, str(questions)]) == 0
    assert json.loads(capsys.readouterr().out)["path"] == \
        "task/arrivals/1/client-questions.csv"
    assert (real / "task/arrivals/1/client-questions.csv").exists()
    assert not (decoy / "task/arrivals").exists()
    for bad in (str(real), "../" + rid, rid + "/..", "latest", "20261026-154644-B3B359"):
        assert main(["attach", bad, str(questions)]) == 2
    assert "is not a run id" in capsys.readouterr().err
    assert main(["attach", "20261026-000000-000000", str(questions)]) == 2  # no such run


def test_an_arrival_can_be_captured_by_the_evidence_service(run_dir: Path,
                                                            questions: Path) -> None:
    from stemma.evidence.service import EvidenceService

    rec = attach(run_dir, questions)
    receipt = EvidenceService(run_dir).capture_file(rec["path"], "the client's question list")
    assert receipt["status"] == "ok", receipt


def test_the_copy_is_made_without_the_lock(run_dir: Path, questions: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    # Codex review of 2717edc: the lock was held while copying up to 512 MiB.
    from stemma.register import safeio

    held_during_write: list[bool] = []
    real_write = safeio.write_bytes
    lock_path = run_dir / ".stemma" / "arrivals.jsonl.lock"

    def watched(rd: Path, rel: Any, data: bytes) -> None:
        import fcntl
        import os

        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                held_during_write.append(False)
            except BlockingIOError:
                held_during_write.append(True)
        finally:
            os.close(fd)
        real_write(rd, rel, data)

    monkeypatch.setattr(arrivals.safeio, "write_bytes", watched)
    attach(run_dir, questions)
    assert held_during_write == [False]
