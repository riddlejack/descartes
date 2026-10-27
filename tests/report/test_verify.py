"""`stemma verify`: a clean run passes, and every kind of tampering is named and fails."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from report_fixture import fresh_copy

from stemma.register.derivation import DerivationError
from stemma.report import verify
from stemma.report.runread import RunReadError

RUN1 = Path(os.environ.get("STEMMA_RUN1_DIR") or Path.home() / ".private/runs/"
            "stemma2-walking-skeleton/20261026-070412-2b3c98")
needs_sandbox = pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS's")


@pytest.fixture
def run(tmp_path_factory: pytest.TempPathFactory, tmp_path: Path) -> tuple[Path, dict[str, str]]:
    dest = tmp_path / "run"
    ids = fresh_copy(tmp_path_factory.getbasetemp(), dest)
    return dest, ids


def section(result: verify.Result, key: str) -> verify.Section:
    return next(s for s in result.sections if s.key == key)


def receipt(run_dir: Path, rid: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((run_dir / "store/receipts" / f"{rid}.json").read_text())
    return data


@needs_sandbox
def test_a_clean_run_passes_with_plain_counts(run: tuple[Path, dict[str, str]]) -> None:
    run_dir, _ids = run
    result = verify.verify(run_dir)
    assert result.outcome == "PASS", verify.render(result)
    counts = {s.key: (s.done, len(s.failures)) for s in result.sections}
    assert counts == {"captures": (2, 0), "texts": (2, 0), "quotations": (2, 0),
                      "calculations": (1, 0), "log": (3, 0), "fingerprints": (1, 0)}
    text = verify.render(result)
    assert "2 captures re-hashed against their receipts, 0 changed" in text
    assert "1 calculations re-run in the sandbox, 0 differ" in text
    assert "Result: PASS" in text and result.exit_code == 0


@needs_sandbox
def test_one_changed_byte_of_a_stored_source_fails_and_names_it(
        run: tuple[Path, dict[str, str]]) -> None:
    run_dir, ids = run
    sha = receipt(run_dir, ids["r1"])["sha256"]
    path = run_dir / "store/bytes" / sha
    os.chmod(path, 0o644)
    data = bytearray(path.read_bytes())
    data[-3] ^= 1
    path.write_bytes(bytes(data))
    result = verify.verify(run_dir)
    assert result.outcome == "FAIL" and result.exit_code == 1
    fails = section(result, "captures").failures
    assert len(fails) == 1 and ids["r1"] in fails[0] and "stored bytes changed" in fails[0]
    assert "Release: REFUSED" in verify.render(result)


@needs_sandbox
def test_a_changed_figure_in_the_stored_text_breaks_its_quotation(
        run: tuple[Path, dict[str, str]]) -> None:
    run_dir, ids = run
    rec = receipt(run_dir, ids["r1"])
    path = run_dir / "store/text" / f"{rec['text_id']}.txt"
    os.chmod(path, 0o644)
    path.write_text(path.read_text().replace("212", "213"))
    result = verify.verify(run_dir)
    fails = section(result, "quotations").failures
    assert len(fails) == 1 and f"c:{ids['now'][:12]} (cited in the answer)" in fails[0]
    assert "no longer in the stored text" in fails[0]


@needs_sandbox
def test_a_changed_figure_in_the_register_log_shows_as_a_changed_claim(
        run: tuple[Path, dict[str, str]]) -> None:
    run_dir, ids = run
    log = run_dir / "register/log.jsonl"
    lines = log.read_text().splitlines(keepends=True)
    hit = [i for i, ln in enumerate(lines) if '"number":212' in ln]
    assert hit
    lines[hit[0]] = lines[hit[0]].replace('"number":212', '"number":312')
    log.write_text("".join(lines))
    result = verify.verify(run_dir)
    fails = section(result, "log").failures
    assert len(fails) == 1 and ids["now"][:12] in fails[0] and "changed after filing" in fails[0]


@needs_sandbox
def test_a_dropped_log_line_means_the_log_does_not_fold(
        run: tuple[Path, dict[str, str]]) -> None:
    run_dir, _ids = run
    log = run_dir / "register/log.jsonl"
    lines = log.read_text().splitlines(keepends=True)
    log.write_text("".join(lines[:2] + lines[3:]))
    with pytest.raises(RunReadError, match="does not fold"):
        verify.verify(run_dir)
    assert verify.main([str(run_dir)]) == 2
    sec = verify.check_log(run_dir)
    assert sec.failures and "does not fold" in sec.failures[0]


@needs_sandbox
def test_an_edited_script_no_longer_reproduces(run: tuple[Path, dict[str, str]]) -> None:
    run_dir, ids = run
    (run_dir / "analysis/helper.py").write_text("def years_left():\n    return 5\n")
    result = verify.verify(run_dir)
    fails = section(result, "calculations").failures
    assert len(fails) == 1 and ids["proj"][:12] in fails[0]
    assert "now reports 412.0; the claim says 372" in fails[0]


@needs_sandbox
def test_an_edited_frozen_draft_fails_its_fingerprint(run: tuple[Path, dict[str, str]]) -> None:
    run_dir, _ids = run
    path = run_dir / "register/drafts/answer.v1.md"
    os.chmod(path, 0o644)
    path.write_text(path.read_text() + "\nOne more line.\n")
    result = verify.verify(run_dir)
    fails = section(result, "fingerprints").failures
    assert fails == [("submitted draft v1 (register/drafts/answer.v1.md) changed after the "
                     "register fingerprinted it.")]


@needs_sandbox
def test_a_deleted_source_is_missing_not_skipped(run: tuple[Path, dict[str, str]]) -> None:
    run_dir, ids = run
    (run_dir / "store/bytes" / receipt(run_dir, ids["r2"])["sha256"]).unlink()
    fails = section(verify.verify(run_dir), "captures").failures
    assert fails and "stored bytes are missing" in fails[0]


def test_no_sandbox_is_incomplete_never_a_pass(run: tuple[Path, dict[str, str]]) -> None:
    run_dir, _ids = run

    def no_sandbox(*_a: Any, **_k: Any) -> Any:
        raise DerivationError("sandbox-exec is not available on this machine")

    result = verify.verify(run_dir, rerun_fn=no_sandbox)
    assert result.outcome == "INCOMPLETE" and result.exit_code == 3
    assert "could not run here" in verify.render(result)


def test_a_symlinked_helper_is_refused_not_followed(
        run: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    run_dir, _ids = run
    outside = tmp_path / "outside.py"
    outside.write_text("def years_left():\n    return 4\n")
    helper = run_dir / "analysis/helper.py"
    helper.unlink()
    helper.symlink_to(outside)
    with pytest.raises(DerivationError, match="not a plain file"):
        verify.code_bundle(run_dir, "analysis/proj.py")


def test_the_bundle_holds_only_what_the_script_imports(run: tuple[Path, dict[str, str]]) -> None:
    run_dir, _ids = run
    code = verify.code_bundle(run_dir, "analysis/proj.py")
    assert sorted(code.files) == ["helper.py", "proj.py"]


@pytest.mark.parametrize("script", ["analysis/../run.json", "/etc/passwd", "proj.py",
                                    "analysis/x.txt"])
def test_a_script_outside_analysis_is_refused(run: tuple[Path, dict[str, str]],
                                              script: str) -> None:
    with pytest.raises(DerivationError):
        verify.code_bundle(run[0], script)


def test_a_folder_that_is_not_a_run_is_a_loud_error(tmp_path: Path) -> None:
    assert verify.main([str(tmp_path)]) == 2


@needs_sandbox
@pytest.mark.skipif(not (RUN1 / "run.json").exists(), reason="run 1's folder is not here")
def test_run_one_verifies_in_under_a_minute_and_names_its_differences() -> None:
    """Run 1's real folder: every capture, quotation and fingerprint holds; the
    calculations whose scripts were edited after filing are named (a real finding)."""
    result = verify.verify(RUN1)
    assert result.seconds < 60
    for key in ("captures", "texts", "quotations", "log", "fingerprints"):
        s = section(result, key)
        assert s.done > 0 and not s.failures, (key, s.failures[:3])
    calc = section(result, "calculations")
    assert calc.done > 100 and not calc.not_run
    assert all(("now reports" in f or "no result" in f) for f in calc.failures)
