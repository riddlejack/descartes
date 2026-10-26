"""The judge's log of a run: what ran, on which model, what the Engine did, what Zetaris
was asked, and the answer, every line labelled `[live]` or `[replay sha:...]`.

Read only from the run folder's own records, never from memory or a summary:
- the model behind each request: the driver's `session_launched` and
  `resolved_model_effort` events (`events.jsonl`), and any Nightshift receipts
  (`datalayer/providers.jsonl`);
- the Engine (Meterless World Model): each transaction in the register's log
  (`register/log.jsonl`), the register tool calls (`events.jsonl`), and the
  conformance suite's result when it ran;
- Zetaris: each query in `datalayer/queries.jsonl`, with engine, purpose and row count;
- the answer: `deliverable/answer.reader.md`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hackathon.replay import labelled_text, replay_marker

# Resource limit: characters of SQL shown per query line (the full text is in the log).
SQL_CHARS = 160


def _records(run_dir: Path, rel: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for label, line in labelled_text(run_dir, rel):
        if line.strip():
            try:
                rec = json.loads(line)
            except ValueError:
                rec = {"unreadable_line": line[:80]}
            out.append((label, rec if isinstance(rec, dict) else {"value": rec}))
    return out


def _line(label: str, text: str) -> str:
    return f"[{label}] {text}"


def header(run_dir: Path) -> list[str]:
    marker = replay_marker(run_dir)
    if marker is None:
        return ["# Run log (live run)", f"Run folder: {run_dir}"]
    return ["# Run log: REPLAY, not a live run",
            (f"Recorded run {marker['source_run_id']} (recorded {marker['recorded_at']}), "
            f"replayed {marker['replayed_at']}. Every [replay] line comes from a recorded "
            "file whose SHA-256 is shown and checked; only [live] lines ran now.")]


def models(run_dir: Path) -> list[str]:
    out = ["", "## The model behind each request"]
    for label, e in _records(run_dir, "events.jsonl"):
        if e.get("event") == "session_launched":
            out.append(_line(label, f"launched {e.get('role')} ({e.get('session')}): "
                             f"model {e.get('model')}, effort {e.get('effort')}, prompt "
                             f"{e.get('prompt')}"))
        elif e.get("event") == "resolved_model_effort":
            ok = "matches" if e.get("match") else "DOES NOT MATCH"
            out.append(_line(label, f"verified {e.get('session')} ({e.get('scope')}): ran on "
                             f"{e.get('model')}, effort {e.get('effort')}, {ok} what was set"))
        elif e.get("event") == "model_effort_mismatch":
            out.append(_line(label, f"MISMATCH {json.dumps(e, sort_keys=True)[:200]}"))
    for label, r in _records(run_dir, "datalayer/providers.jsonl"):
        out.append(_line(label, f"{r.get('provider')} {r.get('purpose')}: route "
                         f"{r.get('route')} answered by {r.get('served_model')}"
                         f"{'' if r.get('ok') else ' (FAILED)'}"))
    if len(out) == 2:
        out.append("(no model events recorded)")
    return out


def engine(run_dir: Path, conformance: list[tuple[str, str]] | None = None) -> list[str]:
    out = ["", "## The Engine: Meterless World Model (the claim register)"]
    for label, tx in _records(run_dir, "register/log.jsonl"):
        out.append(_line(label, f"tx {tx.get('tx')} {tx.get('op')} by {tx.get('actor')}: "
                         f"{str(tx.get('summary'))[:120]}"))
    calls: dict[tuple[str, str, str], int] = {}
    for label, e in _records(run_dir, "events.jsonl"):
        if e.get("event") in ("register_write", "register_read", "denied"):
            key = (label, str(e.get("event")), str(e.get("tool")))
            calls[key] = calls.get(key, 0) + 1
    for (label, kind, tool), n in sorted(calls.items()):
        out.append(_line(label, f"{kind} {tool} x{n}"))
    if conformance is not None:
        out.append("World Model's own conformance suite, unmodified, against this code:")
        out += [_line(label, text) for label, text in conformance]
    return out


def zetaris(run_dir: Path) -> list[str]:
    out = ["", "## Data layer: every query (Zetaris when configured, SQLite otherwise)"]
    n = 0
    for label, q in _records(run_dir, "datalayer/queries.jsonl"):
        if q.get("purpose") in ("sync", "warning"):
            out.append(_line(label, f"{q.get('engine')} {q.get('purpose')}: "
                             f"{json.dumps(q.get('rows') or q.get('warning'))[:160]}"))
            continue
        n += 1
        sql = " ".join(str(q.get("sql", "")).split())
        sql = sql if len(sql) <= SQL_CHARS else sql[:SQL_CHARS] + "..."
        status = f"{q.get('rows')} rows" if q.get("ok", True) else f"FAILED {q.get('error')}"
        out.append(_line(label, f"{q.get('engine')} {q.get('purpose')}"
                         f"{' via ' + str(q['backend']) if q.get('backend') else ''}: "
                         f"{status}, {q.get('ms', '?')} ms: {sql}"))
    if n == 0:
        out.append("(no queries recorded)")
    return out


def answer(run_dir: Path) -> list[str]:
    out = ["", "## The answer (deliverable/answer.reader.md)"]
    lines = labelled_text(run_dir, "deliverable/answer.reader.md")
    out += [_line(label, text) for label, text in lines] or ["(no deliverable)"]
    return out


def judge_log(run_dir: Path, conformance: list[tuple[str, str]] | None = None) -> str:
    parts = header(run_dir) + models(run_dir) + engine(run_dir, conformance) \
        + zetaris(run_dir) + answer(run_dir)
    return "\n".join(parts) + "\n"
