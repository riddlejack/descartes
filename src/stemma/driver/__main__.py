"""`stemma` (or `python -m stemma.driver`): start [--frame-only] <task.json> |
resume <run_dir> | status <run_dir> | attach <run_id> <file>."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from stemma.driver.config import DriverConfig, runs_dir_from_env
from stemma.driver.errors import DriverError, RunStateError
from stemma.driver.runfolder import RUN_ID, create_run
from stemma.driver.runner import resume, show_status, start


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m stemma.driver")
    ap.add_argument("--plugin-src", type=Path, default=None,
                    help="plugin source folder (default: the repo's plugin/)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_start = sub.add_parser("start", help="create a run from task.json and run it")
    p_start.add_argument("task", type=Path)
    p_start.add_argument("--frame-only", action="store_true",
                         help="end the run once the frame, the tree and the answer to the "
                              "frame critique exist (stop_after = frame)")
    p_resume = sub.add_parser("resume", help="resume a stopped run")
    p_resume.add_argument("run_dir", type=Path)
    p_status = sub.add_parser("status", help="print a run's status")
    p_status.add_argument("run_dir", type=Path)
    p_attach = sub.add_parser("attach", help="hand the running lead a new file")
    p_attach.add_argument("run_id", help="a run id under $STEMMA_RUNS")
    p_attach.add_argument("file", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "attach":
        return _attach(args.run_id, args.file)
    try:
        cfg = DriverConfig.from_env(args.plugin_src)
        if args.cmd == "start":
            run_dir = create_run(cfg, args.task,
                                 stop_after="frame" if args.frame_only else None)
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


def _attach(run_id: str, file: Path) -> int:
    from stemma.hooks.arrivals import AttachError, attach

    try:
        if not RUN_ID.fullmatch(run_id):
            raise RunStateError(f"{run_id!r} is not a run id (like 20261026-070412-2b3c98)")
        runs = runs_dir_from_env()
        run_dir = runs / run_id
        if run_dir.is_symlink() or not (run_dir / "run.json").is_file():
            raise RunStateError(f"no run {run_id} under STEMMA_RUNS ({runs})")
        rec = attach(run_dir, file)
    except (DriverError, AttachError, OSError) as exc:
        print(f"stemma attach: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({k: rec[k] for k in ("n", "path", "bytes", "sha256", "at")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
