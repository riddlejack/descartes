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
    sha256,
    verify,
)

from .conftest import REPO, add_receipts


def _live_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("[live]")]


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


@pytest.mark.parametrize("secret", [
    "ZETARIS_API_KEY=zk_live_abc123def456",
    "export METERLESS_RUNTIME_TOKEN=tok_abcdef123456",
    "STEMMA_PG_PASSWORD: s3cretvalue99",
    'stemma_pg_password = "s3cretvalue99"',
    '{"api_key": "abcd1234efgh5678"}',
    '{"runtime_token": "xyz1234567890"}',
    'CREATE DATASOURCE d OPTIONS (password "hunter2hunter2")',
    "Authorization: Bearer abcdefgh12345678",
    "sk-ant-api03-AbCdEfGh123456",
    "nvapi-AbCdEfGh123456",
    "ghp_AbCdEfGhIjKlMnOpQrStUvWx12",
    "AKIAABCDEFGHIJKLMNOP",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
])
def test_recording_refuses_every_credential_form(run_dir: Path, tmp_path: Path,
                                                 secret: str) -> None:
    (run_dir / "deliverable" / "notes.md").write_text(f"before\n{secret}\nafter\n")
    with pytest.raises(ReplayError, match="nothing was recorded"):
        record(run_dir, tmp_path / "pack")
    assert not (tmp_path / "pack").exists()


@pytest.mark.parametrize("masked", ['password "***"', "ZETARIS_API_KEY=<set>",
                                    '{"input_tokens": 1204}', '{"author": "stemma:worker"}',
                                    "The token budget was 30,000 words."])
def test_masked_values_and_ordinary_words_record(run_dir: Path, tmp_path: Path,
                                                 masked: str) -> None:
    (run_dir / "deliverable" / "notes.md").write_text(masked + "\n")
    assert record(run_dir, tmp_path / "pack")["items"]


def test_a_pack_carrying_a_credential_is_refused(run_dir: Path, tmp_path: Path) -> None:
    pack = _pack(run_dir, tmp_path)
    f = pack / "files" / "deliverable" / "answer.reader.md"
    f.write_text("ZETARIS_API_KEY=zk_live_abc123def456\n")
    m = json.loads((pack / "manifest.json").read_text())
    for item in m["items"]:
        if item["path"] == "deliverable/answer.reader.md":
            item["sha256"], item["bytes"] = sha256(f.read_bytes()), len(f.read_bytes())
    (pack / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(ReplayError, match="holds a key"):
        verify(pack)


@pytest.mark.parametrize("hostile", ["x/../../outside", "../outside", "a/b", "", ".hidden"])
def test_hostile_run_ids_never_leave_the_runs_folder(run_dir: Path, tmp_path: Path,
                                                     hostile: str) -> None:
    pack = _pack(run_dir, tmp_path)
    m = json.loads((pack / "manifest.json").read_text())
    m["source_run_id"] = hostile
    (pack / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(ReplayError, match="plain identifier"):
        replay(pack, tmp_path / "runs-out")
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("path", ["./deliverable/answer.reader.md",
                                  "deliverable//answer.reader.md",
                                  "deliverable/./answer.reader.md"])
def test_non_canonical_manifest_paths_are_refused(run_dir: Path, tmp_path: Path,
                                                  path: str) -> None:
    pack = _pack(run_dir, tmp_path)
    m = json.loads((pack / "manifest.json").read_text())
    for item in m["items"]:
        if item["path"] == "deliverable/answer.reader.md":
            item["path"] = path
    (pack / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(ReplayError, match="canonical"):
        verify(pack)


def test_lookups_canonicalise_and_never_say_live(run_dir: Path, tmp_path: Path) -> None:
    rp = replay(_pack(run_dir, tmp_path), tmp_path / "out")
    for spelling in ("./deliverable/answer.reader.md", "deliverable//answer.reader.md"):
        labels = {label for label, _ in labelled_text(rp.folder, spelling)}
        assert labels and all(lb.startswith("replay sha:") for lb in labels)


def test_a_folder_without_marker_is_recorded_not_live(run_dir: Path, tmp_path: Path) -> None:
    pack = _pack(run_dir, tmp_path)
    text = judge_log(pack / "files")  # the pack's own files, no REPLAY.json
    assert not _live_lines(text) and "[recorded sha:" in text


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
    with pytest.raises(ReplayError, match="canonical"):
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


def test_lines_added_after_replay_are_marked_and_changes_are_refused(run_dir: Path,
                                                                      tmp_path: Path) -> None:
    rp = replay(_pack(run_dir, tmp_path), tmp_path / "out")
    q = rp.folder / "datalayer" / "queries.jsonl"
    with q.open("a") as fh:
        fh.write(json.dumps({"engine": "sqlite", "purpose": "query", "rows": 1}) + "\n")
    labels = [label for label, _ in labelled_text(rp.folder, "datalayer/queries.jsonl")]
    assert labels[0].startswith("replay sha:") and labels[-1] == "appended after replay"
    q.write_text(q.read_text().replace("812", "9"))
    with pytest.raises(ReplayError, match="changed after"):
        judge_log(rp.folder)


def test_a_run_folder_is_labelled_recorded(run_dir: Path) -> None:
    text = judge_log(run_dir)
    assert text.startswith("# Run log (read from the run's own records)")
    assert not _live_lines(text)
    assert not [ln for ln in text.splitlines() if ln.startswith("[replay")]
    assert "] tx 1 upsertEntity" in text and "[recorded sha:" in text


def test_provider_receipts_are_recorded_and_shown(run_dir: Path, tmp_path: Path) -> None:
    add_receipts(run_dir)
    pack = _pack(run_dir, tmp_path)
    assert "datalayer/providers.jsonl" in {i["path"] for i in json.loads(
        (pack / "manifest.json").read_text())["items"]}
    text = judge_log(replay(pack, tmp_path / "out").folder)
    assert "nightshift demo: route meterless/deep answered by claude-opus-5-5" in text


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
    later = judge_log(result.folder)  # a log written afterwards keeps the result, as recorded
    assert not _live_lines(later)
    assert "PASS sid-001" in later and "] suite PASSED" in later and "[recorded 20" in later
