"""The `stemma` MCP server over stdio, with identity from the hook's stamp only."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import anyio
from evidence_kit import capture_invented
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

TEXT = "Acme Widgets reported revenue of 1.2bn in fiscal 2025."


def make_run(tmp_path: Path) -> tuple[Path, str]:
    """A run whose store holds one invented document, captured by part A's service."""
    run = tmp_path / "run"
    return run, capture_invented(run, "acme-note.txt", TEXT)


def stamp(run: Path, tool_use_id: str, agent_type: str, session: str = "s-1") -> None:
    calls = run / "register/calls"
    calls.mkdir(parents=True, exist_ok=True)
    (calls / f"{tool_use_id}.json").write_text(json.dumps(
        {"agent_type": agent_type, "agent_id": None, "session_id": session}))


async def session_calls(run: Path, calls: list[tuple[str, dict[str, Any], str | None]]
                        ) -> list[tuple[bool, str]]:
    params = StdioServerParameters(
        command=sys.executable, args=["-I", "-m", "stemma.mcp_server"],
        env={"STEMMA_RUN_DIR": str(run), "PATH": "/usr/bin:/bin"},
    )
    out: list[tuple[bool, str]] = []
    async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
        await s.initialize()
        for name, args, tool_use_id in calls:
            meta = {"claudecode/toolUseId": tool_use_id} if tool_use_id else None
            r = await s.call_tool(name, args, meta=meta)
            text = "".join(getattr(c, "text", "") for c in r.content)
            out.append((bool(r.is_error), text))
    return out


def claim_on(receipt_id: str) -> dict[str, Any]:
    return {"statement": "Acme Widgets' fiscal 2025 revenue was 1.2bn.", "tier": "observed",
            "branch_id": "b1",
            "value": {"number": 1.2e9, "kind": "level", "as_written": "1.2bn"},
            "support": [{"receipt_id": receipt_id, "quote": "revenue of 1.2bn in fiscal 2025"}]}


def test_identity_comes_from_the_stamp(tmp_path: Path) -> None:
    run, receipt = make_run(tmp_path)
    CLAIM = claim_on(receipt)
    stamp(run, "toolu_w", "stemma:worker")
    stamp(run, "toolu_c", "stemma:claim-checker")
    forged = dict(CLAIM, statement="Forged author attempt: revenue was 1.2bn.")
    results = anyio.run(session_calls, run, [
        ("file_claim", CLAIM, None),                       # no tool use id: refused
        ("file_claim", CLAIM, "toolu_missing"),            # no stamp for it: refused
        ("file_claim", CLAIM, "../register/calls/toolu_w"),  # path tricks: refused
        ("file_claim", CLAIM, "toolu_c"),                  # checker may not file
        ("file_claim", dict(forged, agent_type="stemma:lead"), "toolu_w"),  # forged arg
        ("file_claim", CLAIM, "toolu_w"),                  # the worker files
    ])
    from stemma.register.store import Register
    reg = Register(run)
    for is_error, text in results[:3]:
        assert is_error and "no harness identity" in text
    assert results[3][0] and "claim-checker sessions do not file claims" in results[3][1]
    assert not results[4][0], results[4][1]
    forged_id = json.loads(results[4][1])["claim_id"]
    assert reg.get_claim(forged_id)["author"] == "stemma:worker"  # the argument was ignored
    assert not results[5][0], results[5][1]
    filed = json.loads(results[5][1])
    assert filed["tag"].startswith("[[c:")
    events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()]
    assert [e["event"] for e in events].count("register_write") == 2
    assert any(e["event"] == "denied" and e["agent_type"] == "stemma:claim-checker"
               for e in events)


def test_server_lists_the_register_tools(tmp_path: Path) -> None:
    run, _ = make_run(tmp_path)

    async def names() -> list[str]:
        params = StdioServerParameters(command=sys.executable,
                                       args=["-I", "-m", "stemma.mcp_server"],
                                       env={"STEMMA_RUN_DIR": str(run), "PATH": "/usr/bin:/bin"})
        async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
            await s.initialize()
            return [t.name for t in (await s.list_tools()).tools]

    have = set(anyio.run(names))
    assert {"file_claim", "record_claim_verdict", "get_claim", "find_claims", "echo_candidates",
            "entity_review", "tree_upsert", "propose_branch_done", "record_branch_verdict",
            "request_frame_critique", "submit_draft", "await_checks", "finish_run"} <= have
