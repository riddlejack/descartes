"""Command line: `python -m stemma.datalayer <command>`.

- `sync <run_dir>`: copy the run's evidence records to SQLite (and Postgres in Zetaris mode);
- `values <run_dir> <claim_id>...`: the numbers a recorded script would get;
- `setup-zetaris`: register the Postgres datasource and the semantic layer in Zetaris;
- `check-zetaris`: one `SELECT 1` through the REST endpoint.
Settings come from the environment (see `stemma.datalayer`).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from stemma.datalayer import Settings, setup, sync, values_backend
from stemma.datalayer.errors import DataLayerError, QueryError
from stemma.datalayer.querylog import QueryLog
from stemma.datalayer.zetaris import ZetarisClient


def _client(s: Settings, log_path: Path | None) -> ZetarisClient:
    if s.zetaris is None:
        raise DataLayerError("ZETARIS_API_KEY is not set")
    return ZetarisClient(s.zetaris, QueryLog(log_path))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m stemma.datalayer")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("sync")
    sp.add_argument("run_dir", type=Path)
    vp = sub.add_parser("values")
    vp.add_argument("run_dir", type=Path)
    vp.add_argument("ids", nargs="+")
    zp = sub.add_parser("setup-zetaris")
    zp.add_argument("--log", type=Path, default=None)
    cp = sub.add_parser("check-zetaris")
    cp.add_argument("--log", type=Path, default=None)
    args = p.parse_args(argv)
    try:
        s = Settings.from_env()
        if args.cmd == "sync":
            print(json.dumps(sync(args.run_dir, s), indent=2))
        elif args.cmd == "values":
            print(json.dumps(values_backend(args.run_dir, s).values(args.ids), indent=2))
        elif args.cmd == "check-zetaris":
            print(_client(s, args.log).run("SELECT 1 AS one", ("one",)))
        else:
            if s.postgres is None:
                raise DataLayerError("STEMMA_PG_PASSWORD is not set")
            client = _client(s, args.log)
            for st in setup.statements(s.postgres, s.jdbc_host, s.datasource, s.namespace,
                                       s.model):
                try:
                    answer = client.execute(st.sql)
                except QueryError as exc:
                    if any(t in str(exc) for t in st.tolerate):
                        print(f"{st.purpose}: already done")
                        continue
                    raise
                print(f"{st.purpose}: {json.dumps(answer)[:200]}")
    except DataLayerError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
