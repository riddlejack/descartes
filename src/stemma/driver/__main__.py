"""`stemma` (or `python -m stemma.driver`): start [--frame-only] <task.json> |
resume <run_dir> | status <run_dir> | attach <run_id> <file> | report <run_dir> <out_dir> |
verify <run_dir> | new "QUESTION".

`report`, `verify` and `new` belong to the product part (builder W3) and are handed over
whole to its `main(argv)`; when that module is not in this build, the command refuses on
stderr with exit code 2, never a silent success.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from stemma.driver.config import DriverConfig, runs_dir_from_env
from stemma.driver.errors import DriverError, RunStateError
from stemma.driver.runfolder import RUN_ID, create_run
from stemma.driver.runner import run_to_end, show_status

# Where each product command lives (assumption, read from W3's branch
# `claude/v2-product-111b83`, whose `run.py` calls these: `stemma.report.site.main`,
# `stemma.report.verify.main`, `stemma.onboard.new.main`, each `main(argv) -> int`).
# Invariant: the command hands its arguments over unparsed; the module owns its options.
PRODUCT: Final[dict[str, str]] = {
    "report": "stemma.report.site",
    "verify": "stemma.report.verify",
    "new": "stemma.onboard.new",
}


def main(argv: list[str] | None = None) -> int:
    args_in = sys.argv[1:] if argv is None else list(argv)
    if args_in[:1] and args_in[0] in PRODUCT:  # their options are theirs to parse
        return _product(args_in[0], args_in[1:])
    ap = argparse.ArgumentParser(prog="stemma")
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
    p_resume.add_argument("--reason", required=True, help="operator reason, recorded verbatim")
    p_resume.add_argument("--grant-review-round-from", type=int, default=None,
                          help="grant one extra round, conditional on this current limit")
    p_resume.add_argument("--grant-id", default=None,
                          help="stable operator grant id; retries never grant twice")
    p_status = sub.add_parser("status", help="print a run's status")
    p_status.add_argument("run_dir", type=Path)
    p_attach = sub.add_parser("attach", help="hand the running lead a new file")
    p_attach.add_argument("run_id", help="a run id under $STEMMA_RUNS")
    p_attach.add_argument("file", type=Path)
    for cmd, text in (("report", "a static report of a run: report <run_dir> <out_dir>"),
                      ("verify", "re-check a recorded run offline: verify <run_dir>"),
                      ("new", 'onboarding, ending in task.json: new "QUESTION"')):
        p = sub.add_parser(cmd, help=text, add_help=False)
        p.add_argument("rest", nargs=argparse.REMAINDER)
    args = ap.parse_args(args_in)
    if args.cmd in PRODUCT:
        return _product(args.cmd, list(args.rest))
    if args.cmd == "attach":
        return _attach(args.run_id, args.file)
    try:
        cfg = DriverConfig.from_env(args.plugin_src)
        if args.cmd == "start":
            run_dir = create_run(cfg, args.task,
                                 stop_after="frame" if args.frame_only else None)
            print(run_dir, flush=True)
            return run_to_end(cfg, run_dir, resume_first=False)
        if args.cmd == "resume":
            return run_to_end(cfg, args.run_dir, resume_first=True, operator_reason=args.reason,
                              grant_from=args.grant_review_round_from, grant_id=args.grant_id)
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


def _product(cmd: str, rest: list[str]) -> int:
    """Hand `stemma <cmd> rest...` to the product module's `main(rest)`, or refuse."""
    module = PRODUCT[cmd]
    try:
        mod = importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name is not None and (module == exc.name or module.startswith(exc.name + ".")):
            print(f"stemma {cmd} is not installed in this build: {module} is missing",
                  file=sys.stderr)
        else:
            print(f"stemma {cmd} cannot start: {module} needs {exc.name}, which is missing",
                  file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - any import failure is a loud refusal
        print(f"stemma {cmd} cannot start: importing {module} failed: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    entry: Callable[[list[str]], Any] | None = getattr(mod, "main", None)
    if not callable(entry):
        print(f"stemma {cmd} is not installed in this build: {module} has no main()",
              file=sys.stderr)
        return 2
    code = entry(rest)
    if isinstance(code, bool) or not isinstance(code, int):
        print(f"stemma {cmd}: {module}.main returned {code!r}, not an exit code",
              file=sys.stderr)
        return 2
    return code


if __name__ == "__main__":
    sys.exit(main())
