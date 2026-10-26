"""The hook in part B's register (contract 5a), against the real `Register`.

Zetaris mode routes each derivation input through the data layer: a different number
refuses the claim (`input_mismatch`); an unreachable data layer files the claim with the
register's own numbers and writes `datalayer_unavailable` to the run's events. Without
Zetaris mode nothing changes. No network: "unreachable" is a closed loopback port.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

import stemma.datalayer as dl
from stemma.register import jsonl
from stemma.register.derivation import RerunResult
from stemma.register.evidence_port import QuoteCheckFailed
from stemma.register.filing import Refused
from stemma.register.store import Caller, Register

TEXT = "Larkspur Ferries carried 1.92 million passengers on revenue of 48.7m."
WORKER = Caller("stemma:worker", "a1", "s-lead")
ZETARIS_DOWN = {"STEMMA_DATALAYER": "zetaris", "ZETARIS_API_KEY": "key-not-real",
                "ZETARIS_REST_URL": "http://127.0.0.1:1/api/v1.0",
                "STEMMA_PG_PASSWORD": "pw-not-real", "STEMMA_PG_HOST": "127.0.0.1",
                "STEMMA_PG_PORT": "1"}


class FakeEvidence:
    def check_quote(self, run_dir: Path, receipt_id: str, quote: str) -> bool:
        if receipt_id != "r1":
            raise QuoteCheckFailed(f"unknown receipt {receipt_id}")
        return quote in TEXT

    def read_text(self, run_dir: Path, receipt_id: str) -> str:
        return TEXT


class RecordingRerun:
    """Stands in for the sandboxed re-run: divides the two inputs it is handed."""

    def __init__(self) -> None:
        self.seen: list[dict[str, float]] = []

    def __call__(self, script: Path, values: dict[str, float]) -> RerunResult:
        self.seen.append(dict(values))
        a, b = values.values()
        return RerunResult(a / b, 0, "", 120)


def _observed(statement: str, number: float, written: str, quote: str) -> dict[str, Any]:
    return {"statement": statement, "tier": "observed", "branch_id": "b1",
            "entities": ["Larkspur Ferries"],
            "value": {"number": number, "unit": "x", "period": "FY2024", "kind": "level",
                      "as_written": written},
            "support": [{"receipt_id": "r1", "quote": quote}]}


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Register, RecordingRerun, str, str]:
    run = tmp_path / "run-hook-1"
    run.mkdir()
    (run / "run.json").write_text(json.dumps({"run_id": "run-hook-1", "as_of": None}))
    (run / "analysis").mkdir()
    (run / "analysis" / "fare.py").write_text("# divides revenue by passengers\n")
    rr = RecordingRerun()
    reg = Register(run, evidence=FakeEvidence(), rerun_fn=rr)
    a = reg.file_claim(WORKER, _observed("Revenue was 48.7m.", 48.7e6, "48.7m",
                                         "revenue of 48.7m"))["claim_id"]
    b = reg.file_claim(WORKER, _observed("Passengers were 1.92 million.", 1.92e6,
                                         "1.92 million", "1.92 million passengers"))["claim_id"]
    return reg, rr, a, b


def _estimated(a: str, b: str) -> dict[str, Any]:
    return {"statement": "Revenue per passenger was about 25.36.", "tier": "estimated",
            "branch_id": "b1", "entities": ["Larkspur Ferries"],
            "value": {"number": 48.7e6 / 1.92e6, "unit": "x per passenger", "period": "FY2024",
                      "kind": "ratio", "denominator": "passengers", "as_written": "25.36"},
            "derivation": {"script": "analysis/fare.py", "inputs": [a[:12], b[:12]]}}


def _events(reg: Register) -> list[dict[str, Any]]:
    return jsonl.read_all(reg.run_dir / "events.jsonl")


def test_without_zetaris_mode_nothing_changes(setup: tuple[Register, RecordingRerun, str, str],
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    reg, rr, a, b = setup
    monkeypatch.delenv("STEMMA_DATALAYER", raising=False)
    out = reg.file_claim(WORKER, _estimated(a, b))
    assert any(c["check"] == "derivation_reproduces" for c in out["checks"])
    assert rr.seen == [{a: 48.7e6, b: 1.92e6}]
    assert not (reg.run_dir / "datalayer").exists()


def test_unreachable_files_with_register_numbers_and_is_loud(
        setup: tuple[Register, RecordingRerun, str, str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    reg, rr, a, b = setup
    for k, v in ZETARIS_DOWN.items():
        monkeypatch.setenv(k, v)
    out = reg.file_claim(WORKER, _estimated(a, b))
    assert any(c["check"] == "derivation_reproduces" for c in out["checks"])
    assert rr.seen == [{a: 48.7e6, b: 1.92e6}]  # the register's own numbers
    loud = [e for e in _events(reg) if e.get("event") == "datalayer_unavailable"]
    assert {e["claim_id"] for e in loud} == {a, b} and all(e["source"] == "register"
                                                           for e in loud)
    assert all("SinkError" in e["error"] for e in loud)


def test_a_different_number_refuses(setup: tuple[Register, RecordingRerun, str, str],
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    reg, rr, a, b = setup
    for k, v in ZETARIS_DOWN.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(dl, "sync", lambda *args, **kwargs: {})

    def served(self: dl.EvidenceValues, ids: list[str]) -> dict[str, float]:
        return {i: (1.93e6 if i == b else 48.7e6) for i in ids}  # one input altered

    monkeypatch.setattr(dl.EvidenceValues, "values", served)
    with pytest.raises(Refused, match="input_mismatch") as e:
        reg.file_claim(WORKER, _estimated(a, b))
    assert "1.93" not in str(e.value) and rr.seen == []  # names the check, hints nothing


def test_matching_numbers_come_from_the_data_layer(
        setup: tuple[Register, RecordingRerun, str, str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    reg, rr, a, b = setup
    for k, v in ZETARIS_DOWN.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(dl, "sync", lambda *args, **kwargs: {})
    calls: list[list[str]] = []

    def served(self: dl.EvidenceValues, ids: list[str]) -> dict[str, float]:
        calls.append(list(ids))
        return {i: (1.92e6 if i == b else 48.7e6) for i in ids}

    monkeypatch.setattr(dl.EvidenceValues, "values", served)
    reg.file_claim(WORKER, _estimated(a, b))
    assert calls == [[a], [b]] and rr.seen == [{a: 48.7e6, b: 1.92e6}]
    assert not [e for e in _events(reg) if e.get("event") == "datalayer_unavailable"]


def test_an_unwritable_log_never_stops_a_filing(setup: tuple[Register, RecordingRerun, str, str],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    reg, rr, a, b = setup
    for k, v in ZETARIS_DOWN.items():
        monkeypatch.setenv(k, v)
    folder = reg.run_dir / "datalayer"
    folder.mkdir(exist_ok=True)
    (folder / "queries.jsonl").write_text("")
    os.chmod(folder / "queries.jsonl", 0o400)  # the log cannot be written
    os.chmod(folder, 0o500)
    try:
        out = reg.file_claim(WORKER, _estimated(a, b))
    finally:
        os.chmod(folder, 0o700)
        os.chmod(folder / "queries.jsonl", 0o600)
    assert any(c["check"] == "derivation_reproduces" for c in out["checks"])
    assert rr.seen == [{a: 48.7e6, b: 1.92e6}]
    loud = [e for e in _events(reg) if e.get("event") == "datalayer_unavailable"]
    assert loud and all("log_error" in e for e in loud)


def test_a_failing_alert_goes_to_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    run = tmp_path / "run-x"
    run.mkdir()
    (run / "run.json").write_text(json.dumps({"run_id": "run-x"}))

    def boom(record: dict[str, Any]) -> None:
        raise OSError("events.jsonl is gone")

    monkeypatch.setattr(dl, "sync", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    lookup = dl.input_lookup(run, lambda g: ("a" * 64, 3.0), dl.Settings.from_env({}),
                             alert=boom)
    assert lookup("a" * 12) == ("a" * 64, 3.0)
    assert "datalayer_unavailable" in capsys.readouterr().err
