"""AI providers other than the Claude Code CLI the driver runs.

`nightshift`: Meterless Nightshift's Runtime, an OpenAI-compatible local endpoint.
Whether Claude routes through it without loss is the pre-registered stage-2 test in
`docs/notes/hackathon.md`; until it passes, no research call goes through it.
"""

from pathlib import Path

from stemma.datalayer.querylog import QueryLog

RECEIPTS_REL = Path("datalayer", "providers.jsonl")


def receipt_log(run_dir: Path) -> QueryLog:
    """Where a run's provider receipts go (the judge's log reads them from here)."""
    return QueryLog(RECEIPTS_REL, root=run_dir)
