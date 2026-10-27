"""Stemma 2: one entry point.

    python run.py                          SAMPLE_MODE=true: replay the recorded sample run
    python run.py replay [PACK]            replay a recorded run (default: the bundled sample)
    python run.py ask "QUESTION" [--as-of YYYY-MM-DD]    run a real question (live)
    python run.py status RUN_DIR           what is running, what landed, what waits on you
    python run.py logs RUN_DIR             the judge's log: models, Engine, Zetaris, answer
    python run.py conformance              World Model's conformance suite, live
    python run.py record RUN_DIR PACK_DIR  record a finished run as a replayable pack
    python run.py cleanup RUN_DIR          remove a finished run's copies from Zetaris and Postgres
    python run.py verify RUN_DIR           re-check a recorded run offline: hashes, quotes, sums
    python run.py report RUN_DIR OUT_DIR   a static report of a run (index.html, for GitHub Pages)
    python run.py tamper RUN_DIR           change one byte of a copy; watch verify refuse it
    python run.py new "QUESTION"           onboarding: scope, readings, criteria -> task.json

A replay is never shown as live: every recorded line is marked [replay] with its file's
SHA-256, and only what runs now (the conformance suite, the data layer over the replayed
evidence) is marked [live]. A live question needs the Claude Code settings in
`.env.example`; Zetaris (`STEMMA_DATALAYER=zetaris`) and Nightshift are optional.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
for p in (REPO, REPO / "src"):  # runs from a fresh clone without installing
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

SAMPLE_PACK = REPO / "hackathon" / "sample" / "pack"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _runs_dir() -> Path:
    return Path(os.environ.get("STEMMA_RUNS") or REPO / "runs")


def cmd_replay(pack: Path, conformance: bool) -> int:
    from hackathon.replay import ReplayError
    from hackathon.sample import run_sample

    if not (pack / "manifest.json").exists():
        print(f"No recorded run at {pack}. Record one with: python run.py record RUN_DIR "
              f"{pack}", file=sys.stderr)
        return 2
    try:
        result = run_sample(pack, _runs_dir(), conformance)
    except ReplayError as exc:
        print(f"replay refused: {exc}", file=sys.stderr)
        return 2
    print(result.log)
    print(f"[live] replay folder: {result.folder} (log saved as JUDGE-LOG.md)")
    return 0 if result.ok else 1


def cmd_ask(question: str, as_of: str | None) -> int:
    if _truthy(os.environ.get("SAMPLE_MODE")):
        print("SAMPLE_MODE is on, so nothing live runs: `python run.py` replays the recorded "
              "sample. Set SAMPLE_MODE=false and the settings in .env.example to ask a "
              "question.", file=sys.stderr)
        return 2
    from stemma.driver.config import DriverConfig
    from stemma.driver.errors import DriverError
    from stemma.driver.runfolder import create_run
    from stemma.driver.runner import start

    task: dict[str, object] = {"question": question}
    if as_of:
        task["as_of"] = as_of
    with tempfile.TemporaryDirectory(prefix="stemma-task-") as td:
        path = Path(td) / "task.json"
        path.write_text(json.dumps(task), encoding="utf-8")
        try:
            cfg = DriverConfig.from_env(None)
            run_dir = create_run(cfg, path)
            print(f"[live] run folder: {run_dir}", flush=True)
            code = start(cfg, run_dir)
        except DriverError as exc:
            print(f"stemma: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    print(f"[live] the run's log: python run.py logs {run_dir}")
    return code


def cmd_status(run_dir: Path) -> int:
    from hackathon.replay import replay_marker

    marker = replay_marker(run_dir)
    if marker is not None:
        print(json.dumps({"mode": "replay", "notice": marker["notice"],
                          "source_run_id": marker["source_run_id"],
                          "recorded_at": marker["recorded_at"]}, indent=1))
        status = run_dir / "status.json"
        if status.exists():
            print("[replay] recorded status.json:")
            print(status.read_text(encoding="utf-8"))
        return 0
    from stemma.driver.config import DriverConfig
    from stemma.driver.errors import DriverError
    from stemma.driver.runner import show_status

    try:
        print(json.dumps(show_status(DriverConfig.from_env(None), run_dir), indent=1))
    except DriverError as exc:
        print(f"stemma: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


def cmd_logs(run_dir: Path) -> int:
    from hackathon.judgelog import judge_log

    print(judge_log(run_dir, runs_dir=_runs_dir()))
    return 0


def cmd_conformance() -> int:
    from hackathon.conformance import run_conformance, save_run

    result = run_conformance()
    for label, line in result.labelled("live"):
        print(f"[{label}] {line}")
    print(f"[live] result kept at {save_run(result, _runs_dir())}")
    return result.exit_code


def cmd_cleanup(run_dir: Path) -> int:
    from stemma.datalayer import cleanup
    from stemma.datalayer.errors import DataLayerError

    try:
        removed = cleanup(run_dir)
    except DataLayerError as exc:
        print(f"cleanup stopped: {exc}", file=sys.stderr)
        return 2
    for item in removed:
        print(f"[live] removed {item}")
    return 0


def cmd_record(run_dir: Path, pack: Path) -> int:
    from hackathon.replay import ReplayError, record

    try:
        manifest = record(run_dir, pack)
    except ReplayError as exc:
        print(f"record refused: {exc}", file=sys.stderr)
        return 2
    print(f"recorded {len(manifest['items'])} files from run {manifest['source_run_id']} "
          f"into {pack}; left out {len(manifest['left_out'])} (listed in manifest.json)")
    return 0


def cmd_verify(run_dir: Path, as_json: bool) -> int:
    from stemma.report.verify import main as verify_main

    return verify_main([str(run_dir), *(["--json"] if as_json else [])])


def cmd_report(run_dir: Path, out_dir: Path, with_verify: bool) -> int:
    from stemma.report.site import main as report_main

    return report_main([str(run_dir), str(out_dir), *(["--verify"] if with_verify else [])])


def cmd_tamper(run_dir: Path, what: str, keep: bool) -> int:
    from stemma.report.tamper import main as tamper_main

    return tamper_main([str(run_dir), "--what", what, *(["--keep"] if keep else [])])


def cmd_new(rest: list[str]) -> int:
    from stemma.onboard.new import main as new_main

    return new_main(rest)


def main(argv: list[str] | None = None) -> int:
    args_in = sys.argv[1:] if argv is None else argv
    if args_in[:1] == ["new"]:  # onboarding has its own options; see `run.py new -h`
        return cmd_new(args_in[1:])
    ap = argparse.ArgumentParser(prog="python run.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    rp = sub.add_parser("replay", help="replay a recorded run")
    rp.add_argument("pack", nargs="?", type=Path, default=SAMPLE_PACK)
    rp.add_argument("--no-conformance", action="store_true")
    ak = sub.add_parser("ask", help="run a real question")
    ak.add_argument("question")
    ak.add_argument("--as-of", default=None)
    st = sub.add_parser("status", help="a run's status")
    st.add_argument("run_dir", type=Path)
    lg = sub.add_parser("logs", help="the judge's log of a run")
    lg.add_argument("run_dir", type=Path)
    sub.add_parser("conformance", help="World Model's conformance suite, live")
    cu = sub.add_parser("cleanup", help="remove a finished run's Zetaris and Postgres copies")
    cu.add_argument("run_dir", type=Path)
    rc = sub.add_parser("record", help="record a finished run as a replayable pack")
    rc.add_argument("run_dir", type=Path)
    rc.add_argument("pack", type=Path)
    vf = sub.add_parser("verify", help="re-check a recorded run offline, with no keys")
    vf.add_argument("run_dir", type=Path)
    vf.add_argument("--json", action="store_true")
    rpt = sub.add_parser("report", help="a static report of a run, for GitHub Pages")
    rpt.add_argument("run_dir", type=Path)
    rpt.add_argument("out_dir", type=Path)
    rpt.add_argument("--verify", action="store_true", help="show verify's result on the page")
    tp = sub.add_parser("tamper", help="the tamper demo, on a copy of a recorded run")
    tp.add_argument("run_dir", type=Path)
    tp.add_argument("--what", choices=("source", "figure"), default="source")
    tp.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd is None:
        if _truthy(os.environ.get("SAMPLE_MODE")):
            return cmd_replay(SAMPLE_PACK, True)
        ap.print_help()
        return 0
    if args.cmd == "replay":
        return cmd_replay(args.pack, not args.no_conformance)
    if args.cmd == "ask":
        return cmd_ask(args.question, args.as_of)
    if args.cmd == "status":
        return cmd_status(args.run_dir)
    if args.cmd == "logs":
        return cmd_logs(args.run_dir)
    if args.cmd == "conformance":
        return cmd_conformance()
    if args.cmd == "cleanup":
        return cmd_cleanup(args.run_dir)
    if args.cmd == "verify":
        return cmd_verify(args.run_dir, args.json)
    if args.cmd == "report":
        return cmd_report(args.run_dir, args.out_dir, args.verify)
    if args.cmd == "tamper":
        return cmd_tamper(args.run_dir, args.what, args.keep)
    return cmd_record(args.run_dir, args.pack)


if __name__ == "__main__":
    sys.exit(main())
