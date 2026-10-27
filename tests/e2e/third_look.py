"""The third look (N1) on a COPY of a recorded run, exactly as the feature runs it.

    python tests/e2e/third_look.py <run folder> <second-family results.json>

Not collected by pytest; a live run (same environment variables as run_e2e.py). The
script copies the run into $STEMMA_RUNS, re-assembles its plugin from this repository
(so the claim checker's current text, which handles recheck:<claim_id>, is the one
used), records each disagreement from the results file through
Register.record_second_family (which writes the assignment and requests the check, as
the driver does), and launches only those recheck:<claim_id> checkers through the
driver's own launch path (canary, settings, verification), none of the run's other
pending work. It prints each claim's verdict before and after, the third look's
evidence, and the sessions' token use.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from stemma.driver import assemble, events, usage
from stemma.driver.config import PLUGIN_DIR, DriverConfig, Limits
from stemma.driver.runfolder import new_run_id
from stemma.driver.runner import Driver
from stemma.driver.task import fill_switches
from stemma.register import checks_api
from stemma.register import second_family as sf
from stemma.register.store import Register

REPO = Path(__file__).resolve().parents[2]


def prepare(cfg: DriverConfig, src: Path) -> Path:
    rid = new_run_id()
    run = cfg.runs_dir / rid
    shutil.copytree(src, run, symlinks=True,
                    ignore=shutil.ignore_patterns("*.lock", "sessions", "settings",
                                                  ".stemma-tmp"))
    for stale in (".stemma/driver-state.json", ".stemma/stop-hook.json"):
        (run / stale).unlink(missing_ok=True)
    shutil.rmtree(run / PLUGIN_DIR, ignore_errors=True)
    switches = fill_switches({})
    rj = json.loads((run / "run.json").read_text())
    switches["s5_models"] = rj["models"]
    asm = assemble.assemble(cfg.plugin_src, run / PLUGIN_DIR, switches)
    rj.update({"run_id": rid, "switches": switches, "plugin": asm.to_json(),
               "limits": Limits().to_json(), "second_family": {"enabled": False}})
    (run / "run.json").write_text(json.dumps(rj, indent=1))
    return run


def third_look_verdict(after: dict[str, Any] | None, recheck_sessions: set[str]
                       ) -> str | None:
    """The third look's verdict: the claim's verdict only when a recheck session for it
    filed it. A checker that failed or filed nothing leaves the earlier verdict in
    place, which is no third look (Codex re-check E)."""
    if not after or after.get("session") not in recheck_sessions:
        return None
    return str(after.get("verdict"))


def main(src: str, results: str) -> int:
    cfg = DriverConfig.from_env(REPO / "plugin")
    run = prepare(cfg, Path(src))
    print(f"run folder: {run}", flush=True)
    reg = Register(run)
    data = json.loads(Path(results).read_text())
    disagreements = [r for r in data["results"] if r.get("disagrees")]
    before: dict[str, Any] = {}
    for r in disagreements:
        cid = r["claim_id"]
        before[cid] = reg.get_claim(cid)["verdict"]
        reg.record_second_family(cid, sf.Answer(
            r["second_family"], r["reason"], sf.PRIMARY_MODEL, r.get("served_model"), {}))
    d = Driver(cfg, run, checks_api, Limits())
    todo = [q for q in checks_api.pending_checks(run) if q.prompt.startswith("recheck:")]
    started = time.time()
    while todo or d.live:
        while todo and d._live_checks() < d.limits.max_concurrent_checks:
            d._launch_check(todo.pop(0), relaunch=False)
        d._police(time.monotonic())
        for name, live in list(d.live.items()):
            if not live.session.running():
                d._end(name, live)
            else:
                d._scan_transcripts(live, final=False)
        time.sleep(2)
    reg = Register(run)
    launched = [e for e in events.read(run) if e.get("event") == "session_launched"]
    out = []
    for r in disagreements:
        cid = r["claim_id"]
        after = reg.get_claim(cid)
        mine = {str(e.get("session_id")) for e in launched
                if e.get("prompt") == f"recheck:{cid}"}
        look = third_look_verdict(after["verdict"], mine)
        out.append({"claim_id": cid, "statement": after["statement"],
                    "claude_before": (before[cid] or {}).get("verdict"),
                    "second_family": r["second_family"], "second_family_reason": r["reason"],
                    "third_look": look if look is not None else "missing",
                    "third_look_evidence": (after["verdict"] or {}).get("evidence")
                    if look is not None else None,
                    "corrected_claim_id": after.get("superseded_by")})
    recs = [x for x in usage.read_records(run) if x.get("kind") == "session"
            and str(x.get("prompt", "")).startswith("recheck:")]
    summary = {
        "wall_s": round(time.time() - started),
        "sessions": len(recs),
        "missing": sum(1 for o in out if o["third_look"] == "missing"),
        "changed": sum(1 for o in out if o["third_look"] not in ("missing",
                                                                 o["claude_before"])),
        "verdicts": {o["claim_id"][:12]: [o["claude_before"], o["third_look"]] for o in out},
        "tokens": usage.summary(recs).get("tokens") if recs else None,
        "cost_estimate_usd": usage.summary(recs).get("cost_estimate_usd") if recs else None,
        "ended": [{k: e.get(k) for k in ("session", "outcome")}
                  for e in events.read(run) if e.get("event") == "session_ended"],
    }
    (run / "third-look-results.json").write_text(json.dumps({"summary": summary,
                                                             "claims": out}, indent=1))
    print(json.dumps(summary, indent=1))
    print(f"details: {run / 'third-look-results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
