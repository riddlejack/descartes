"""`stemma verify`: a clean run passes, and every kind of tampering is named and fails."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from report_fixture import fresh_copy, release, seal

from stemma.register.derivation import DerivationError
from stemma.report import verify

# Run 1's recorded folder, kept outside the repository; these tests skip without it.
RUN1 = Path(os.environ.get("STEMMA_RUN1_DIR") or "/nonexistent-stemma-run1")
needs_sandbox = pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS's")


@pytest.fixture
def run(tmp_path_factory: pytest.TempPathFactory, tmp_path: Path) -> tuple[Path, dict[str, str]]:
    dest = tmp_path / "run"
    ids = fresh_copy(tmp_path_factory.getbasetemp(), dest)
    return dest, ids


def checks_sealed(run_dir: Path) -> bool:
    man = json.loads((run_dir / "release/manifest.json").read_text())
    return isinstance(man.get("checks"), dict)


def section(result: verify.Result, key: str) -> verify.Section:
    return next(s for s in result.sections if s.key == key)


def receipt(run_dir: Path, rid: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((run_dir / "store/receipts" / f"{rid}.json").read_text())
    return data


@needs_sandbox
def test_an_unsealed_clean_run_is_unsealed_never_pass(run: tuple[Path, dict[str, str]]) -> None:
    run_dir, _ids = run
    result = verify.verify(run_dir)
    assert result.outcome == "UNSEALED" and result.exit_code == 4, verify.render(result)
    counts = {s.key: (s.done, len(s.failures)) for s in result.sections}
    assert counts == {"seal": (0, 0), "captures": (2, 0), "texts": (2, 0),
                      "quotations": (2, 0), "calculations": (1, 0), "log": (3, 0),
                      "fingerprints": (1, 0)}
    text = verify.render(result)
    assert "2 captures re-hashed against their receipts, 0 changed" in text
    assert "1 calculations re-run in the sandbox, 0 differ" in text
    assert "Result: UNSEALED" in text and "PASS" not in text


@needs_sandbox
def test_a_sealed_run_passes_and_a_pinned_digest_is_checked(
        run: tuple[Path, dict[str, str]]) -> None:
    run_dir, _ids = run
    release(run_dir)
    digest = seal(run_dir)
    result = verify.verify(run_dir)
    assert checks_sealed(run_dir)
    assert result.outcome == "PASS" and result.seal.state == "sealed", verify.render(result)
    assert "against the run's own manifest (not pinned)" in verify.render(result)
    pinned = verify.verify(run_dir, expect=digest)
    assert pinned.outcome == "PASS" and pinned.seal.state == "pinned"
    assert result.release_state == "released"
    wrong = verify.verify(run_dir, expect="0" * 64)
    assert wrong.outcome == "FAIL" and "not to the pinned" in section(wrong, "seal").failures[0]
    assert verify.main([str(run_dir), "--expect", digest]) == 0
    assert verify.main([str(run_dir), "--expect", "nothex"]) == 2


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
    result = verify.verify(run_dir)
    assert result.outcome == "FAIL" and result.exit_code == 1
    assert any("does not fold" in f for _l, f in result.failures)
    assert verify.main([str(run_dir)]) == 1


@pytest.mark.parametrize("tail", [b"", b'{"tx": 99', b"garbage"])
def test_an_unterminated_log_tail_fails(run: tuple[Path, dict[str, str]], tail: bytes) -> None:
    run_dir, _ids = run
    log = run_dir / "register/log.jsonl"
    data = log.read_bytes()
    log.write_bytes(data[:-1] + tail if not tail else data + tail)
    result = verify.verify(run_dir)
    assert result.outcome == "FAIL"
    assert any("unterminated tail" in f or "no newline" in f for _l, f in result.failures)


@needs_sandbox
@pytest.mark.parametrize("change", ["drop_last", "verdict", "reorder"])
def test_a_sealed_log_rewritten_in_any_way_fails(run: tuple[Path, dict[str, str]],
                                                 change: str) -> None:
    """Codex probes: dropping the last transaction, changing a value in a fact, and
    reordering transactions with their numbers adjusted all PASSED before the seal."""
    run_dir, _ids = run
    seal(run_dir)
    log = run_dir / "register/log.jsonl"
    lines = log.read_bytes().splitlines(keepends=True)
    if change == "drop_last":
        lines = lines[:-1]
    elif change == "verdict":
        i = next(i for i, ln in enumerate(lines) if b'"draft_submitted"' in ln)
        lines[i] = lines[i].replace(b'"version":1', b'"version":2', 1)
    else:
        a, b = json.loads(lines[-2]), json.loads(lines[-1])
        a["tx"], b["tx"] = b["tx"], a["tx"]
        for ev in a["events"] + b["events"]:
            ev["tx"] = a["tx"] if ev in a["events"] else b["tx"]
        lines[-2:] = [(json.dumps(b, separators=(",", ":")) + "\n").encode(),
                      (json.dumps(a, separators=(",", ":")) + "\n").encode()]
    log.write_bytes(b"".join(lines))
    result = verify.verify(run_dir)
    assert result.outcome == "FAIL", verify.render(result)


@needs_sandbox
def test_a_same_length_change_to_unquoted_text_is_caught_by_the_seal(
        run: tuple[Path, dict[str, str]]) -> None:
    run_dir, ids = run
    rec = receipt(run_dir, ids["r1"])
    path = run_dir / "store/text" / f"{rec['text_id']}.txt"
    unsealed_before = verify.verify(run_dir).outcome
    seal(run_dir)
    os.chmod(path, 0o644)
    path.write_text(path.read_text().replace("from 180", "from 999"))
    result = verify.verify(run_dir)
    assert unsealed_before == "UNSEALED"
    assert section(result, "quotations").failures == []
    assert [f for f in section(result, "seal").failures if "store/text/" in f]


@needs_sandbox
def test_a_script_swapped_for_a_constant_and_added_or_removed_files_fail(
        run: tuple[Path, dict[str, str]]) -> None:
    run_dir, ids = run
    seal(run_dir)
    (run_dir / "analysis/proj.py").write_text(
        "from stemma.analysis import result\nresult(372)\n")
    (run_dir / "draft/answer.v2.md").write_text("A new working draft.\n")
    rec = receipt(run_dir, ids["r2"])
    (run_dir / "store/receipts" / f"{ids['r2']}.json").unlink()
    result = verify.verify(run_dir)
    assert section(result, "calculations").failures == []  # the number still matches
    fails = section(result, "seal").failures
    def named(path: str, word: str) -> bool:
        return any(path in f and word in f for f in fails)

    assert named("analysis/proj.py", "changed") and named("draft/answer.v2.md", "added")
    assert named(f"store/receipts/{ids['r2']}.json", "removed")
    assert rec["receipt_id"] == ids["r2"]


def test_a_deliverable_added_to_an_unreleased_run_is_not_released(
        run: tuple[Path, dict[str, str]]) -> None:
    from stemma.report import runread

    run_dir, _ids = run
    (run_dir / "deliverable").mkdir()
    (run_dir / "deliverable/answer.md").write_text("# Anything at all\n")
    r = runread.load(run_dir)
    assert r.release.state == "not_released" and r.deliverable.kind == "submitted_draft"
    assert "no release" in r.release.detail


def test_a_released_deliverable_changed_afterwards_is_a_mismatch(
        run: tuple[Path, dict[str, str]]) -> None:
    from stemma.report import runread

    run_dir, _ids = run
    release(run_dir)
    assert runread.load(run_dir).release.state == "released"
    (run_dir / "deliverable/answer.md").write_text("# Changed\n")
    r = runread.load(run_dir)
    assert r.release.state == "mismatch" and r.deliverable.kind == "submitted_draft"


def test_a_release_block_withdraws_the_release(run: tuple[Path, dict[str, str]]) -> None:
    from stemma.register import checks_api
    from stemma.report import runread

    run_dir, _ids = run
    release(run_dir)
    checks_api.block_release(run_dir, "a checking session ran on the wrong model")
    r = runread.load(run_dir)
    assert r.release.state == "withdrawn" and "wrong model" in r.release.detail


def test_malformed_receipts_are_named_failures_not_crashes(
        run: tuple[Path, dict[str, str]]) -> None:
    run_dir, ids = run
    p = run_dir / "store/receipts" / f"{ids['r1']}.json"
    rec = json.loads(p.read_text())
    rec["flagged_ranges"] = [{}]
    os.chmod(p, 0o644)
    p.write_text(json.dumps(rec))
    q = run_dir / "store/receipts" / f"{ids['r2']}.json"
    os.chmod(q, 0o644)
    q.write_text("[" * 100_000 + "]" * 100_000)
    result = verify.verify(run_dir, rerun_fn=lambda *a, **k: (_ for _ in ()).throw(
        DerivationError("sandbox-exec is not available")))
    fails = [f for _l, f in result.failures]
    assert any("flagged_ranges is not a list" in f for f in fails)
    assert any(ids["r2"] in f and "not JSON" in f for f in fails)
    assert result.exit_code == 1


def test_a_fifo_or_symlinked_source_is_refused_not_read(
        run: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    run_dir, ids = run
    sha = receipt(run_dir, ids["r1"])["sha256"]
    path = run_dir / "store/bytes" / sha
    path.unlink()
    os.mkfifo(path)
    rec = receipt(run_dir, ids["r2"])
    text = run_dir / "store/text" / f"{rec['text_id']}.txt"
    outside = tmp_path / "secret.txt"
    outside.write_text("SECRET OUTSIDE THE RUN")
    text.unlink()
    text.symlink_to(outside)
    result = verify.verify(run_dir, rerun_fn=lambda *a, **k: (_ for _ in ()).throw(
        DerivationError("sandbox-exec is not available")))
    caps = section(result, "captures").failures
    assert any("not a plain file" in f for f in caps)
    assert any("link" in f for f in section(result, "texts").failures)
    assert "SECRET" not in json.dumps(result.as_json())


def test_unsafe_fingerprint_paths_are_refused_before_opening(
        run: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    from stemma.report import runread

    run_dir, _ids = run
    r = runread.load(run_dir)
    r.run_facts["record_frozen"] = [
        {"branch_id": "b1", "round": 1, "snapshot": "../outside.md", "sha256": "a" * 64},
        {"branch_id": "b1", "round": 2, "snapshot": "/etc/passwd", "sha256": "a" * 64}]
    fails = verify.check_fingerprints(r).failures
    assert len(fails) == 2 and all("not a path inside the run folder" in f
                                   or "plain path" in f for f in fails)


@needs_sandbox
def test_verify_writes_nothing_and_works_on_a_read_only_copy(
        run: tuple[Path, dict[str, str]]) -> None:
    import subprocess

    run_dir, _ids = run
    seal(run_dir)
    for lock in run_dir.rglob("*.lock"):
        lock.unlink()
    before = sorted(p.relative_to(run_dir).as_posix() for p in run_dir.rglob("*"))
    subprocess.run(["chmod", "-R", "a-w", str(run_dir)], check=True)
    try:
        result = verify.verify(run_dir)
    finally:
        subprocess.run(["chmod", "-R", "u+w", str(run_dir)], check=True)
    after = sorted(p.relative_to(run_dir).as_posix() for p in run_dir.rglob("*"))
    assert result.outcome == "PASS", verify.render(result)
    assert after == before


@needs_sandbox
def filed_bundle(run_dir: Path, cid: str) -> str | None:
    """The bundle SHA-256 the register stored with a claim (v0.4), or None (earlier)."""
    from stemma.report import runread

    sha = (runread.load(run_dir).claims[cid].get("derivation") or {}).get("bundle_sha256")
    return sha if isinstance(sha, str) else None


@needs_sandbox
def test_an_edited_working_script_is_reported_and_the_filed_copy_reruns(
        run: tuple[Path, dict[str, str]]) -> None:
    """With a stored bundle (v0.4), the filed copy is re-run, so the claim still
    reproduces, and the edit of the working copy in analysis/ is reported."""
    run_dir, ids = run
    (run_dir / "analysis/helper.py").write_text("def years_left():\n    return 5\n")
    result = verify.verify(run_dir)
    calc = section(result, "calculations")
    assert filed_bundle(run_dir, ids["proj"]) is not None
    assert calc.failures == []
    assert "1 re-run from the copy filed with the claim" in calc.extra


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
    with pytest.raises(DerivationError):
        verify.bundle_for(run_dir, "analysis/proj.py")


def test_the_bundle_holds_only_what_the_script_imports(run: tuple[Path, dict[str, str]]) -> None:
    run_dir, _ids = run
    code = verify.bundle_for(run_dir, "analysis/proj.py")
    assert sorted(code.files) == ["helper.py", "proj.py"]


@pytest.mark.parametrize("script", ["analysis/../run.json", "/etc/passwd", "proj.py",
                                    "analysis/x.txt"])
def test_a_script_outside_analysis_is_refused(run: tuple[Path, dict[str, str]],
                                              script: str) -> None:
    with pytest.raises(DerivationError):
        verify.bundle_for(run[0], script)


def test_a_folder_that_is_not_a_run_is_a_loud_error(tmp_path: Path) -> None:
    assert verify.main([str(tmp_path)]) == 2


@needs_sandbox
@pytest.mark.skipif(not (RUN1 / "run.json").exists(),
                    reason="set STEMMA_RUN1_DIR to run 1's folder")
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


@needs_sandbox
def test_a_memo_never_hides_a_changed_script(run: tuple[Path, dict[str, str]]) -> None:
    """The tamper demo's second pass reuses only byte-identical re-runs."""
    run_dir, ids = run
    memo: verify.Memo = {}
    assert not verify.verify(run_dir, memo=memo).failures and len(memo) == 1
    sha = filed_bundle(run_dir, ids["proj"])
    assert sha is not None  # the filed copy is what re-runs
    target = next(p for p in (run_dir / "store").rglob("helper.py") if sha in str(p))
    os.chmod(target, 0o644)
    target.write_text("def years_left():\n    return 5\n")
    fails = section(verify.verify(run_dir, memo=memo), "calculations").failures
    assert len(fails) == 1 and ids["proj"][:12] in fails[0]


def _append_tx(run_dir: Path, predicate: str, about: str, value: dict[str, Any]) -> None:
    """Append one well-formed World Model transaction, as a forger with write access
    to the folder would (the next tx and event numbers, a plausible source)."""
    log = run_dir / "register/log.jsonl"
    lines = log.read_bytes().splitlines()
    last = json.loads(lines[-1])
    tx = last["tx"] + 1
    seq = last["events"][-1]["seq"] + 1
    at = "2030-01-03T00:00:00.000Z"
    src = {"kind": "tool", "at": at, "by": "stemma:claim-checker", "runId": "s-forged"}
    ev = {"id": f"evt_{seq}", "seq": seq, "kind": "assertFact", "schemaVersion": 1, "at": at,
          "tx": tx, "payload": {"id": f"fact_forged{seq}", "about": about,
                                "predicate": predicate, "value": value, "confidence": None,
                                "source": src, "assertedAt": at}}
    new = {"tx": tx, "at": at, "op": "assertFact", "actor": "stemma:claim-checker",
           "summary": "forged", "events": [ev]}
    log.write_bytes(log.read_bytes() + json.dumps(new, separators=(",", ":")).encode() + b"\n")


@needs_sandbox
def test_forged_log_additions_after_the_seal_fail_and_are_never_read(
        run: tuple[Path, dict[str, str]]) -> None:
    """Codex recheck H1: an appended `holds` verdict and an appended `finished` record
    passed, pinned, and the second showed the run as released."""
    from stemma.report import runread

    run_dir, ids = run
    digest = seal(run_dir)  # an unreleased run, sealed
    _append_tx(run_dir, "verdict", f"claim:stemma:{ids['now']}",
               {"verdict": "holds", "evidence": "forged"})
    _append_tx(run_dir, "finished", "run:stemma:run",
               {"at": "2030-01-03T00:00:00Z", "version": 1, "sha256": "0" * 64,
                "sessions": []})
    result = verify.verify(run_dir, expect=digest)
    assert result.outcome == "FAIL"
    assert any("appended" in f for _l, f in result.failures), verify.render(result)
    r = runread.load(run_dir)
    assert r.release.state == "not_released"
    assert (r.claims[ids["now"]].get("verdict") or {}).get("evidence") != "forged"


def test_a_forged_revocation_after_the_seal_is_never_read(
        run: tuple[Path, dict[str, str]]) -> None:
    from stemma.report import runread

    run_dir, _ids = run
    release(run_dir)
    seal(run_dir)
    assert checks_sealed(run_dir)
    checks = run_dir / "register/checks.jsonl"
    checks.write_bytes(checks.read_bytes() + json.dumps(
        {"kind": "release_blocked", "request_id": "", "at": "2030-01-03T00:00:00Z",
         "reason": "forged block"}).encode() + b"\n")
    assert runread.load(run_dir).release.state == "released"


@pytest.mark.parametrize("how", ["fifo", "malformed", "symlink"])
def test_side_files_are_read_safely_and_fail_by_name(
        run: tuple[Path, dict[str, str]], tmp_path: Path, how: str) -> None:
    """Codex recheck M1: checks.jsonl and refused_corrections.jsonl bypassed the safe
    reader (a FIFO could block; a malformed line raised LogCorrupt)."""
    run_dir, _ids = run
    target = run_dir / ("register/checks.jsonl" if how != "symlink"
                        else "register/refused_corrections.jsonl")
    if target.exists():
        target.unlink()
    if how == "fifo":
        os.mkfifo(target)
    elif how == "malformed":
        target.write_text('{"kind": "verified", "session_id": "x"}\nnot json\n')
    else:
        outside = tmp_path / "outside.jsonl"
        outside.write_text('{"session_id": "x", "claim_id": "y"}\n')
        target.symlink_to(outside)
    result = verify.verify(run_dir, rerun_fn=lambda *a, **k: (_ for _ in ()).throw(
        DerivationError("sandbox-exec is not available")))
    assert result.outcome == "FAIL"
    text = " ".join(f for _l, f in result.failures)
    assert target.name in text


def test_an_oversized_input_number_is_a_named_failure(
        run: tuple[Path, dict[str, str]]) -> None:
    from stemma.report import runread

    run_dir, ids = run
    r = runread.load(run_dir)
    r.claims[ids["now"]]["value"] = {**r.claims[ids["now"]]["value"], "number": 10 ** 400}
    out = verify._rerun_one(r, r.claims[ids["proj"]], lambda *a, **k: None)
    assert out[0] is not None and "too large" in out[0]


def test_dot_names_are_plain_but_dot_segments_are_not(tmp_path: Path) -> None:
    from stemma.report import safefs

    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.json").write_text("{}")
    (tmp_path / ".DS_Store").write_text("")
    files, problems = safefs.walk(tmp_path)
    assert problems == [] and ".claude/settings.json" in files
    for bad in (".", "..", "a/../b", "./a"):
        with pytest.raises(safefs.UnsafeRead):
            safefs.parts(bad)


def test_a_manifest_that_does_not_seal_the_check_records_fails(
        run: tuple[Path, dict[str, str]]) -> None:
    run_dir, _ids = run
    seal(run_dir)
    path = run_dir / "release/manifest.json"
    man = json.loads(path.read_text())
    del man["checks"]
    path.chmod(0o644)
    path.write_text(json.dumps(man))
    result = verify.verify(run_dir)
    assert result.outcome == "FAIL"
    assert any("does not seal register/checks.jsonl" in f for _l, f in result.failures)
