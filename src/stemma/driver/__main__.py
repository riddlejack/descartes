"""`python -m stemma.driver start <task.json> | resume <run_dir> | status <run_dir>`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from stemma.driver.config import DriverConfig
from stemma.driver.errors import DriverError
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
    args = ap.parse_args(argv)
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


if __name__ == "__main__":
    sys.exit(main())
