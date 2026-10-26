"""Command line: `python -m stemma.datalayer <command>`.

- `sync <run_dir>`: copy the run's evidence records to SQLite (and Postgres in Zetaris mode);
- `values <run_dir> <claim_id>...`: the numbers a recorded script would get;
- `provision <run_dir>`: sync, then give the run its own read-only role, datasource and
  semantic layer in Zetaris (done once per run; later calls only check the role);
- `check-zetaris`: one `SELECT 1` through the REST endpoint.
Settings come from the environment (see `stemma.datalayer`).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from stemma.datalayer import Settings, setup, sync, values_backend
from stemma.datalayer.errors import DataLayerError
from stemma.datalayer.querylog import QueryLog
from stemma.datalayer.safepath import read_file
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
    zp = sub.add_parser("provision")
    zp.add_argument("run_dir", type=Path)
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
            if s.mode != "zetaris":
                raise DataLayerError("provision needs STEMMA_DATALAYER=zetaris")
            sync(args.run_dir, s)  # syncs, then provisions the run in Zetaris
            print(read_file(args.run_dir, setup.RECORD_REL.as_posix()).decode())  # type: ignore[union-attr]
    except DataLayerError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
