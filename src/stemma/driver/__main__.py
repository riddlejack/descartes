"""`stemma` (or `python -m stemma.driver`): start <task.json> | resume <run_dir> |
status <run_dir> | attach <run_id> <file>."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from stemma.driver.config import DriverConfig
from stemma.driver.errors import DriverError, RunStateError
from stemma.driver.runfolder import create_run
from stemma.driver.runner import resume, show_status, start


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m stemma.driver")
    ap.add_argument("--plugin-src", type=Path, default=None,
                    help="plugin source folder (default: the repo's plugin/)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_start = sub.add_parser("start", help="create a run from task.json and run it")
    p_start.add_argument("task", type=Path)
    p_resume = sub.add_parser("resume", help="resume a stopped run")
    p_resume.add_argument("run_dir", type=Path)
    p_status = sub.add_parser("status", help="print a run's status")
    p_status.add_argument("run_dir", type=Path)
    p_attach = sub.add_parser("attach", help="hand the running lead a new file")
    p_attach.add_argument("run_id", help="a run id under the runs folder, or a run folder")
    p_attach.add_argument("file", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "attach":
        return _attach(args.run_id, args.file, args.plugin_src)
    try:
        cfg = DriverConfig.from_env(args.plugin_src)
        if args.cmd == "start":
            run_dir = create_run(cfg, args.task)
            print(run_dir, flush=True)
            return start(cfg, run_dir)
        if args.cmd == "resume":
            return resume(cfg, args.run_dir)
        st = show_status(cfg, args.run_dir)
        print(json.dumps(st, indent=1))
        return 0
    except DriverError as exc:
        print(f"stemma driver: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def _attach(run_id: str, file: Path, plugin_src: Path | None) -> int:
    from stemma.hooks.arrivals import AttachError, attach

    try:
        given = Path(run_id)
        run_dir = given if given.is_dir() else DriverConfig.from_env(plugin_src).runs_dir / run_id
        if not (run_dir / "run.json").is_file():
            raise RunStateError(f"no run {run_id!r} (no run.json in {run_dir})")
        rec = attach(run_dir, file)
    except (DriverError, AttachError, OSError) as exc:
        print(f"stemma attach: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({k: rec[k] for k in ("n", "path", "bytes", "sha256", "at")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
