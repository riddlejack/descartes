"""SAMPLE_MODE: replay a recorded run, then run live only what needs no credentials.

Live now: World Model's conformance suite against the register's engine, and the data
layer over the replayed evidence (a sync into SQLite, or Zetaris when configured, and one
read-only query through `query_data`). Everything else is the recorded run, labelled
replay with its hash. Needs no Zetaris, no Nightshift and no credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hackathon.conformance import Conformance, run_conformance
from hackathon.judgelog import judge_log
from hackathon.replay import replay

LIVE_QUERY = ("SELECT c.claim_id, c.tier, c.verdict, count(s.receipt_id) AS sources "
              "FROM claim c LEFT JOIN claim_support s ON s.claim_id = c.claim_id "
              "GROUP BY c.claim_id, c.tier, c.verdict ORDER BY c.claim_id")


@dataclass(frozen=True)
class SampleRun:
    folder: Path
    log: str
    conformance: Conformance | None

    @property
    def ok(self) -> bool:
        return self.conformance is None or self.conformance.passed


def _live_data_layer(folder: Path) -> list[tuple[str, str]]:
    from stemma.datalayer.errors import DataLayerError
    from stemma.datalayer.query import run_query

    try:
        out = run_query(folder, LIVE_QUERY)
    except DataLayerError as exc:
        return [("live", f"the data layer did not run: {exc}")]
    lines = [("live", (f"query_data via {out['backend']} over the replayed rows: "
              f"{out['row_count']} rows ({', '.join(out['columns'])})"))]
    lines += [("live", "  " + " | ".join(str(v) for v in row)) for row in out["rows"][:20]]
    return lines


def run_sample(pack: Path, runs_dir: Path, conformance: bool = True) -> SampleRun:
    rp = replay(pack, runs_dir)
    conf = run_conformance() if conformance else None
    live = _live_data_layer(rp.folder)
    log = judge_log(rp.folder, conf.labelled() if conf else None)
    log += "\n## Live now, over the replayed evidence\n" + "\n".join(
        f"[{label}] {text}" for label, text in live) + "\n"
    (rp.folder / "JUDGE-LOG.md").write_text(log, encoding="utf-8")
    return SampleRun(rp.folder, log, conf)
