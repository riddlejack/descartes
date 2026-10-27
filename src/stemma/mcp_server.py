"""The one `stemma` MCP server: part A's evidence tools and the register's tools.

Agents see these as `mcp__plugin_stemma_stemma__<tool>`. The server runs outside the
Bash sandbox and finds the run folder through `STEMMA_RUN_DIR`.

Identity: every register tool reads who is calling from the stamp the PreToolUse
guard wrote for this call (`register/calls/<tool_use_id>.json`), matched by the
`claudecode/toolUseId` the harness puts in the request's `_meta`. No tool argument
says who the caller is, and a call with no stamp is refused.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from stemma.register import analysis_run, jsonl
from stemma.register.evidence_port import EvidenceUnavailable
from stemma.register.filing import Refused
from stemma.register.store import Caller, Register, now
from stemma.register.wm import WorldModelError

TOOL_USE_META = "claudecode/toolUseId"


server: MCPServer[Any] = MCPServer(
    "stemma",
    instructions=(
        "Stemma's evidence tools and claim register. Figures cross hand-offs as claim "
        "ids; every check is started by the host."
    ),
)

_registers: dict[Path, Register] = {}


def run_dir() -> Path:
    value = os.environ.get("STEMMA_RUN_DIR", "")
    if not value:
        raise ToolError("Refused: STEMMA_RUN_DIR is not set for this server.")
    return Path(value).resolve()


def register() -> Register:
    rd = run_dir()
    if rd not in _registers:
        _registers[rd] = Register(rd)
    return _registers[rd]


def caller_of(ctx: Context[Any]) -> Caller:
    meta = ctx.request_context.meta or {}
    tool_use_id = meta.get(TOOL_USE_META) if hasattr(meta, "get") else None
    if not isinstance(tool_use_id, str) or not tool_use_id or "/" in tool_use_id \
            or tool_use_id.startswith("."):
        raise ToolError("Refused: this call carries no harness identity.")
    stamp = run_dir() / "register" / "calls" / f"{tool_use_id}.json"
    try:
        data = json.loads(stamp.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolError("Refused: this call carries no harness identity.") from exc
    who = data.get("agent_type")
    if not isinstance(who, str) or not who:
        raise ToolError("Refused: this call carries no harness identity.")
    return Caller(who, data.get("agent_id"), str(data.get("session_id") or ""))


def _event(name: str, caller: Caller, **fields: Any) -> None:
    jsonl.append(run_dir() / "events.jsonl", {
        "event": name, "agent_type": caller.agent_type, "agent_id": caller.agent_id,
        "session_id": caller.session_id, **fields, "at": now()})


def _call[T](ctx: Context[Any], tool: str, fn: Callable[[Register, Caller], T]) -> T:
    return _call_as(caller_of(ctx), tool, fn)


def _call_as[T](caller: Caller, tool: str, fn: Callable[[Register, Caller], T]) -> T:
    try:
        result = fn(register(), caller)
    except Refused as exc:
        _event("denied", caller, tool=tool, refusal=str(exc))
        raise ToolError(str(exc)) from exc
    except (EvidenceUnavailable, WorldModelError) as exc:
        raise ToolError(f"Refused: {tool} could not run ({type(exc).__name__}: {exc}).") from exc
    _event("register_write" if tool in WRITE_TOOLS else "register_read", caller, tool=tool)
    return result


WRITE_TOOLS = frozenset({"file_claim", "record_claim_verdict", "entity_review", "tree_upsert",
                         "propose_branch_done", "record_branch_verdict",
                         "request_frame_critique", "submit_draft", "finish_run",
                         "proceed_on_assumption", "record_frame", "record_review",
                         "revise_criteria"})


@server.tool()
def file_claim(ctx: Context[Any], statement: str, tier: str, branch_id: str | None = None,
               value: dict[str, Any] | None = None, support: list[dict[str, Any]] | None = None,
               derivation: dict[str, Any] | None = None,
               entities: list[str] | None = None) -> dict[str, Any]:
    """File a claim. tier: observed | interested_party | secondary | estimated | assumed.
    value: {number, unit, period, kind (level|count|ratio|share|rate|change), as_written,
    denominator (for ratio, share, rate)}. support: [{receipt_id, quote}] (exact text from
    the receipt). derivation (estimated): {script: "analysis/<file>.py", inputs: [claim
    ids]}. Code checks exact facts and refuses with the failed check. No check starts on
    filing: once a submitted draft cites the claim, the host starts a checker for its
    source document. Returns the claim id and its tag."""
    raw = {"statement": statement, "tier": tier, "branch_id": branch_id, "value": value,
           "support": support or [], "derivation": derivation, "entities": entities or []}
    return _call(ctx, "file_claim", lambda r, c: r.file_claim(c, raw))


@server.tool()
def record_claim_verdict(ctx: Context[Any], claim_id: str, verdict: str, evidence: str,
                         corrected_claim: dict[str, Any] | None = None) -> dict[str, Any]:
    """Claim checker only, for a claim your check lists (checks/doc-<receipt_id>.json for
    a doc:<receipt_id> check): holds | holds_with_correction | not_supported |
    could_not_check, with its evidence. For holds_with_correction, corrected_claim is the
    corrected claim (same fields as file_claim); it supersedes the old one."""
    return _call(ctx, "record_claim_verdict",
                 lambda r, c: r.record_claim_verdict(c, claim_id, verdict, evidence,
                                                     corrected_claim))


@server.tool()
async def run_analysis(ctx: Context[Any], script: str, inputs: list[str] | None = None,
                       documents: list[str] | None = None) -> dict[str, Any]:
    """Lead and workers: run a script under analysis/ ("analysis/<file>.py") exactly as
    file_claim re-runs it: with the modules under its folder it imports, no network, no
    writes outside a temporary folder, a time limit. inputs: the claim ids it reads with
    stemma.analysis.inputs(); documents: the receipt ids it reads with document().
    Returns result (the exact value passed to result(), or null), stdout (capped),
    exit_code, stderr_tail and the bundle's hash and files. Writes nothing to the run."""
    caller = caller_of(ctx)  # read before leaving the request's task

    def go() -> dict[str, Any]:
        return _call_as(caller, "run_analysis",
                        lambda r, c: analysis_run.run_for(r, c, script, inputs, documents))

    return await anyio.to_thread.run_sync(go)


@server.tool()
def check_assignment(ctx: Context[Any]) -> dict[str, Any]:
    """Claim checker only: the exact list of claims your session was started for (your
    prompt, doc:<receipt_id> or derivations:v<n>, and its round), with the file that holds
    it. Record a verdict for each; you may judge no other claim."""
    return _call(ctx, "check_assignment", lambda r, c: r.check_assignment(c))


@server.tool()
def revise_criteria(ctx: Context[Any], text: str, reason: str) -> dict[str, Any]:
    """Lead only: replace criteria.md (one line per criterion, "- C1: ...") once the
    criteria are frozen (they freeze when the first review is asked for), stating why.
    Final reviews made against the earlier criteria no longer count. The user's own
    criteria, from the task, are never revised."""
    return _call(ctx, "revise_criteria", lambda r, c: r.revise_criteria(c, text, reason))


@server.tool()
def get_claim(ctx: Context[Any], claim_id: str) -> dict[str, Any]:
    """One claim by id or 12-hex tag, with its checks, latest verdict and successor."""
    return _call(ctx, "get_claim", lambda r, c: r.get_claim(claim_id))


@server.tool()
def find_claims(ctx: Context[Any], text: str | None = None, branch_id: str | None = None,
                tier: str | None = None, entity: str | None = None,
                include_superseded: bool = False) -> list[dict[str, Any]]:
    """Claims matching every filter given (text is a substring of the statement)."""
    return _call(ctx, "find_claims", lambda r, c: r.find_claims(text, branch_id, tier, entity,
                                                                 include_superseded))


@server.tool()
def echo_candidates(ctx: Context[Any]) -> list[dict[str, Any]]:
    """Live claims that cite the same document. Code lists them; you judge independence."""
    return _call(ctx, "echo_candidates", lambda r, c: r.echo_candidates())


@server.tool()
def claim_dependents(ctx: Context[Any], claim_id: str) -> list[dict[str, Any]]:
    """Every run file that uses a claim's tag, with who last wrote it."""
    return _call(ctx, "claim_dependents",
                 lambda r, c: r.dependents(r.resolve_claim_id(claim_id)))


@server.tool()
def entity_review(ctx: Context[Any], action: str = "list", proposal_id: str = "",
                  reason: str = "", name: str = "", parent: str = "",
                  support: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Near-duplicate entity names. action: list | approve | reject (proposal_id, reason)
    | parent (name, parent, support: two [{receipt_id, quote}] from different sources)."""
    return _call(ctx, "entity_review",
                 lambda r, c: r.entity_review(c, action, proposal_id, reason, name, parent,
                                              support))


@server.tool()
def tree_upsert(ctx: Context[Any], node: dict[str, Any]) -> dict[str, Any]:
    """Create or change one tree node: {id, parent, kind (question|open_lane), statement,
    doubt, settles_by, flip_point, crux, branch_id, status (open|in_progress), set_aside}.
    statement is one short question; doubt, settles_by and flip_point are for leaves.
    Nodes are sub-questions of the question asked; options and rivals are never nodes.
    Lead only: set_aside is the reason a node is set aside (neither possible answer could
    change the headline), or null to bring it back; a set-aside node and everything under
    it counts as closed and is listed by gate_status. Fields left out keep their values.
    Lead only: {id, remove: reason} removes a node, for a duplicate merged into
    another. It is refused while anything holds the node: its children, a branch it has
    or ever had, a register record naming it (a review point, a verdict, an assumption),
    or a branch order or revision order naming it; the refusal names each. Other files
    are not read: the framer's critique or a branch record naming it does not hold it.
    A removed id is not used again. A node id never ends in a full stop."""
    return _call(ctx, "tree_upsert", lambda r, c: r.tree_upsert(c, node))


@server.tool()
def get_tree(ctx: Context[Any]) -> list[dict[str, Any]]:
    """The issue tree's nodes, each with how it closes (closes_by: "branch" for a leaf
    question or an open lane holding work; "children" for any node with children, whose
    status is derived; "nothing" for an empty open lane; "set_aside" or
    "ancestor_set_aside") and closed."""
    return _call(ctx, "get_tree", lambda r, c: r.tree_view())


@server.tool()
def propose_branch_done(ctx: Context[Any], branch_id: str) -> dict[str, Any]:
    """Propose a branch done once branches/<id>/record.md has every heading. The record is
    frozen as a snapshot (returned as record_snapshot) for a branch reviewer session, then
    started; await_checks(["branch:<id>"]) waits for it. Changing record.md after a review
    reopens the branch."""
    return _call(ctx, "propose_branch_done", lambda r, c: r.propose_branch_done(c, branch_id))


@server.tool()
def record_branch_verdict(ctx: Context[Any], branch_id: str, verdict: str, reason: str,
                          method: str | None = None, gap_type: str | None = None,
                          points: list[dict[str, Any]] | None = None,
                          resolutions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Branch reviewer only, judging the record snapshot of your round
    (register/branches/<id>/record.r<round>.md): answered | find_more (method) | analyse |
    bound | ask_user (the reason is the question for the user) | accept_gap (gap_type:
    user_held | source_unavailable | method_exhausted | out_of_scope), with the reason.
    points: [{point, material, node (node id or null), criterion (C-id or null)}], at
    least one of node and criterion set. find_more, analyse and bound send the branch back
    and need a material point; a branch is sent back once. A later answered or
    accept_gap gives resolutions: [{point_id, status: resolved | bounded | conditional |
    disputed, note}] for every point sent back (bounded and conditional: the record names
    the point id; disputed: a revision order of the branch names it)."""
    return _call(ctx, "record_branch_verdict",
                 lambda r, c: r.record_branch_verdict(c, branch_id, verdict, reason, method,
                                                      gap_type, points, resolutions))


@server.tool()
def proceed_on_assumption(ctx: Context[Any], branch_id: str, assumption: str) -> dict[str, Any]:
    """Lead only: a branch waiting on the user returns to in_progress on the stated
    assumption. The deliverable must list the assumption; the branch reviewer may later
    accept the gap as user_held."""
    return _call(ctx, "proceed_on_assumption",
                 lambda r, c: r.proceed_on_assumption(c, branch_id, assumption))


@server.tool()
def request_frame_critique(ctx: Context[Any]) -> dict[str, Any]:
    """Lead only: start the blind framer's critique of frame.md and the tree."""
    return _call(ctx, "request_frame_critique", lambda r, c: r.request_frame_critique(c))


@server.tool()
def submit_draft(ctx: Context[Any], version: int) -> dict[str, Any]:
    """Lead only: freeze draft/answer.v<version>.md and start the three final reviews,
    and a claim checker for each source document the draft's cited claims rest on (claims
    with a verdict are not checked again). Each submission is one review round; after
    max_review_rounds (run.json, a resource limit) it refuses another."""
    return _call(ctx, "submit_draft", lambda r, c: r.submit_draft(c, version))


@server.tool()
async def await_checks(ctx: Context[Any], ids: list[str | int],
                       max_wait_s: float = 60) -> dict[str, Any]:
    """Wait up to max_wait_s for checks: claim ids, branch ids, draft versions, or the
    forms "claim:<id>", "branch:<id>", "draft:<n>", "frame:own", "frame:critique",
    "ideas:own" (the idea list, framer/ideas.md).
    Returns what landed and what has not; never invents a verdict."""
    caller = caller_of(ctx)  # read before leaving the request's task

    def wait() -> dict[str, Any]:
        return _call_as(caller, "await_checks", lambda r, c: r.await_checks(ids, max_wait_s))

    return await anyio.to_thread.run_sync(wait)


@server.tool()
def record_frame(ctx: Context[Any], kind: str, text: str) -> dict[str, Any]:
    """Blind framer only: record your own frame (kind "own") or your critique (kind
    "critique") as markdown text. The register writes framer/own-frame.md or
    framer/critique.md; those files are written by this tool only."""
    return _call(ctx, "record_frame", lambda r, c: r.record_frame(c, kind, text))


@server.tool()
def record_review(ctx: Context[Any], version: int, text: str,
                  points: list[dict[str, Any]]) -> dict[str, Any]:
    """Final reviewers only: record your review of draft <version> as markdown text, with
    its points as a list of {point: text, material: true or false, node: tree node id or
    null, criterion: criterion id (C1, ...) from criteria.md or null}, at least one of
    node and criterion set (you judge which are material: would it change the answer, or
    whether a criterion is met). Each point gets an id (v<version>.<you>.<n>). The register
    writes reviews/v<version>/<your file>; reviews are written by this tool only. The
    draft under review is register/drafts/answer.v<version>.md."""
    return _call(ctx, "record_review", lambda r, c: r.record_review(c, version, text, points))


@server.tool()
def gate_status(ctx: Context[Any], version: int) -> dict[str, Any]:
    """Lead only: read the release gate for draft <version> without releasing. Returns
    every unmet condition, numbered one per line, the same list finish_run refuses with;
    the open material points of that version's reviews by criterion and by node; and the
    nodes set aside, with their reasons. Call it after every review round."""
    return _call(ctx, "gate_status", lambda r, c: r.gate_status(c, version))


@server.tool()
def finish_run(ctx: Context[Any], version: int) -> dict[str, Any]:
    """Lead only: release the submitted, reviewed draft <version> as the deliverable.
    The register copies the frozen draft to deliverable/answer.md itself; it releases
    only when gate_status shows nothing unmet, and when it refuses it lists every unmet
    condition, numbered. After the last review round run.json allows, only the latest
    version may be released, and deliverable/completion.md lists that round's points."""
    return _call(ctx, "finish_run", lambda r, c: r.finish_run(c, version))


def _register_evidence_tools() -> None:
    try:
        from stemma.evidence.tools import register_tools
    except ImportError:
        return
    register_tools(server)


def _register_datalayer_tools() -> None:
    """Part D's `query_data` (CONTRACTS section 5a), served by this one server."""
    from stemma.datalayer.tools import register_tools

    register_tools(server)


_register_evidence_tools()
_register_datalayer_tools()

# Host secrets this process may load (invariant: the data layer's secret settings).
SECRET_NAMES = ("ZETARIS_API_KEY", "STEMMA_PG_PASSWORD")


def load_host_secrets(env: dict[str, str] | None = None) -> list[str]:
    """Read the run's host-secrets file (`STEMMA_HOST_SECRETS`) into this process's own
    environment: the MCP server is the only process that holds them. The file must be a
    regular file of this user, mode 0600, reached without a link. Returns the names set."""
    e = os.environ if env is None else env
    path = e.get("STEMMA_HOST_SECRETS")
    if not path:
        return []
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or stat.S_IMODE(st.st_mode) & 0o077):
            raise RuntimeError(f"{path} is not a private file of this user")
        data = json.loads(os.read(fd, 64 * 1024))  # resource limit: bytes of the file
    finally:
        os.close(fd)
    names = []
    for k in SECRET_NAMES:
        v = data.get(k) if isinstance(data, dict) else None
        if isinstance(v, str) and v:
            e[k] = v
            names.append(k)
    return names


def main() -> None:
    load_host_secrets()
    server.run()


if __name__ == "__main__":
    main()
