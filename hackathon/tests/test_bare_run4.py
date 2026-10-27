"""The bare arm for run 4: the Stemma arm's own task.json, a wall-time limit, a
canary-only start, and a folder the leakage audit reads completely."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hackathon.headtohead import bare
from hackathon.tests.test_headtohead import GOOD, INIT, _env, _Proc, _Sessions
from stemma.audit import leakage

AS_OF = "2026-10-01"
TASK: dict[str, Any] = {
    "question": "As of 30 September 2026, should a holder of Example Corp's 7% notes due "
                "2031 keep holding them through 31 December 2026?",
    "intake": {"Who asked for it, and who will use it?": "a fixed-income portfolio manager",
               "What will it be used for?": "a decision memo with explicit calls"},
    "as_of": AS_OF,
}


@pytest.fixture
def ready(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from stemma.driver import clean

    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(clean, "check_canary", lambda init, exp: [])


def _task_file(tmp_path: Path, **over: Any) -> Path:
    p = tmp_path / "task.json"
    p.write_text(json.dumps({**TASK, **over}))
    return p


def _run(tmp_path: Path, sessions: Any, **kw: Any) -> Path:
    t = bare.task_from_file(_task_file(tmp_path))
    return bare.launch(tmp_path / "out", t, popen=sessions, version="2.1.308 (Claude Code)",
                       **kw)


def test_the_task_is_the_stemma_arms_own_file(tmp_path: Path) -> None:
    t = bare.task_from_file(_task_file(tmp_path, criteria=["Give a call on each event."]))
    prompt = bare.prompt_for(t)
    assert TASK["question"] in prompt
    for k, v in TASK["intake"].items():
        assert f"- {k} {v}" in prompt
    assert "Use only information published before 2026-10-01 00:00 UTC" in prompt
    assert "- C1: Give a call on each event." in prompt
    with pytest.raises(bare.BareError, match="unknown keys"):
        bare.task_from_file(_task_file(tmp_path, notes="x"))


def test_the_folder_is_one_the_leakage_audit_reads_completely(
        tmp_path: Path, ready: None) -> None:
    sessions = _Sessions()
    folder = _run(tmp_path, sessions)
    run = json.loads((folder / "run.json").read_text())
    assert run["as_of"] == AS_OF and run["run_id"] == folder.name
    assert run["harness"]["config_dir"] == str(tmp_path / "config")
    launched = [json.loads(x) for x in (folder / "events.jsonl").read_text().splitlines()]
    starts = [e for e in launched if e["event"] == "session_launched"]
    assert [e["session"] for e in starts] == ["scope-canary", "bare"]
    for e, argv in zip(starts, sessions.calls, strict=True):
        assert f"--session-id={e['session_id']}" in argv
        stream = folder / ".stemma" / "sessions" / f"{e['session']}.stream.jsonl"
        assert json.loads(stream.read_text().splitlines()[0])["session_id"] == e["session_id"]
        assert (folder / "transcripts" / f"{e['session_id']}.jsonl").is_file()
    ends = [e for e in launched if e["event"] == "session_ended"]
    assert [e["transcript"] for e in ends] == [
        f"transcripts/{e['session_id']}.jsonl" for e in starts]

    targets = tmp_path / "targets.json"
    targets.write_text(json.dumps({"as_of": AS_OF, "figures": [
        {"id": "x", "value": "123.456", "scale": "million"}]}))
    report = leakage.audit(folder, leakage.date.fromisoformat(AS_OF),
                           leakage.load_targets(targets))
    assert report.complete and report.gaps == []
    assert report.read["session_stream"] == 2 and report.read["transcript"] == 2
    # Run-time stamps (the folder's run id, the session times) are not reported; the
    # only hits are the task's own words ("through 31 December 2026") in the prompt.
    assert report.hits and {(h.found, h.origin) for h in report.hits} == {
        ("31 December 2026", "prompt")}


def test_the_session_and_the_record_cannot_rewrite_the_record(tmp_path: Path) -> None:
    folder = tmp_path / "f"
    st = bare.settings_extra(folder, tmp_path / "config", bare.SCOPE_PYTHON, AS_OF)
    for p in (".stemma", "transcripts", "events.jsonl", "run.json"):
        assert f"Write(/{folder / p}/**)" in st["permissions"]["deny"], p
        assert str(folder / p) in st["sandbox"]["filesystem"]["denyWrite"], p


def test_canary_only_never_sends_the_task(tmp_path: Path, ready: None) -> None:
    sessions = _Sessions()
    folder = _run(tmp_path, sessions, canary_only=True)
    assert len(sessions.calls) == 1
    assert "safety check" in sessions.calls[0][sessions.calls[0].index("-p") + 1]
    assert all(TASK["question"] not in " ".join(argv) for argv in sessions.calls)
    out = json.loads((folder / "outcome.json").read_text())
    assert out["canary_only"] is True and out["eligible"] is False
    assert out["canary_transcript"].startswith("transcripts/")
    assert not (folder / "answer.md").exists()
    assert not (folder / ".stemma" / "sessions" / "bare.stream.jsonl").exists()


def test_a_canary_whose_transcript_is_not_kept_refuses_the_arm(
        tmp_path: Path, ready: None) -> None:
    sessions = _Sessions()
    sessions.transcripts = False
    with pytest.raises(bare.BareError, match="canary's transcript was not found"):
        _run(tmp_path, sessions)
    assert len(sessions.calls) == 1


def test_a_session_on_another_id_fails_its_canary(tmp_path: Path, ready: None) -> None:
    class Elsewhere(_Sessions):
        def __call__(self, argv: list[str], **k: Any) -> _Proc:
            super().__call__(argv, **k)
            return _Proc([INIT, GOOD])  # session id "s1", not the one launched

    with pytest.raises(bare.BareError, match="not the '.*' it was launched with"):
        _run(tmp_path, Elsewhere())


def test_the_wall_time_limit_stops_the_real_session(
        tmp_path: Path, ready: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bare, "POLL_S", 0.05)

    class Slow(_Sessions):
        def __call__(self, argv: list[str], **k: Any) -> _Proc:
            proc = super().__call__(argv, **k)
            if "safety check" in argv[argv.index("-p") + 1]:
                return proc
            sid = next(a.split("=", 1)[1] for a in argv if a.startswith("--session-id="))
            self.proc = _Proc([], hang=True)
            hang = self.proc._hang

            def started() -> Any:  # the init line, then nothing until stopped
                yield json.dumps({**INIT, "session_id": sid}) + "\n"
                yield from hang()

            self.proc.stdout = started()
            return self.proc

    sessions = Slow()
    with pytest.raises(bare.BareError, match="ran past 0 s"):
        _run(tmp_path, sessions, max_s=0.3)
    assert sessions.proc.terminated
    folder = next(p for p in (tmp_path / "out").iterdir() if p.is_dir())
    assert json.loads((folder / "outcome.json").read_text())["eligible"] is False


def test_the_cli_takes_the_task_the_limit_and_canary_only(
        tmp_path: Path, ready: None, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    seen: dict[str, Any] = {}

    def fake(root: Path, t: dict[str, Any], **kw: Any) -> Path:
        seen.update(t=t, **kw)
        return root

    monkeypatch.setattr(bare, "launch", fake)
    code = bare.main(["--task", str(_task_file(tmp_path)), "--max-s", "9000",
                      "--canary-only", "--out", str(tmp_path / "o")])
    assert code == 0 and seen["max_s"] == 9000 and seen["canary_only"] is True
    assert seen["t"]["question"] == TASK["question"] and seen["t"]["as_of"] == AS_OF
    assert "stopped before the task" in capsys.readouterr().out
    assert bare.main(["--task", "t.json", "--question-file", "q.json"]) == 2


def test_a_permission_notice_with_a_text_message_does_not_break_the_scope_check() -> None:
    """Seen live (run-4 dry run): the harness's `permission_denied` system line carries
    `message` as a string, not an object."""
    notice = {"type": "system", "subtype": "permission_denied",
              "message": "Claude requested permissions to read from /x/marker.txt"}
    stream = json.dumps(notice) + "\n"
    problems = bare.scope_problems(stream, Path("/x/marker.txt"), "tok", Path("/f/c.txt"),
                                   "ctl", AS_OF)
    assert "the canary did not try the Read tool on the marker" in problems


@pytest.mark.parametrize("receipt,ok", [
    # Seen live (run-4 dry run): example.com's archived copy is short, so its status is
    # short_text; the broker still reached the archive and stored the bytes.
    ({"kind": "archive", "status": "short_text", "sha256": "7d" * 32, "bytes_len": 713,
      "archive": {"served_timestamp": "20260930235048"}}, True),
    ({"kind": "archive", "status": "ok", "sha256": "ab" * 32, "bytes_len": 900,
      "archive": {"served_timestamp": "20260915000000"}}, True),
    ({"kind": "archive", "status": "archive_unavailable", "sha256": None,
      "archive": {}}, False),
    ({"kind": "archive", "status": "ok", "sha256": "ab" * 32, "bytes_len": 900,
      "archive": {"served_timestamp": "20261001000000"}}, False),  # not before the date
    ({"kind": "web", "status": "ok", "sha256": "ab" * 32, "bytes_len": 900}, False)])
def test_the_broker_control_is_an_archived_copy_dated_before_the_cutoff(
        receipt: dict[str, Any], ok: bool) -> None:
    assert bare._archived(json.dumps(receipt), AS_OF) is ok


def test_an_attachment_is_copied_read_only_named_and_recorded(
        tmp_path: Path, ready: None) -> None:
    import hashlib
    import stat

    brief = tmp_path / "brief.md"
    brief.write_bytes(b"# Brief\n\nE1: an invented event.\n")
    t = bare.task_from_file(_task_file(tmp_path, attachments=[str(brief)]))
    assert t["attachments"] == [str(brief.resolve())]
    assert ("The user attached task/attachments/brief.md, in your working folder."
            in bare.prompt_for(t))
    sessions = _Sessions()
    folder = bare.launch(tmp_path / "out", t, popen=sessions,
                         version="2.1.308 (Claude Code)")
    copy = folder / "task" / "attachments" / "brief.md"
    assert copy.read_bytes() == brief.read_bytes()
    assert not copy.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    digest = hashlib.sha256(brief.read_bytes()).hexdigest()
    for record in ("run.json", "launch.json"):
        got = json.loads((folder / record).read_text())["attachments"]
        assert got == [{"path": "task/attachments/brief.md", "source": str(brief.resolve()),
                        "sha256": digest, "bytes": str(len(brief.read_bytes()))}]
    real = sessions.calls[-1][sessions.calls[-1].index("-p") + 1]
    assert "task/attachments/brief.md" in real  # the real session is told of it
    st = json.loads((folder / "launch.json").read_text())["settings_extra"]
    assert f"Write(/{folder / 'task'}/**)" in st["permissions"]["deny"]


def test_the_launcher_gives_its_preregistered_task(tmp_path: Path) -> None:
    brief = tmp_path / "brief.md"
    brief.write_text("E1\n")
    launcher = tmp_path / "runX_demo.py"
    launcher.write_text(
        "TASK = " + repr({**TASK, "attachments": [str(brief)]}) + "\n"
        "def task_with(question_file):\n    return dict(TASK)\n")
    t = bare.task_from_launcher(launcher)
    assert t["question"] == TASK["question"] and t["as_of"] == AS_OF
    assert t["attachments"] == [str(brief.resolve())]
    assert bare.main(["--launcher", str(launcher), "--task", "t.json"]) == 2
