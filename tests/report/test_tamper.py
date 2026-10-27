"""The tamper demo: it changes only a copy, verify catches the change, and a change
verify missed would fail the demo loudly."""

from __future__ import annotations

import hashlib
import io
import sys
from pathlib import Path
from typing import Any

import pytest
from report_fixture import fresh_copy

from stemma.register.derivation import DerivationError
from stemma.report import tamper, verify

needs_sandbox = pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS's")


@pytest.fixture
def run(tmp_path_factory: pytest.TempPathFactory, tmp_path: Path) -> Path:
    dest = tmp_path / "run"
    fresh_copy(tmp_path_factory.getbasetemp(), dest)
    return dest


def digest(folder: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        if p.is_file() and not p.name.endswith(".lock"):
            h.update(str(p.relative_to(folder)).encode() + p.read_bytes())
    return h.hexdigest()


@needs_sandbox
def test_one_byte_of_a_cited_source_is_caught_and_the_original_is_untouched(run: Path) -> None:
    before = digest(run)
    out = io.StringIO()
    assert tamper.demo(run, "source", out=out) == 0
    text = out.getvalue()
    assert "Tampering: one byte of the stored source behind figure 1 of the answer." in text
    assert "'2' became '3'" in text and "counted 312 public" in text
    assert "figures of the answer that rest on this source: 1\n" in text
    assert "Caught. 1 new failure(s)" in text and "the stored bytes changed" in text
    assert "Release: REFUSED." in text
    assert digest(run) == before


@needs_sandbox
def test_a_changed_figure_in_the_log_is_caught(run: Path) -> None:
    out = io.StringIO()
    assert tamper.demo(run, "figure", out=out) == 0
    text = out.getvalue()
    assert "number 212 became 213" in text
    assert "Register log: claim c:" in text and "changed after filing" in text


def test_a_change_verify_misses_fails_the_demo(run: Path, monkeypatch: Any) -> None:
    real = verify.verify

    def blind(run_dir: Path, *a: Any, **k: Any) -> verify.Result:
        r = real(run_dir, rerun_fn=lambda *x, **y: (_ for _ in ()).throw(
            DerivationError("sandbox-exec is not available")))
        for s in r.sections:
            s.failures.clear()
        return r

    monkeypatch.setattr(verify, "verify", blind)
    out = io.StringIO()
    assert tamper.demo(run, "source", out=out) == 1
    assert "NOT CAUGHT" in out.getvalue()


def test_a_receipt_hash_that_is_a_path_writes_nothing_outside(
        run: Path, tmp_path: Path, monkeypatch: Any) -> None:
    """Codex recheck H2: a receipt `sha256` of `/somewhere/outside.txt` was written to."""
    import json

    outside = tmp_path / "outside.txt"
    outside.write_text("212 benches")
    monkeypatch.setattr(verify, "verify", lambda *a, **k: _clean_result())
    for p in (run / "store/receipts").glob("*.json"):
        rec = json.loads(p.read_text())
        rec["sha256"] = str(outside)
        p.chmod(0o644)
        p.write_text(json.dumps(rec))
    assert tamper.main([str(run)]) == 2
    assert outside.read_text() == "212 benches"


def _clean_result() -> verify.Result:
    return verify.Result("x", None, [verify.Section("captures", "Captures")], 0.0,
                         "not released")


def test_a_run_holding_a_link_is_refused(run: Path, tmp_path: Path,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "secret.txt").write_text("outside")
    (run / "analysis/link.py").symlink_to(tmp_path / "secret.txt")
    assert tamper.main([str(run)]) == 2
    assert "a symbolic link" in capsys.readouterr().err


def test_a_run_that_already_fails_verify_is_refused(
        run: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    rec = json.loads(next((run / "store/receipts").glob("*.json")).read_text())
    target = run / "store/bytes" / rec["sha256"]
    target.chmod(0o644)
    target.write_bytes(target.read_bytes() + b" ")
    before = digest(run)
    assert tamper.main([str(run)]) == 2
    assert "already fails verify" in capsys.readouterr().err
    assert digest(run) == before
