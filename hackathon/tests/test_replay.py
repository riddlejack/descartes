"""Record, verify, replay and label: a replay is never shown as live."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hackathon.judgelog import judge_log
from hackathon.replay import (
    ReplayError,
    labelled_text,
    record,
    replay,
    replay_marker,
    verify,
)

from .conftest import REPO


def _pack(run_dir: Path, tmp_path: Path) -> Path:
    pack = tmp_path / "pack"
    record(run_dir, pack)
    return pack


def test_record_keeps_what_a_reader_needs_and_lists_the_rest(run_dir: Path,
                                                             tmp_path: Path) -> None:
    m = record(run_dir, tmp_path / "pack")
    kept = {i["path"] for i in m["items"]}
    assert {"run.json", "events.jsonl", "register/log.jsonl", "deliverable/answer.reader.md",
            "store/receipts/r-annual.json", "datalayer/queries.jsonl"} <= kept
    left = {i["path"] for i in m["left_out"]}
    assert {".claude/settings.json", ".stemma/state.json", "plugin/agents/lead.md",
            f"store/bytes/{'ab' * 32}"} <= left
    assert m["source_run_id"] == run_dir.name and all(len(i["sha256"]) == 64 for i in m["items"])


@pytest.mark.parametrize("secret", ["sk-ant-api03-AbCdEfGh123456", "nvapi-AbCdEfGh123456",
                                    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123"])
def test_record_refuses_anything_shaped_like_a_credential(run_dir: Path, tmp_path: Path,
                                                          secret: str) -> None:
    (run_dir / "deliverable" / "notes.md").write_text(f"key {secret}\n")
    with pytest.raises(ReplayError, match="credential"):
        record(run_dir, tmp_path / "pack")


def test_a_changed_byte_is_refused(run_dir: Path, tmp_path: Path) -> None:
    pack = _pack(run_dir, tmp_path)
    f = pack / "files" / "deliverable" / "answer.reader.md"
    f.write_text(f.read_text().replace("48.7m", "58.7m"))
    with pytest.raises(ReplayError, match="does not match"):
        replay(pack, tmp_path / "runs-out")


def test_unlisted_missing_and_escaping_files_are_refused(run_dir: Path, tmp_path: Path) -> None:
    pack = _pack(run_dir, tmp_path)
    (pack / "files" / "planted.md").write_text("not in the manifest")
    with pytest.raises(ReplayError, match="not in its manifest"):
        verify(pack)
    (pack / "files" / "planted.md").unlink()
    (pack / "files" / "events.jsonl").unlink()
    with pytest.raises(ReplayError, match="missing"):
        verify(pack)
    pack2 = _pack(run_dir, tmp_path / "second")
    m = json.loads((pack2 / "manifest.json").read_text())
    m["items"][0]["path"] = "../../outside.txt"
    (pack2 / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(ReplayError, match="not a plain path"):
        verify(pack2)


def test_replay_marks_every_item_with_its_hash(run_dir: Path, tmp_path: Path) -> None:
    pack = _pack(run_dir, tmp_path)
    rp = replay(pack, tmp_path / "out")
    marker = replay_marker(rp.folder)
    assert marker is not None and marker["mode"] == "replay"
    manifest = json.loads((pack / "manifest.json").read_text())
    assert [(i["path"], i["sha256"], i["label"]) for i in marker["items"]] == [
        (i["path"], i["sha256"], "replay") for i in manifest["items"]]
    assert "Nothing in it ran now" in marker["notice"]


def test_judge_log_never_presents_a_replay_as_live(run_dir: Path, tmp_path: Path) -> None:
    rp = replay(_pack(run_dir, tmp_path), tmp_path / "out")
    text = judge_log(rp.folder)
    assert text.startswith("# Run log: REPLAY, not a live run")
    body = [ln for ln in text.splitlines() if ln.startswith("[")]
    assert body and all(ln.startswith("[replay sha:") for ln in body)
    assert "model claude-opus-5-5" in text and "tx 1 upsertEntity" in text
    assert "zetaris query: 2 rows, 812 ms: SELECT claim_id FROM x.claim" in text
    assert "[replay sha:" in text.split("## The answer")[1]


def test_lines_added_after_replay_are_live_and_changes_are_refused(run_dir: Path,
                                                                    tmp_path: Path) -> None:
    rp = replay(_pack(run_dir, tmp_path), tmp_path / "out")
    q = rp.folder / "datalayer" / "queries.jsonl"
    with q.open("a") as fh:
        fh.write(json.dumps({"engine": "sqlite", "purpose": "query", "rows": 1}) + "\n")
    labels = [label for label, _ in labelled_text(rp.folder, "datalayer/queries.jsonl")]
    assert labels[0].startswith("replay sha:") and labels[-1] == "live"
    q.write_text(q.read_text().replace("812", "9"))
    with pytest.raises(ReplayError, match="changed after"):
        judge_log(rp.folder)


def test_a_live_run_is_labelled_live(run_dir: Path) -> None:
    text = judge_log(run_dir)
    assert text.startswith("# Run log (live run)")
    assert "[replay" not in text and "[live] tx 1 upsertEntity" in text


def _run_py(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    full = {**os.environ, **(env or {})}
    return subprocess.run([sys.executable, str(REPO / "run.py"), *args], capture_output=True,
                          text=True, env=full, timeout=600, check=False)


def test_run_py_replays_and_refuses_live_in_sample_mode(run_dir: Path, tmp_path: Path) -> None:
    pack = _pack(run_dir, tmp_path)
    out = _run_py("replay", str(pack), "--no-conformance",
                  env={"STEMMA_RUNS": str(tmp_path / "out"), "STEMMA_DATALAYER": "sqlite"})
    assert out.returncode == 0, out.stderr
    assert "# Run log: REPLAY, not a live run" in out.stdout
    assert "[live] query_data via sqlite over the replayed rows: 2 rows" in out.stdout
    folder = next((tmp_path / "out").iterdir())
    assert (folder / "JUDGE-LOG.md").exists()
    st = _run_py("status", str(folder))
    assert st.returncode == 0 and '"mode": "replay"' in st.stdout
    ask = _run_py("ask", "anything", env={"SAMPLE_MODE": "true"})
    assert ask.returncode == 2 and "SAMPLE_MODE is on" in ask.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node for the suite")
def test_sample_run_includes_the_live_conformance_suite(run_dir: Path, tmp_path: Path) -> None:
    from hackathon.sample import run_sample

    result = run_sample(_pack(run_dir, tmp_path), tmp_path / "out")
    assert result.ok and "[live] PASS sid-001" in result.log
    assert "[live] 8/8 checks passed" in result.log and "[live] suite PASSED" in result.log
