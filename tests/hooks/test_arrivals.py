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
from stemma.hooks.arrivals import AttachError, attach, take_notice
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


def test_parallel_tool_calls_deliver_each_notice_once(run_dir: Path, questions: Path) -> None:
    attach(run_dir, questions)
    attach(run_dir, questions)
    got: list[str | None] = []

    def one(i: int) -> None:
        hook = pre(run_dir, "Read", {"file_path": "x"}, tool_use_id=f"toolu_{i}")
        got.append(take_notice(run_dir, hook)[0])

    threads = [threading.Thread(target=one, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    notes = [g for g in got if g is not None]
    assert len(notes) == 1 and "arrivals/1/" in notes[0] and "arrivals/2/" in notes[0]
    marks = [r for r in jsonl.read_all(run_dir / arrivals.LOG) if r["kind"] == "delivered"]
    assert sorted(m["n"] for m in marks) == [1, 2]


def test_the_notice_rides_beside_an_input_rewrite(run_dir: Path, questions: Path) -> None:
    attach(run_dir, questions)
    hook: dict[str, Any] = pre(run_dir, "Agent", {"subagent_type": "stemma:worker",
                                                  "prompt": "go", "model": "other"})
    r = call(run_dir, "pre", hook)
    assert r.returncode == 0
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["updatedInput"] == {"subagent_type": "stemma:worker", "prompt": "go"}
    assert "task/arrivals/1/" in out["additionalContext"]


def test_the_cli_attaches_by_run_id(run_dir: Path, questions: Path,
                                    capsys: pytest.CaptureFixture[str],
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    from stemma.driver.__main__ import main

    assert main(["attach", str(run_dir), str(questions)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["path"] == "task/arrivals/1/client-questions.csv"
    assert main(["attach", str(run_dir.parent / "no-such-run"), str(questions)]) == 2


def test_an_arrival_can_be_captured_by_the_evidence_service(run_dir: Path,
                                                            questions: Path) -> None:
    from stemma.evidence.service import EvidenceService

    rec = attach(run_dir, questions)
    receipt = EvidenceService(run_dir).capture_file(rec["path"], "the client's question list")
    assert receipt["status"] == "ok", receipt
