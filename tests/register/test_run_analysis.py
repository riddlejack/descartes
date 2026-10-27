"""`run_analysis`: the same bundle and sandbox as the filing re-run, for authors at work.
These run the real sandbox-exec re-run; the last test drives the MCP server over stdio."""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any

import anyio
import pytest
from test_filing import lookup_ab
from test_mcp_server import session_calls, stamp
from test_store import LEAD, WORKER, FakeEvidence, claim

from stemma.register import derivation
from stemma.register.analysis_run import run_analysis, run_for
from stemma.register.filing import Refused
from stemma.register.store import Caller, Register


def no_run(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("the path must be refused before anything runs")


def no_lookup(i: str) -> tuple[str, float]:
    raise AssertionError("no lookup expected")


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def test_returns_the_exact_result_and_stdout(tmp_path: Path) -> None:
    write(tmp_path, {"analysis/calc.py": (
        "import helper\nfrom stemma.analysis import inputs, result\n"
        "v = inputs('aaaaaaaaaaaa', 'bbbbbbbbbbbb')\nprint('ratio of', v)\n"
        "result(helper.f(v['aaaaaaaaaaaa'], v['bbbbbbbbbbbb']))\n"),
        "analysis/helper.py": "def f(a, b):\n    return a / b + 0.1 + 0.2\n"})
    out = run_analysis(tmp_path, "analysis/calc.py", ["a" * 12, "b" * 12], None, lookup_ab,
                       None)
    assert out["result"] == 0.75 + 0.1 + 0.2  # the float exactly as computed
    assert out["exit_code"] == 0
    assert out["stdout"].startswith("ratio of {'aaaaaaaaaaaa': 3.0")
    assert out["stdout_truncated"] is False
    assert out["bundle_files"] == ["calc.py", "helper.py"]
    assert out["inputs"] == ["a" * 64, "b" * 64]


def test_an_integer_result_comes_back_as_written(tmp_path: Path) -> None:
    write(tmp_path, {"analysis/n.py": "from stemma.analysis import result\nresult(2**60 + 1)\n"})
    out = run_analysis(tmp_path, "analysis/n.py", None, None, no_lookup, None)
    assert out["result"] == 2**60 + 1 and isinstance(out["result"], int)


def test_no_result_and_a_failure_are_reported_not_hidden(tmp_path: Path) -> None:
    write(tmp_path, {"analysis/look.py": "print('just looking')\n",
                     "analysis/bad.py": "raise ValueError('boom')\n"})
    out = run_analysis(tmp_path, "analysis/look.py", None, None, no_lookup, None)
    assert out["result"] is None and out["exit_code"] == 0
    assert out["stdout"] == "just looking\n"
    bad = run_analysis(tmp_path, "analysis/bad.py", None, None, no_lookup, None)
    assert bad["result"] is None and bad["exit_code"] != 0
    assert "ValueError: boom" in bad["stderr_tail"]


def test_stdout_is_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(derivation, "STDOUT_MAX_BYTES", 10)
    write(tmp_path, {"analysis/loud.py": "print('x' * 100)\n"})
    out = run_analysis(tmp_path, "analysis/loud.py", None, None, no_lookup, None)
    assert out["stdout"] == "x" * 10 and out["stdout_truncated"] is True


def test_the_time_limit_holds(tmp_path: Path) -> None:
    write(tmp_path, {"analysis/slow.py": "import time\ntime.sleep(30)\n"})
    with pytest.raises(Refused, match="ran past its 2 s limit"):
        run_analysis(tmp_path, "analysis/slow.py", None, None, no_lookup, None, time_limit_s=2)


def test_the_script_cannot_read_the_run_folder_or_write_outside(tmp_path: Path) -> None:
    secret = tmp_path / "register" / "secret.txt"
    write(tmp_path, {"register/secret.txt": "not for scripts"})
    victim = tmp_path / "victim.txt"
    write(tmp_path, {"analysis/peek.py": (
        "import pathlib\nfrom stemma.analysis import result\nn = 0\n"
        f"try:\n    pathlib.Path({str(secret)!r}).read_text(); n += 1\nexcept OSError: pass\n"
        f"try:\n    pathlib.Path({str(victim)!r}).write_text('x'); n += 10\n"
        "except OSError: pass\nresult(n)\n")})
    out = run_analysis(tmp_path, "analysis/peek.py", None, None, no_lookup, None)
    assert out["result"] == 0
    assert not victim.exists()


def _hostile(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    write(outside, {"calc.py": "from stemma.analysis import result\nresult(666)\n"})
    write(tmp_path, {"analysis/calc.py": "from stemma.analysis import result\nresult(1)\n",
                     "analysis/notes.txt": "x\n", "secret.py": "result = 2\n"})
    (tmp_path / "analysis" / "link.py").symlink_to(outside / "calc.py")
    (tmp_path / "analysis" / "linkdir").symlink_to(outside)
    nfc = unicodedata.normalize("NFC", "café.py")
    write(tmp_path, {f"analysis/{nfc}": "from stemma.analysis import result\nresult(3)\n"})


HOSTILE = [
    "/etc/passwd", "ABSOLUTE", "../analysis/calc.py", "analysis/../secret.py",
    "analysis/sub/../calc.py", "./analysis/calc.py", "analysis//calc.py", "analysis/./calc.py",
    "Analysis/calc.py", "ANALYSIS/calc.py", "analysis/CALC.py", "analysis/Calc.py",
    unicodedata.normalize("NFD", "analysis/café.py"), "analysis/link.py",
    "analysis/linkdir/calc.py", "analysis/notes.txt", "analysis", "analysis/", "secret.py",
    "analysis/calc.py\x00", "analysis\\calc.py", "",
]


@pytest.mark.parametrize("given", HOSTILE)
def test_paths_outside_analysis_are_refused(tmp_path: Path, given: str) -> None:
    _hostile(tmp_path)
    if given == "ABSOLUTE":
        given = str(tmp_path / "analysis" / "calc.py")
    with pytest.raises(Refused, match="run_analysis"):
        run_analysis(tmp_path, given, None, None, no_lookup, None, rerun_fn=no_run)


def test_the_real_names_still_run(tmp_path: Path) -> None:
    _hostile(tmp_path)
    assert run_analysis(tmp_path, "analysis/calc.py", None, None, no_lookup, None)["result"] == 1
    nfc = unicodedata.normalize("NFC", "analysis/café.py")
    assert run_analysis(tmp_path, nfc, None, None, no_lookup, None)["result"] == 3


def test_inputs_resolve_through_the_register_and_only_authors_run(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    reg = Register(run, evidence=FakeEvidence())
    cid = reg.file_claim(WORKER, claim())["claim_id"]
    write(run, {"analysis/x.py": (
        f"from stemma.analysis import inputs, result\nresult(inputs('{cid[:12]}')"
        f"['{cid[:12]}'] / 1e9)\n")})
    for who in (WORKER, LEAD):
        assert run_for(reg, who, "analysis/x.py", [cid[:12]], None)["result"] == 1.2
    with pytest.raises(Refused, match="no single claim"):
        run_for(reg, WORKER, "analysis/x.py", ["f" * 12], None)
    checker = Caller("stemma:claim-checker", None, "s-c")
    with pytest.raises(Refused, match="do not run analysis scripts"):
        run_for(reg, checker, "analysis/x.py", [cid[:12]], None)


def test_the_mcp_tool_runs_for_a_worker_and_refuses_others(tmp_path: Path) -> None:
    run = tmp_path / "run"
    write(run, {"analysis/calc.py": "from stemma.analysis import result\nprint('hi')\nresult(5)\n"})
    stamp(run, "toolu_w", "stemma:worker")
    stamp(run, "toolu_c", "stemma:claim-checker")
    calls: list[tuple[str, dict[str, Any], str | None]] = [
        ("run_analysis", {"script": "analysis/calc.py"}, "toolu_w"),
        ("run_analysis", {"script": "analysis/calc.py"}, "toolu_c"),
        ("run_analysis", {"script": "analysis/calc.py"}, None),
        ("run_analysis", {"script": "../run/analysis/calc.py"}, "toolu_w"),
    ]
    results = anyio.run(session_calls, run, calls)
    ok, text = results[0]
    assert not ok and '"result":5' in text.replace(" ", "") and "hi" in text
    assert results[1][0] and "do not run analysis scripts" in results[1][1]
    assert results[2][0] and "no harness identity" in results[2][1]
    assert results[3][0] and "not a plain path in analysis/" in results[3][1]


# --- The lead reads claim inputs only (Codex recheck P1) --------------------------------

PROBE = "from stemma.analysis import document\nprint(document('r1'))\n"


def test_the_lead_cannot_read_a_document_through_a_script(tmp_path: Path) -> None:
    from test_store import TEXT

    run = tmp_path / "run"
    run.mkdir()
    reg = Register(run, evidence=FakeEvidence())
    write(run, {"analysis/peek.py": PROBE})
    assert LEAD.agent_type == "stemma:lead" and LEAD.agent_id is None
    with pytest.raises(Refused, match="lead sessions run analysis on claim inputs only"):
        run_for(reg, LEAD, "analysis/peek.py", None, ["r1"])
    out = run_for(reg, LEAD, "analysis/peek.py", None, None)
    assert out["exit_code"] != 0 and out["result"] is None
    assert "AnalysisError: receipt r1 is not among this script's documents" in out["stderr_tail"]
    assert TEXT not in out["stdout"] and TEXT not in out["stderr_tail"]
    # The same script works for a worker who names the document.
    worker = run_for(reg, WORKER, "analysis/peek.py", None, ["r1"])
    assert worker["exit_code"] == 0 and worker["stdout"] == TEXT + "\n"


# --- LIVE: the real sandbox on this machine ----------------------------------------------

def _sandbox_applies() -> bool:
    import subprocess

    if not Path(derivation.SANDBOX_EXEC).exists():
        return False
    p = subprocess.run([derivation.SANDBOX_EXEC, "-p", "(version 1)(allow default)",
                        "/usr/bin/true"], capture_output=True, check=False,
                       timeout=30)  # resource limit: seconds to probe
    return p.returncode == 0


@pytest.mark.skipif(not _sandbox_applies(), reason="sandbox-exec cannot apply a profile here")
def test_live_network_and_outside_writes_fail_and_the_run_says_so(tmp_path: Path) -> None:
    import socket

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(0.5)
    port = listener.getsockname()[1]
    victim = tmp_path / "victim.txt"
    write(tmp_path, {"analysis/escape.py": (
        "import socket, pathlib\nfrom stemma.analysis import result\nwon = 0\n"
        "try:\n"
        f"    socket.create_connection(('127.0.0.1', {port}), timeout=3)\n"
        "    won += 1\n    print('network: connected')\n"
        "except OSError as e:\n    print('network: blocked', type(e).__name__)\n"
        "try:\n"
        f"    pathlib.Path({str(victim)!r}).write_text('x')\n"
        "    won += 10\n    print('write: done')\n"
        "except OSError as e:\n    print('write: blocked', type(e).__name__)\n"
        "pathlib.Path('scratch.txt').write_text('inside is fine')\n"
        "print('scratch: written')\nresult(won)\n")})
    try:
        out = run_analysis(tmp_path, "analysis/escape.py", None, None, no_lookup, None)
        with pytest.raises(TimeoutError):
            listener.accept()  # nothing ever reached the listener
    finally:
        listener.close()
    assert out["exit_code"] == 0 and out["result"] == 0
    assert "network: blocked" in out["stdout"] and "write: blocked" in out["stdout"]
    assert "scratch: written" in out["stdout"]
    assert not victim.exists()
