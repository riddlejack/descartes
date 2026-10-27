"""The sealed release manifest: seal an invented run folder, then tamper with it in every
way the seal must catch, and change it in every way a released run legitimately changes."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from stemma.register import manifest
from stemma.register.manifest import (
    LogCorrupt,
    LogError,
    LogTorn,
    ManifestError,
    UnsealableEntry,
)

RUN_ID = "20990101-000000-abcdef"


def _tx(n: int) -> bytes:
    return (json.dumps({"tx": n, "events": [{"kind": "assert", "n": n}]}) + "\n").encode()


SEALED_LOG = len(_tx(1) + _tx(2))


@pytest.fixture
def run(tmp_path: Path) -> Path:
    """A small invented run folder, as it stands at release."""
    r = tmp_path / "run"
    files = {
        "run.json": json.dumps({"run_id": RUN_ID}).encode(),
        "CLAUDE.md": b"# Standing rules (invented)\n",
        "task/task.json": b'{"question": "Should the invented bakery open a second shop?"}\n',
        "frame.md": b"# Frame\nWorking answer: yes, if rent stays below a third of sales.\n",
        "tree.json": b'{"nodes": []}\n',
        "branches/b1/order.md": b"# Intent\nSize the second site's footfall.\n",
        "branches/b1/record.md": b"# Conclusion and confidence\nFootfall 1,200 a day.\n",
        "branches/b1/verdicts/1.json": b'{"verdict": "answered"}\n',
        "store/bytes/" + "a" * 64: b"<html>invented source page</html>",
        "store/receipts/r1.json": b'{"receipt_id": "r1"}\n',
        "store/index.sqlite": b"derived index v1",
        "register/log.jsonl": _tx(1) + _tx(2),
        "register/checks.jsonl": b'{"kind": "request"}\n',
        "register/drafts/answer.v1.md": b"# Answer\nOpen the second shop.\n",
        "register/calls/tu1.json": b'{"role": "lead"}\n',
        "deliverable/answer.md": b"# Answer\nOpen the second shop.\n",
        "events.jsonl": b'{"event": "launch"}\n',
        "usage.jsonl": b'{"tokens": 10}\n',
        "usage.md": b"10 tokens\n",
        "status.json": b'{"state": "running"}\n',
        ".stemma/driver-state.json": b"{}\n",
        ".stemma/sessions/lead.stream.jsonl": b'{"type": "system"}\n{"type": "assi',
        "register/log.jsonl.lock": b"",
        ".stemma-tmp/s1/.keep": b"",
    }
    for rel, data in files.items():
        p = r / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return r


def _flip(path: Path, at: int = 3) -> None:
    data = bytearray(path.read_bytes())
    data[at] ^= 0x01
    path.write_bytes(bytes(data))


def _manifest(run: Path) -> dict[str, Any]:
    m: dict[str, Any] = json.loads((run / manifest.MANIFEST).read_bytes())
    return m


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_seal_writes_the_canonical_manifest_and_check_matches(run: Path) -> None:
    rec = manifest.seal(run, 1)
    data = (run / manifest.MANIFEST).read_bytes()
    m = json.loads(data)
    assert data == json.dumps(m, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False).encode()
    assert not data.endswith(b"\n")
    assert m["format"] == "stemma-release-manifest/2"
    assert (m["run_id"], m["version"]) == (RUN_ID, 1)
    assert m["released_at"].endswith("Z")
    assert m["excluded"] == [*manifest.EXCLUDED, ".stemma/", ".stemma-tmp/"]
    assert m["growing"] == ["register/calls/"]
    paths = [f["path"] for f in m["files"]]
    assert paths == sorted(paths)
    # Excluded operational and post-release files are not listed; everything else is.
    assert set(paths) == {
        "run.json", "CLAUDE.md", "task/task.json", "frame.md", "tree.json",
        "branches/b1/order.md", "branches/b1/record.md", "branches/b1/verdicts/1.json",
        "store/bytes/" + "a" * 64, "store/receipts/r1.json", "register/log.jsonl",
        "register/drafts/answer.v1.md", "register/calls/tu1.json", "deliverable/answer.md"}
    body = (run / "branches/b1/record.md").read_bytes()
    entry = {"path": "branches/b1/record.md", "size": len(body), "sha256": _sha(body)}
    assert entry in m["files"]
    log = (run / "register/log.jsonl").read_bytes()
    assert m["log"] == {"path": "register/log.jsonl", "bytes": len(log), "sha256": _sha(log),
                        "transactions": 2}
    assert rec == {"manifest": "release/manifest.json", "manifest_sha256": _sha(data),
                   "files": 14, "log": m["log"],
                   "checks": m["checks"]}
    checks = (run / "register/checks.jsonl").read_bytes()
    assert m["checks"] == {"path": "register/checks.jsonl", "bytes": len(checks),
                           "sha256": _sha(checks), "records": 1}
    assert manifest.check(run) == []
    assert manifest.check(run, rec["manifest_sha256"]) == []


def test_run_id_given_or_refused(run: Path) -> None:
    (run / "run.json").write_text("{}")
    with pytest.raises(ManifestError, match="run.json has no run_id"):
        manifest.seal(run, 1)
    manifest.seal(run, 2, run_id="given")
    assert (_manifest(run)["run_id"], _manifest(run)["version"]) == ("given", 2)
    with pytest.raises(ManifestError, match="not a positive whole number"):
        manifest.seal(run, 0)


@pytest.mark.parametrize("rel,expect", [
    ("branches/b1/record.md", "changed: branches/b1/record.md"),
    ("register/log.jsonl", (f"register log: the {SEALED_LOG} bytes of register/log.jsonl "
                           f"sealed at release changed")),
    ("deliverable/answer.md", "changed: deliverable/answer.md"),
    ("store/bytes/" + "a" * 64, "changed: store/bytes/" + "a" * 64),
    ("store/receipts/r1.json", "changed: store/receipts/r1.json"),
    ("tree.json", "changed: tree.json"),
])
def test_one_changed_byte_is_named(run: Path, rel: str, expect: str) -> None:
    manifest.seal(run, 1)
    _flip(run / rel)
    out = manifest.check(run)
    assert len(out) == 1 and out[0].startswith(expect), out


def test_added_removed_and_replaced_by_a_symlink_are_named(run: Path, tmp_path: Path) -> None:
    manifest.seal(run, 1)
    (run / "branches/b1/notes.md").write_text("added after release")
    order_size = (run / "branches/b1/order.md").stat().st_size
    (run / "branches/b1/order.md").unlink()
    outside = tmp_path / "outside.md"
    outside.write_bytes((run / "frame.md").read_bytes())  # the same bytes, behind a link
    (run / "frame.md").unlink()
    (run / "frame.md").symlink_to(outside)
    os.mkfifo(run / "branches/b1/pipe")  # must not hang the check
    assert sorted(manifest.check(run)) == sorted([
        "added: branches/b1/notes.md (not in the seal)",
        f"removed: branches/b1/order.md (sealed with {order_size} bytes, missing now)",
        f"not a plain file: frame.md is a symlink to {outside}, sealed as a plain file",
        "not a plain file: branches/b1/pipe is a fifo",
    ])


def test_a_folder_replaced_by_a_symlink_is_named(run: Path, tmp_path: Path) -> None:
    manifest.seal(run, 1)
    copy = tmp_path / "b1-copy"
    (run / "branches/b1").rename(copy)
    (run / "branches/b1").symlink_to(copy)  # every byte the same, reached through a link
    out = manifest.check(run)
    assert f"not a plain file: branches/b1 is a symlink to {copy}" in out
    assert any(x.startswith("removed: branches/b1/record.md") for x in out)


def test_register_log_append_truncation_and_removal_are_named(run: Path) -> None:
    manifest.seal(run, 1)
    log = run / "register/log.jsonl"
    with log.open("ab") as f:  # a transaction after the seal lies outside it
        f.write(_tx(3))
    assert manifest.check(run) == [
        (f"register log: {len(_tx(3))} bytes appended to register/log.jsonl after the seal "
        f"(1 transactions), outside the sealed log")]
    with log.open("ab") as f:
        f.write(b'{"tx": 4')
    out = manifest.check(run)
    assert len(out) == 1 and "not whole records" in out[0]
    log.write_bytes(_tx(1))
    assert manifest.check(run) == [
        (f"register log: register/log.jsonl is shorter than when sealed ({len(_tx(1))} bytes "
        f"now, {SEALED_LOG} sealed)")]
    log.unlink()
    assert manifest.check(run) == [
        f"register log: register/log.jsonl is missing (sealed at {SEALED_LOG} bytes)"]


def test_post_release_changes_do_not_fail(run: Path) -> None:
    manifest.seal(run, 1)
    (run / "retro.md").write_text("# Retrospective (invented)\n")
    with (run / "events.jsonl").open("ab") as f:
        f.write(b'{"event": "launch", "role": "retrospective-writer"}\n')
    with (run / "usage.jsonl").open("ab") as f:
        f.write(b'{"tokens": 5}\n')
    with (run / "register/checks.jsonl").open("ab") as f:
        f.write(b'{"kind": "ended"}\n')
    (run / "register/calls/tu2.json").write_text('{"role": "retrospective-writer"}\n')
    with (run / ".stemma/sessions/lead.stream.jsonl").open("ab") as f:
        f.write(b'stant"}\n{"type": "result"}\n')
    (run / ".stemma/sessions/retro.stream.jsonl").write_text('{"type": "system"}\n')
    (run / ".stemma/settings").mkdir()
    (run / ".stemma/settings/retro.json").write_text("{}\n")
    (run / "usage.md").write_text("15 tokens\n")
    (run / "status.json").write_text('{"state": "released"}\n')
    (run / "status.html").write_text("<p>released</p>\n")
    (run / ".stemma/driver-state.json").write_text('{"done": true}\n')
    (run / "store/index.sqlite").write_bytes(b"derived index v2")
    (run / "store/index.sqlite-shm").write_bytes(b"shm")
    (run / "register/index.sqlite").write_bytes(b"rebuilt")
    (run / ".stemma-tmp/s2").mkdir()
    (run / ".stemma-tmp/s2/out.txt").write_bytes(b"command output")
    (run / "events.jsonl.lock").write_bytes(b"")
    (run / "events.jsonl.torn").write_bytes(b"partial")
    (run / ".stemma/link").symlink_to("/etc")  # inside an excluded folder: not looked at
    assert manifest.check(run) == []


def test_exclusions_are_exact_names() -> None:
    ex = manifest.excluded
    assert ex(".stemma") and ex(".stemma/sessions/a.jsonl") and ex(".stemma-tmp/s1/x")
    assert ex("events.jsonl.lock") and ex("store/index.sqlite-wal") and ex(manifest.MANIFEST)
    assert not ex("branches/b1/events.jsonl") and not ex("a/b/c.lock")
    assert not ex("register/calls/x.json") and not ex("release/extra.md")
    assert not ex("deliverable/answer.md") and not ex(".stemmax/a")


@pytest.mark.parametrize("rel", ["analysis/proof.tmp/result.py",
                                 "branches/b1/data.lock/source.txt",
                                 "branches/b1/events.jsonl", "notes.lock",
                                 "branches/b1/.DS_Store", "register/tmp/x.tmp"])
def test_added_files_under_operational_looking_names_fail(run: Path, rel: str) -> None:
    manifest.seal(run, 1)
    (run / rel).parent.mkdir(parents=True, exist_ok=True)
    (run / rel).write_text("research added after the seal")
    assert manifest.check(run) == [f"added: {rel} (not in the seal)"]


def test_release_holds_only_the_manifests(run: Path) -> None:
    rec = manifest.seal(run, 1)
    (run / "release/extra.md").write_text("added after the seal")
    (run / "release/sub").mkdir()
    (run / "release/sub/manifest.json").write_text("{}")
    assert sorted(manifest.check(run, rec["manifest_sha256"])) == [
        "added: release/extra.md (release/ holds only the manifests)",
        "added: release/sub/manifest.json (release/ holds only the manifests)"]
    with pytest.raises(UnsealableEntry, match="release/extra.md is in release/"):
        manifest.seal(run, 2)
    assert manifest.check(run, rec["manifest_sha256"])  # the refused seal moved nothing
    assert not (run / "release/manifest.1.json").exists()


def test_check_records_are_a_sealed_prefix(run: Path) -> None:
    rec = manifest.seal(run, 1)
    log = run / "register/checks.jsonl"
    sealed = log.read_bytes()
    assert "register/checks.jsonl" not in {f["path"] for f in _manifest(run)["files"]}
    later = b'{"kind": "request", "role": "retrospective-writer"}\n'
    log.write_bytes(sealed + later)  # the retrospective writer's request, after release
    assert manifest.check(run, rec["manifest_sha256"]) == []
    assert manifest.after_seal(run) == [
        ('register/checks.jsonl: after the seal: {"kind": "request", "role": '
        '"retrospective-writer"}')]
    log.write_bytes(sealed.replace(b"request", b"revoked") + later)  # a line edited
    assert manifest.check(run) == [
        (f"check records: the {len(sealed)} bytes of register/checks.jsonl sealed at "
        f"release changed")]
    log.write_bytes(b'{"kind": "verified", "session_id": "s-x"}\n')  # a line replaced
    assert len(manifest.check(run)) == 1
    log.write_bytes(sealed + b'{"kind": "ve')  # a torn append
    assert "not whole records" in manifest.check(run)[0]
    log.unlink()
    assert manifest.check(run) == [
        f"check records: register/checks.jsonl is missing (sealed at {len(sealed)} bytes)"]


def test_identity_stamps_may_be_added_but_not_changed(run: Path) -> None:
    manifest.seal(run, 1)
    (run / "register/calls/tu2.json").write_text('{"role": "retrospective-writer"}')
    assert manifest.check(run) == []
    (run / "register/calls/deep").mkdir()
    (run / "register/calls/deep/x.json").write_text("{}")
    _flip(run / "register/calls/tu1.json")
    assert sorted(manifest.check(run)) == [
        "added: register/calls/deep/x.json (not in the seal)",
        (f"changed: register/calls/tu1.json ({len(b'{"role": "lead"}' + bytes([10]))} bytes "
        f"sealed, {len(b'{"role": "lead"}' + bytes([10]))} now; contents differ)")]


def test_pinned_digest(run: Path) -> None:
    rec = manifest.seal(run, 1)
    wrong = "0" * 64
    assert manifest.check(run, wrong) == [
        (f"release/manifest.json: its SHA-256 is {rec['manifest_sha256']}, not the pinned "
        f"{wrong}")]
    # Tampering consistently (a byte and its manifest entry) passes only without a pin.
    _flip(run / "branches/b1/record.md")
    m = _manifest(run)
    body = (run / "branches/b1/record.md").read_bytes()
    for f in m["files"]:
        if f["path"] == "branches/b1/record.md":
            f["sha256"] = _sha(body)
    (run / manifest.MANIFEST).write_bytes(manifest.canonical(m))
    assert manifest.check(run) == []
    out = manifest.check(run, rec["manifest_sha256"])
    assert len(out) == 1 and "not the pinned" in out[0]


@pytest.mark.parametrize("make", ["symlink", "dangling", "fifo", "folder link"])
def test_seal_refuses_symlinks_and_special_files(run: Path, tmp_path: Path, make: str) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    (run / "analysis").mkdir()
    p = run / "analysis/latest.py"
    if make == "symlink":
        p.symlink_to(run / "frame.md")
    elif make == "dangling":
        p.symlink_to("model.py")
    elif make == "fifo":
        os.mkfifo(p)
    else:
        p.symlink_to(target)
    with pytest.raises(UnsealableEntry, match="analysis/latest.py is a "):
        manifest.seal(run, 1)
    assert not (run / manifest.MANIFEST).exists()


def test_seal_refuses_a_missing_torn_or_corrupt_log(run: Path) -> None:
    log = run / "register/log.jsonl"
    with log.open("ab") as f:
        f.write(b'{"tx": 3')
    with pytest.raises(LogTorn):
        manifest.seal(run, 1)
    log.write_bytes(b"[]\n")
    with pytest.raises(LogCorrupt):
        manifest.seal(run, 1)
    log.unlink()
    with pytest.raises(ManifestError, match="register/log.jsonl is missing"):
        manifest.seal(run, 1)
    assert not (run / manifest.MANIFEST).exists()


def test_seal_refuses_a_symlinked_release_folder(run: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (run / "release").symlink_to(elsewhere)
    with pytest.raises(UnsealableEntry, match="release is a symlink to"):
        manifest.seal(run, 1)
    assert list(elsewhere.iterdir()) == []


def test_a_second_seal_keeps_the_first(run: Path) -> None:
    manifest.seal(run, 1)
    first = (run / manifest.MANIFEST).read_bytes()
    second = manifest.seal(run, 2)
    assert (run / "release/manifest.1.json").read_bytes() == first
    assert manifest.check(run, second["manifest_sha256"]) == []


def test_missing_or_unreadable_manifest_is_named(run: Path) -> None:
    assert manifest.check(run) == [
        "release/manifest.json is missing: the run was never sealed, or its seal was removed"]
    manifest.seal(run, 1)
    (run / manifest.MANIFEST).write_text("{not json")
    out = manifest.check(run)
    assert len(out) == 1 and "is not a readable manifest" in out[0]


def test_check_is_pure_reading(run: Path) -> None:
    manifest.seal(run, 1)
    _flip(run / "branches/b1/record.md")

    def snapshot() -> dict[str, tuple[int, int]]:
        return {str(p.relative_to(run)): (p.lstat().st_size, p.lstat().st_mtime_ns)
                for p in sorted(run.rglob("*"))}

    before = snapshot()
    assert manifest.check(run)
    assert snapshot() == before


def test_parse_and_read_log(run: Path) -> None:
    assert manifest.read_log(run) == [json.loads(_tx(1)), json.loads(_tx(2))]
    assert manifest.parse_log(b"") == []
    with pytest.raises(LogTorn, match="ends in 13 bytes with no newline") as torn:
        manifest.parse_log(_tx(1) + _tx(2) + b'{"tx": 3, "ev')
    assert len(torn.value.records) == 2 and torn.value.tail_bytes == 13
    with pytest.raises(LogCorrupt, match="not a transaction object"):
        manifest.parse_log(_tx(1) + b"[1, 2]\n")
    with pytest.raises(LogCorrupt, match="not JSON"):
        manifest.parse_log(_tx(1) + b"not json\n")
    log = run / "register/log.jsonl"
    log.unlink()
    log.symlink_to(run / "events.jsonl")
    with pytest.raises(LogError, match="not a plain file"):
        manifest.read_log(run)
    log.unlink()
    with pytest.raises(FileNotFoundError):
        manifest.read_log(run)
