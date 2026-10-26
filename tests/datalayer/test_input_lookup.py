"""The hook for part B's derivation-input lookup: synced once, guarded by equality."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stemma.datalayer import InputMismatch, Settings, input_lookup, query_log

from .conftest import C_FARE, C_PASSENGERS, C_REVENUE, FakeRegister

SQLITE = Settings.from_env({})


def _register_lookup(reg: FakeRegister, override: dict[str, float] | None = None):  # type: ignore[no-untyped-def]
    def lookup(given: str) -> tuple[str, float]:
        cid = next(c for c in reg.records if c.startswith(given))
        return cid, (override or {}).get(cid, float(reg.records[cid]["value"]["number"]))
    return lookup


def test_inputs_come_through_the_data_layer(run_dir: Path, register: FakeRegister) -> None:
    lookup = input_lookup(run_dir, _register_lookup(register), SQLITE, register)
    assert lookup(C_REVENUE[:12]) == (C_REVENUE, 48_700_000.0)
    assert lookup(C_PASSENGERS) == (C_PASSENGERS, 1_920_000.0)
    lines = [json.loads(x) for x in query_log(run_dir).file.read_text().splitlines()]  # type: ignore[union-attr]
    assert [e["purpose"] for e in lines].count("sync") == 1      # synced once
    assert [e["purpose"] for e in lines].count("query") == 2     # one logged query per input


def test_a_number_that_differs_from_the_register_is_refused(run_dir: Path,
                                                            register: FakeRegister) -> None:
    lookup = input_lookup(run_dir, _register_lookup(register, {C_FARE: 25.0}), SQLITE, register)
    with pytest.raises(InputMismatch, match="the register 25.0"):
        lookup(C_FARE)


def test_unreachable_data_layer_uses_the_register_number_loudly(
        run_dir: Path, register: FakeRegister, monkeypatch: pytest.MonkeyPatch) -> None:
    import stemma.datalayer as dl
    from stemma.datalayer.errors import ZetarisUnavailable

    def down(*args: object, **kwargs: object) -> None:
        raise ZetarisUnavailable("cannot reach http://localhost:8888: connection refused")

    monkeypatch.setattr(dl, "sync", down)
    alerts: list[dict[str, object]] = []
    lookup = input_lookup(run_dir, _register_lookup(register, {C_FARE: 25.0}), SQLITE,
                          register, alerts.append)
    assert lookup(C_FARE) == (C_FARE, 25.0)        # the register's own number, unchanged
    assert alerts[0]["event"] == "datalayer_unavailable" and C_FARE == alerts[0]["claim_id"]
    lines = [json.loads(x) for x in query_log(run_dir).file.read_text().splitlines()]  # type: ignore[union-attr]
    assert any(e.get("purpose") == "datalayer_unavailable" and e.get("level") == "loud"
               for e in lines)


def test_a_broken_copy_refuses_rather_than_passing(run_dir: Path,
                                                   register: FakeRegister) -> None:
    import sqlite3

    from stemma.datalayer import sqlite_path, sync

    sync(run_dir, SQLITE, register)
    lookup = input_lookup(run_dir, _register_lookup(register), SQLITE, register)
    lookup(C_REVENUE)  # synced now; tamper with the copy, keep its text form
    con = sqlite3.connect(sqlite_path(run_dir))
    with con:
        con.execute("UPDATE claim SET number = 1.0 WHERE claim_id = ?", (C_PASSENGERS,))
    con.close()
    with pytest.raises(InputMismatch):
        lookup(C_PASSENGERS)


def test_negative_zero_is_not_zero(run_dir: Path) -> None:
    from .conftest import claims

    recs = claims()
    recs[2] = dict(recs[2], value=dict(recs[2]["value"], number=0.0))
    reg = FakeRegister(recs)
    lookup = input_lookup(run_dir, _register_lookup(reg, {C_FARE: -0.0}), SQLITE, reg)
    assert 0.0 == -0.0  # equal as numbers, different in bits
    with pytest.raises(InputMismatch):
        lookup(C_FARE)
