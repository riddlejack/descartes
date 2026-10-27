"""Replay N1 (the second family) over run 1's recorded Claude verdicts.

    python tests/e2e/replay_n1.py <a COPY of run 1's folder> [--live] [--out results.json]
                                  [--limit N] [--models m1,m2]

Without --live (the default) nothing leaves this machine: every question is built from
the run's own stored passages exactly as the driver would build it, and the script
reports how many claims would be asked, with one question shown. With --live it asks
Nightshift (settings from the environment: METERLESS_BASE_URL, METERLESS_RUNTIME_TOKEN
or METERLESS_STATE_DIR), at most two at a time, and writes every answer beside Claude's
verdict, with the disagreements counted. The run folder given is copied first and never
written. Not collected by pytest.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from stemma.register import second_family as sf
from stemma.register.store import Register

# Resource limit: questions in flight at once, as in the driver.
CONCURRENCY = 2


def claims_with_verdicts(reg: Register) -> list[dict[str, Any]]:
    """Every claim Claude recorded a verdict on that rests on a stored passage."""
    out = []
    for c in reg.find_claims(include_superseded=True):
        rec = reg.get_claim(c["claim_id"])
        if rec["verdict"] is not None and rec.get("support"):
            out.append(rec)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--models", default=f"{sf.PRIMARY_MODEL},{sf.FALLBACK_MODEL}")
    args = ap.parse_args(argv)
    models = tuple(m for m in args.models.split(",") if m)
    with tempfile.TemporaryDirectory() as tmp:
        run = Path(tmp) / "run"
        shutil.copytree(args.run, run, symlinks=True)
        reg = Register.read_only(run)
        claims = claims_with_verdicts(reg)[: args.limit]
        questions = [(c, sf.messages(c, sf.passages(run, c, reg.evidence))) for c in claims]
        unplaced = sum(1 for c, _m in questions
                       for p in sf.passages(run, c, reg.evidence) if p["placed"] != "yes")
        summary: dict[str, Any] = {
            "claims_with_claude_verdicts": len(claims),
            "claude_verdicts": dict(Counter(c["verdict"]["verdict"] for c in claims)),
            "passages_not_placed": unplaced, "models": list(models), "live": args.live}
        if not args.live:
            if questions:
                summary["sample_question"] = questions[0][1]
            print(json.dumps(summary, indent=1, ensure_ascii=False))
            return 0
        from stemma.datalayer.querylog import QueryLog
        from stemma.providers.nightshift import NightshiftClient

        asker = sf.nightshift_asker(NightshiftClient.from_env(QueryLog()))
        results: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(CONCURRENCY) as pool:
            futs = {pool.submit(sf.ask, asker, run, c, reg.evidence, models): c for c in claims}
            for fut in concurrent.futures.as_completed(futs):
                c = futs[fut]
                claude = c["verdict"]["verdict"]
                try:
                    a = fut.result()
                    results.append({"claim_id": c["claim_id"], "claude": claude,
                                    "second_family": a.verdict, "reason": a.reason,
                                    "served_model": a.served_model,
                                    "usage": a.receipt.get("usage") or {},
                                    "ms": a.receipt.get("ms"),
                                    "disagrees": sf.disagrees(claude, a.verdict)})
                except (sf.SecondFamilyUnavailable, sf.SecondFamilyFailed) as exc:
                    results.append({"claim_id": c["claim_id"], "claude": claude,
                                    "unavailable": str(exc)})
        summary["answers"] = dict(Counter(r.get("second_family", "unavailable")
                                          for r in results))
        summary["disagreements"] = sum(1 for r in results if r.get("disagrees"))
        tokens: Counter[str] = Counter()
        for r in results:
            for k, v in (r.get("usage") or {}).items():
                if isinstance(v, int):
                    tokens[k] += v
        summary["tokens"] = dict(tokens)  # the cost, as the receipts report it
        ms = [r["ms"] for r in results if isinstance(r.get("ms"), int)]
        summary["seconds_per_answer"] = round(sum(ms) / len(ms) / 1000, 1) if ms else None
        summary["served_models"] = dict(Counter(r.get("served_model") for r in results
                                                if "served_model" in r))
        summary["by_pair"] = dict(Counter(f"{r['claude']} / {r.get('second_family', '-')}"
                                          for r in results))
        out = {"summary": summary, "results": sorted(results, key=lambda r: r["claim_id"])}
        if args.out:
            args.out.write_text(json.dumps(out, indent=1, ensure_ascii=False))
        print(json.dumps(summary, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
