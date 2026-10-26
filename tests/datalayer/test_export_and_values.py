"""Export of an invented run, the SQLite path, and the values interface's refusals."""

from __future__ import annotations

import multiprocessing as mp
import sqlite3
from pathlib import Path

import pytest

from stemma.datalayer import Settings, query_log, sqlite_path, sync, values_backend
from stemma.datalayer.errors import DataLayerError, ExportError, ValueMismatch
from stemma.datalayer.export import export_run, markdown_cells
from stemma.datalayer.values import ClaimValueError, EvidenceValues

from .conftest import (
    C_FARE,
    C_NEW,
    C_OLD,
    C_PASSENGERS,
    C_REVENUE,
    RUN_ID,
    FakeRegister,
    claims,
    make_run,
)

SQLITE = Settings.from_env({})


def test_export_counts_every_table(run_dir: Path, register: FakeRegister) -> None:
    ex = export_run(run_dir, register)
    assert ex.counts() == {"capture": 3, "claim": 5, "claim_support": 4, "claim_input": 2,
                           "claim_entity": 5, "claim_verdict": 2, "claim_supersession": 1,
                           "sec_fact": 2, "doc_table_cell": 6, "sync_state": 1}
    assert ex.warnings == []


def test_capture_manifest_keeps_failures(run_dir: Path, register: FakeRegister) -> None:
    rows = {r["receipt_id"]: r for r in export_run(run_dir, register).rows["capture"]}
    assert rows["r-timeout"]["status"] == "timeout"
    assert rows["r-timeout"]["url"] == "https://slow.example/"
    assert rows["r-annual"]["url"] == "https://larkspur.example/ar.pdf"


def test_sec_facts_filed_after_as_of_are_left_out(run_dir: Path, register: FakeRegister) -> None:
    facts = export_run(run_dir, register).rows["sec_fact"]
    assert [f["accession"] for f in facts] == ["0009999901-24-000003", "0009999901-25-000002"]
    assert facts[1]["value"] == 63500000.5 and facts[1]["value_repr"] == "63500000.5"


def test_markdown_cells_keep_escaped_pipes() -> None:
    assert markdown_cells("| a | b |\n|:---|---:|\n| x \\| y | 2 |") == [
        ["a", "b"], ["x \\| y", "2"]]


def test_missing_run_json_is_a_typed_error(tmp_path: Path) -> None:
    with pytest.raises(ExportError, match="run.json"):
        export_run(tmp_path, FakeRegister([]))


def test_unreadable_receipt_is_loud(run_dir: Path, register: FakeRegister) -> None:
    (run_dir / "store" / "receipts" / "r-bad.json").write_text("{not json")
    with pytest.raises(ExportError, match="r-bad"):
        export_run(run_dir, register)


def test_sec_answer_that_is_not_json_is_a_warning(run_dir: Path, register: FakeRegister) -> None:
    sha = next(p for p in (run_dir / "store" / "bytes").iterdir())
    sha.write_bytes(b"<html>rate limited</html>")
    ex = export_run(run_dir, register)
    assert ex.rows["sec_fact"] == [] and "r-sec" in ex.warnings[0]


def _values(run_dir: Path, register: FakeRegister) -> EvidenceValues:
    sync(run_dir, SQLITE, register)
    backend = values_backend(run_dir, SQLITE)
    assert isinstance(backend, EvidenceValues)
    return backend


def test_sqlite_values_by_full_id_and_prefix(run_dir: Path, register: FakeRegister) -> None:
    v = _values(run_dir, register)
    got = v.values([C_REVENUE, f"c:{C_PASSENGERS[:12]}", C_FARE.upper()])
    assert got == {C_REVENUE: 48_700_000.0, f"c:{C_PASSENGERS[:12]}": 1_920_000.0,
                   C_FARE.upper(): 48_700_000.0 / 1_920_000}


def test_values_are_exact_after_the_round_trip(run_dir: Path, register: FakeRegister) -> None:
    got = _values(run_dir, register).values([C_FARE])[C_FARE]
    assert got == 48_700_000.0 / 1_920_000  # bit-for-bit, not approximately


def test_superseded_claims_still_read(run_dir: Path, register: FakeRegister) -> None:
    # Parity with part B's register reader, which resolves any claim by id.
    assert _values(run_dir, register).values([C_OLD, C_NEW]) == {C_OLD: 7.0, C_NEW: 8.0}


@pytest.mark.parametrize("given", [
    "",                                   # not an id
    "abc",                                # too short to be a prefix
    "' OR 1=1 --",                        # injection
    f"{C_REVENUE[:12]}' OR 'a'='a",       # injection after a valid prefix
    "0" * 12,                             # names no claim
])
def test_bad_ids_are_refused(run_dir: Path, register: FakeRegister, given: str) -> None:
    with pytest.raises(ClaimValueError):
        _values(run_dir, register).values([given])


def test_empty_request_is_refused(run_dir: Path, register: FakeRegister) -> None:
    with pytest.raises(ClaimValueError, match="name the claim ids"):
        _values(run_dir, register).values([])


def test_ambiguous_prefix_is_refused(run_dir: Path) -> None:
    recs = claims()
    twin = dict(recs[0], claim_id=recs[0]["claim_id"][:20] + "f" * 44)
    if twin["claim_id"] == recs[0]["claim_id"]:
        twin["claim_id"] = recs[0]["claim_id"][:20] + "e" * 44
    reg = FakeRegister(recs + [twin])
    with pytest.raises(ClaimValueError, match="no single claim"):
        _values(run_dir, reg).values([recs[0]["claim_id"][:16]])


def test_claim_without_number_is_refused(run_dir: Path) -> None:
    recs = claims()
    recs[1] = dict(recs[1], value=None)
    with pytest.raises(ClaimValueError, match="has no number"):
        _values(run_dir, FakeRegister(recs)).values([C_PASSENGERS])


def test_tampered_copy_is_caught(run_dir: Path, register: FakeRegister) -> None:
    v = _values(run_dir, register)
    con = sqlite3.connect(sqlite_path(run_dir))
    with con:
        con.execute("UPDATE claim SET number = number + 0.000001 WHERE claim_id = ?",
                    (C_REVENUE,))
    con.close()
    with pytest.raises(ValueMismatch):
        v.values([C_REVENUE])


def test_other_runs_rows_are_invisible(tmp_path: Path, register: FakeRegister) -> None:
    run = make_run(tmp_path)
    v = _values(run, register)
    con = sqlite3.connect(sqlite_path(run))
    with con:
        con.execute("INSERT INTO claim (run_id, claim_id, number, number_repr) "
                    "VALUES ('other-run', ?, 1.0, '1.0')", ("a" * 64,))
    con.close()
    with pytest.raises(ClaimValueError):
        v.values(["a" * 12])


def test_sync_twice_changes_nothing(run_dir: Path, register: FakeRegister) -> None:
    first = sync(run_dir, SQLITE, register)
    second = sync(run_dir, SQLITE, register)
    assert first == second
    con = sqlite3.connect(sqlite_path(run_dir))
    assert con.execute("SELECT COUNT(*) FROM claim").fetchone()[0] == 5
    con.close()


def test_every_query_is_logged(run_dir: Path, register: FakeRegister) -> None:
    _values(run_dir, register).values([C_REVENUE])
    entries = [e for e in query_log(run_dir).file.read_text().splitlines()]  # type: ignore[union-attr]
    assert any('"purpose": "sync"' in e for e in entries)
    q = [e for e in entries if '"purpose": "query"' in e]
    assert len(q) == 1 and C_REVENUE in q[0] and '"rows": 1' in q[0]


def test_settings_refuse_zetaris_without_credentials() -> None:
    with pytest.raises(DataLayerError, match="ZETARIS_API_KEY"):
        Settings.from_env({"STEMMA_DATALAYER": "zetaris"})
    with pytest.raises(DataLayerError, match="sqlite or zetaris"):
        Settings.from_env({"STEMMA_DATALAYER": "csv"})


def _sync_after_barrier(run: str, barrier: object, n: int) -> None:
    barrier.wait()  # type: ignore[attr-defined]
    for _ in range(n):
        sync(Path(run), SQLITE, FakeRegister(claims()))


def test_concurrent_syncs_leave_one_consistent_copy(run_dir: Path) -> None:
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(4)
    procs = [ctx.Process(target=_sync_after_barrier, args=(str(run_dir), barrier, 5))
             for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    con = sqlite3.connect(sqlite_path(run_dir))
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t} WHERE run_id = ?", (RUN_ID,)).fetchone()[0]
              for t in ("capture", "claim", "claim_support", "sec_fact", "doc_table_cell")}
    con.close()
    assert counts == {"capture": 3, "claim": 5, "claim_support": 4, "sec_fact": 2,
                      "doc_table_cell": 6}


def test_sec_parse_agrees_with_part_a() -> None:
    """D re-reads stored SEC answers; it must find exactly the facts part A's parser finds."""
    import json as _json

    from stemma.datalayer.export import sec_facts_from_bytes
    from stemma.evidence.sec import facts_from_concept_json

    from .conftest import AS_OF, SEC_JSON

    body = _json.dumps(SEC_JSON).encode()
    ours = [(f["accn"], f["end"], f["value"], f["unit"]) for f in sec_facts_from_bytes(body, AS_OF)]
    theirs = [(f["accession"], f["end"], f["value"], f["unit"])
              for f in facts_from_concept_json(SEC_JSON) if str(f["filed"]) < AS_OF]
    assert ours == theirs and len(ours) == 2
