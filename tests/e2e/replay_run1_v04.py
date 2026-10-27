"""Replay run 1's recorded state under contracts v0.4 (docs/notes/register-driver-v2.md).

    python tests/e2e/replay_run1_v04.py <a COPY of run 1's folder>

Nothing reruns and nothing is written to the run folder given: it is copied first. The
replay reports, from what run 1 recorded:
- the tree: which nodes v0.4's tree_upsert refuses (kind), and how the rest close;
- claim checks (D7): checks run 1 started on filing, against one checker per source
  document for the claims each submitted draft cited and had no verdict yet;
- send-backs (D9): branch verdicts that sent a branch back;
- final-review points (D4, D9): material points per version.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from stemma.register import checks_api, tags
from stemma.register.store import (
    NODE_KINDS,
    SEND_BACK,
    Register,
    closure,
)


def tree_replay(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    refused = [n for n in nodes if n["kind"] not in NODE_KINDS]
    # A v0.4 lead keeps the sub-questions (each old condition, as a question) and drops
    # the options and rivals; a node whose parent is dropped becomes a root.
    kept = {n["id"]: {**n, "kind": "question" if n["kind"] == "condition" else n["kind"]}
            for n in nodes if n["kind"] in ("condition", "open_lane")}
    for n in kept.values():
        if n.get("parent") not in kept:
            n["parent"] = None
    info = closure(list(kept.values()))
    return {
        "nodes": len(nodes),
        "refused_by_kind": sorted(f"{n['id']} ({n['kind']})" for n in refused),
        "kept": len(kept),
        "roots_after_drop": sorted(n["id"] for n in kept.values() if n["parent"] is None),
        "closed": sorted(k for k, v in info.items() if v["closed"]),
        "open": sorted(f"{k} ({v['closes_by']}, {v['status']})" for k, v in info.items()
                       if not v["closed"]),
    }


def claims_replay(reg: Register) -> dict[str, Any]:
    states = list(checks_api.states(reg.run_dir).values())
    started = [s for s in states if s.request.role == "claim-checker"]
    claims = {c["claim_id"]: reg.get_claim(c["claim_id"])
              for c in reg.find_claims(include_superseded=True)}
    versions = reg.submitted_versions()
    verdicted: set[str] = set()
    per_version: list[dict[str, Any]] = []
    cited_any: set[str] = set()
    for v in versions:
        text = (reg.run_dir / f"register/drafts/answer.v{v}.md").read_text(encoding="utf-8")
        cited: list[str] = []
        for short in dict.fromkeys(tags.claim_tags(text)):
            hit = [cid for cid in claims if cid.startswith(short)]
            if len(hit) == 1:
                cited.append(hit[0])
        cited_any.update(cited)
        due = [c for c in reg._due_for_checking(cited)
               if c not in verdicted and not claims[c]["superseded_by"]]
        docs = Counter()
        alone = 0
        for c in due:
            home = reg.home_document(claims[c])  # the rule submit_draft applies
            if home is not None:
                docs[home] += 1
            else:
                alone += 1
        per_version.append({"version": v, "cited": len(cited), "due": len(due),
                            "doc_sessions": len(docs), "derivation_claims": alone,
                            "derivation_sessions": 1 if alone else 0})
        verdicted.update(due)  # as if every check landed before the next draft
    return {
        "claims_filed": len(claims),
        "checks_started_run1": len(started),
        "claims_cited_by_any_draft": len(cited_any),
        "checks_on_claims_no_draft_cited": sum(
            1 for s in started if s.request.prompt.removeprefix("claim:") not in cited_any),
        "per_version": per_version,
        "sessions_v04": sum(p["doc_sessions"] + p["derivation_sessions"] for p in per_version),
    }


def reviews_replay(reg: Register) -> dict[str, Any]:
    out = []
    for v in reg.submitted_versions():
        have = reg._recorded_reviews(v)
        pts = [p for r in have.values() for p in r.get("points", [])]
        out.append({"version": v, "reviews": len(have),
                    "material": sum(1 for p in pts if p["material"]),
                    "minor": sum(1 for p in pts if not p["material"])})
    sends = [f for f in reg._run_facts("branch_verdict_recorded")]
    verdicts = []
    for p in sorted((reg.run_dir / "branches").glob("*/verdicts/*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        verdicts.append((d["branch_id"], d["round"], d["verdict"]))
    return {"final_reviews": out, "branch_verdicts": verdicts,
            "send_backs": sum(1 for _b, _r, v in verdicts if v in SEND_BACK),
            "v04_facts": len(sends)}


def main(src: str) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        run = Path(tmp) / "run"
        shutil.copytree(src, run, symlinks=True)
        nodes = json.loads((run / "tree.json").read_text(encoding="utf-8"))["nodes"]
        reg = Register(run)
        report = {"tree": tree_replay(nodes), "claims": claims_replay(reg),
                  "reviews": reviews_replay(reg)}
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
