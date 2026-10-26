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
    lines = [json.loads(x) for x in query_log(run_dir).path.read_text().splitlines()]  # type: ignore[union-attr]
    assert [e["purpose"] for e in lines].count("sync") == 1      # synced once
    assert [e["purpose"] for e in lines].count("query") == 2     # one logged query per input


def test_a_number_that_differs_from_the_register_is_refused(run_dir: Path,
                                                            register: FakeRegister) -> None:
    lookup = input_lookup(run_dir, _register_lookup(register, {C_FARE: 25.0}), SQLITE, register)
    with pytest.raises(InputMismatch, match="the register 25.0"):
        lookup(C_FARE)
