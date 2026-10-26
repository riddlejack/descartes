"""Third review round: one snapshot per read, no response keys in errors, provisioning
decided by structured state, non-finite values refused."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import stemma.datalayer as dl
from stemma.datalayer import Settings, query_log, setup, sync
from stemma.datalayer.errors import QueryError, SinkError
from stemma.datalayer.query import run_query
from stemma.datalayer.querylog import QueryLog
from stemma.datalayer.runspace import RunSpace
from stemma.register import jsonl
from stemma.register.store import Register

from .conftest import C_NEW, C_REVENUE, RUN_ID, FakeRegister, claims
from .test_zetaris_path import FakeZetaris, _fake_db, _zetaris_values

SQLITE = Settings.from_env({})
CONSISTENT = ("SELECT c.claim_id, c.verdict, v.verdict AS last_verdict FROM claim c "
              "JOIN claim_verdict v ON v.claim_id = c.claim_id AND v.n = "
              "(SELECT max(w.n) FROM claim_verdict w WHERE w.claim_id = c.claim_id)")


def _with_new_verdict(verdict: str) -> FakeRegister:
    recs = claims()
    entry = {"verdict": verdict, "evidence": "rechecked", "session": "s-9"}
    recs[0] = dict(recs[0], verdict=entry, verdict_history=[*recs[0]["verdict_history"], entry])
    return FakeRegister(recs)


def test_sqlite_tables_load_from_one_state(run_dir: Path, register: FakeRegister,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    sync(run_dir, SQLITE, register)
    monkeypatch.setattr(dl, "sync", lambda *a, **k: {})  # only the racing writer syncs
    writer: list[threading.Thread] = []

    def race(i: int) -> None:
        if i == 0:  # after the first table is read, a newer sync tries to land
            t = threading.Thread(target=dl.SqliteSink(dl.SQLITE_REL, query_log(run_dir),
                                                      root=run_dir).write,
                                 args=(dl.export_run(run_dir, _with_new_verdict(
                                     "not_supported")),))
            t.start()
            writer.append(t)
            time.sleep(0.4)

    out = run_query(run_dir, CONSISTENT, SQLITE, register, between=race)
    writer[0].join(30)
    assert out["rows"] and all(r[1] == r[2] for r in out["rows"])
    assert out["rows"][0][1] == "holds"  # the state the read began in
    after = run_query(run_dir, CONSISTENT, SQLITE, register)
    assert after["rows"][0][1] == "not_supported"  # the writer landed after the read


def test_zetaris_tables_load_from_one_sync(tmp_path: Path, run_dir: Path,
                                           register: FakeRegister,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    from .test_query_data import ZENV, _sqlite_only_sync

    monkeypatch.setattr(dl, "sync", _sqlite_only_sync)
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    old = _fake_db(tmp_path / "old", run_dir, register)
    new = _fake_db(tmp_path / "new", run_dir, _with_new_verdict("not_supported"))
    fake = FakeZetaris(old)
    calls = {"n": 0}
    inner = fake.__call__

    def swapping(url: str, body: bytes | None, headers: dict[str, str],
                 timeout: float) -> tuple[int, bytes]:
        calls["n"] += 1
        if calls["n"] == 3:
            fake.rows_db = new  # a sync lands between the two table reads
        return inner(url, body, headers, timeout)

    out = run_query(run_dir, CONSISTENT, Settings.from_env(ZENV), register, swapping)
    assert out["backend"] == "zetaris" and all(r[1] == r[2] for r in out["rows"])
    assert out["rows"][0][1] == "not_supported" and calls["n"] > 4  # it read again


def test_zetaris_that_never_settles_is_an_error(tmp_path: Path, run_dir: Path,
                                                register: FakeRegister,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    from .test_query_data import ZENV, _sqlite_only_sync

    monkeypatch.setattr(dl, "sync", _sqlite_only_sync)
    n = {"i": 0}

    def churn(url: str, body: bytes | None, headers: dict[str, str],
              timeout: float) -> tuple[int, bytes]:
        n["i"] += 1
        sql = json.loads(body or b"{}")["sql"]
        if sql.startswith("SELECT generation"):
            return 200, json.dumps({"data": [[f"g{n['i']}"]]}).encode()
        return 200, json.dumps({"data": []}).encode()

    with pytest.raises(QueryError, match="changed during each"):
        run_query(run_dir, CONSISTENT, Settings.from_env(ZENV), register, churn)


def test_export_reads_the_register_once(run_dir: Path, register: FakeRegister) -> None:
    dl.export_run(run_dir, register)
    assert register.snapshots == 1


def test_register_snapshot_waits_for_writers(tmp_path: Path) -> None:
    reg = Register(tmp_path / "run")
    held = threading.Event()

    def hold() -> None:
        with jsonl.exclusive(reg._lock):
            held.set()
            time.sleep(0.5)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    start = time.monotonic()
    position, records = reg.claims_snapshot()
    t.join()
    assert time.monotonic() - start >= 0.4 and records == [] and position == 0


def test_response_keys_never_reach_errors_or_logs(tmp_path: Path) -> None:
    log = QueryLog(tmp_path / "q.jsonl")
    body = json.dumps({"PRIVATE-PROMPT-TEXT": "error"}).encode()
    with pytest.raises(QueryError) as e:
        _zetaris_values(FakeZetaris(body=body), log).values([C_NEW])
    text = str(e.value) + (tmp_path / "q.jsonl").read_text()
    assert "PRIVATE-PROMPT-TEXT" not in text and "bytes, sha256" in str(e.value)
    rows = json.dumps({"data": [{"PRIVATE-KEY-NAME": 1}]}).encode()
    with pytest.raises(QueryError) as e2:
        _zetaris_values(FakeZetaris(body=rows), log).values([C_NEW])
    assert "PRIVATE-KEY-NAME" not in str(e2.value) + (tmp_path / "q.jsonl").read_text()


@pytest.mark.parametrize("number,text", [(float("nan"), "nan"), (float("inf"), "inf"),
                                         (float("-inf"), "-inf")])
def test_non_finite_values_refuse(number: float, text: str) -> None:
    body = json.dumps({"data": [[C_NEW, number, text]]}).encode()
    with pytest.raises(QueryError, match="not a finite number"):
        _zetaris_values(FakeZetaris(body=body), QueryLog()).values([C_NEW])


class FakeSink:
    def __init__(self, exists: bool = False, problems: list[str] | None = None) -> None:
        self.exists = exists
        self.problems = problems or []
        self.created = 0
        self.resets = 0

    def role_exists(self, space: RunSpace) -> bool:
        return self.exists

    def create_run_role(self, space: RunSpace) -> str:
        self.created += 1
        self.exists = True
        return "P" * 32

    def reset_password(self, space: RunSpace) -> str:
        self.resets += 1
        return "Q" * 32

    def write_problems(self, space: RunSpace) -> list[str]:
        return list(self.problems)


class FakeStatements:
    def __init__(self, listed: list[str] | None = None, fail_on: str | None = None) -> None:
        self.listed = listed or []
        self.fail_on = fail_on
        self.sent: list[str] = []

    def execute(self, sql: str) -> Any:
        if self.fail_on and sql.startswith(self.fail_on):
            self.fail_on = None
            raise QueryError("HTTP 500 (12 bytes, sha256 0123456789abcdef)", 500)
        self.sent.append(sql)
        return {}

    def cells(self, sql: str) -> list[list[str]]:
        assert sql == "SHOW DATASOURCES"
        return [[name, "PostgreSQL"] for name in self.listed]


NS = "lightning.metastore.stemma"


def _provision(run_dir: Path, sink: FakeSink, st: FakeStatements) -> setup.Record:
    return setup.provision(run_dir, RUN_ID, sink, st, NS, "stemma-evidence-pg", "stemma")


def test_provisioning_twice_does_nothing_the_second_time(run_dir: Path) -> None:
    sink, st = FakeSink(), FakeStatements()
    rec = _provision(run_dir, sink, st)
    assert setup.complete(rec, RunSpace(RUN_ID), NS) and sink.created == 1
    first = list(st.sent)
    _provision(run_dir, sink, st)
    assert st.sent == first and sink.created == 1


def test_a_half_done_provision_resumes_without_repeating(run_dir: Path) -> None:
    sink, st = FakeSink(), FakeStatements(fail_on="COMPILE USL")
    with pytest.raises(QueryError):
        _provision(run_dir, sink, st)
    _provision(run_dir, sink, st)
    assert sum(s.startswith("CREATE DATASOURCE") for s in st.sent) == 1
    assert sum(s.startswith("COMPILE USL") for s in st.sent) == 1


def test_an_existing_datasource_is_found_by_listing_not_by_message(run_dir: Path) -> None:
    space = RunSpace(RUN_ID)
    sink = FakeSink()
    sink.created = 0
    # The role step was recorded earlier; the datasource exists in Zetaris but the record
    # never said so (a crash between the two).
    setup.write_file(run_dir, setup.RECORD_REL.as_posix(),
                     setup.Record(RUN_ID, ["role"]).as_json(space))
    sink.exists = True
    st = FakeStatements(listed=[space.datasource.upper()])
    _provision(run_dir, sink, st)
    assert st.sent[0] == f"DROP DATASOURCE {space.datasource}"
    assert st.sent[1].startswith("CREATE DATASOURCE") and sink.resets == 1


def test_errors_mentioning_existence_are_not_swallowed(run_dir: Path) -> None:
    class Says(FakeStatements):
        def execute(self, sql: str) -> Any:
            raise QueryError("HTTP 409: datasource already exists", 409)

    with pytest.raises(QueryError, match="already exists"):
        _provision(run_dir, FakeSink(), Says())


def test_a_role_this_run_did_not_create_is_refused(run_dir: Path) -> None:
    st = FakeStatements()
    with pytest.raises(SinkError, match="never reused"):
        _provision(run_dir, FakeSink(exists=True), st)
    assert st.sent == []


def test_a_role_that_can_write_stops_provisioning(run_dir: Path) -> None:
    st = FakeStatements()
    with pytest.raises(SinkError, match="can do more than read"):
        _provision(run_dir, FakeSink(problems=["can change a table"]), st)
    assert st.sent == []  # Zetaris is never given the role


def test_round_trip_numbers_still_exact(run_dir: Path, register: FakeRegister,
                                        tmp_path: Path) -> None:
    db = _fake_db(tmp_path, run_dir, register)
    con = sqlite3.connect(db)
    assert con.execute("SELECT count(*) FROM sync_state").fetchone()[0] == 1
    con.close()
    v = _zetaris_values(FakeZetaris(db), QueryLog()).values([C_REVENUE])
    assert v[C_REVENUE].hex() == (48_700_000.0).hex()
