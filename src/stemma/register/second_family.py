"""A second model family's look at each cited claim (N1, CONTRACTS v0.4 "Second family").

After a draft is submitted and the Claude claim checkers have recorded verdicts, the
host asks a Nemotron model, through Meterless Nightshift, whether each cited claim's
exact stored passage supports the claim as stated. Its answer is recorded beside the
Claude verdict, with the Nightshift receipt. It only adds a check: where it disagrees
with Claude, the host starts one fresh Claude checker with both opinions for a third
look, and that verdict decides. It never approves and never lowers a check, and when
Nightshift is unreachable or refuses, the run records `second_family_unavailable`
loudly and carries on.

Code judges nothing here: it builds the question from exact stored text, reads the
three-way answer the model gives, and compares labels.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from stemma.register.evidence_port import Evidence, QuoteCheckFailed

# The models asked (invariant: the orchestrator's choice for N1; recorded in run.json).
PRIMARY_MODEL: Final = "nvidia/nemotron-3-ultra-550b-a55b"
FALLBACK_MODEL: Final = "nvidia/nemotron-3.5-lightning-30b-a3b"
# Resource limit: characters of stored text kept on each side of a quoted span.
CONTEXT_CHARS: Final = 600
# Resource limit: tokens one answer may use, its reasoning included (a reasoning model
# given too few returns an empty answer).
MAX_TOKENS: Final = 4096
# Resource limit: characters of the model's one-line reason kept.
REASON_MAX_CHARS: Final = 500
# The answers the model may give (invariant: the N1 question's three labels).
ANSWERS: Final = ("supports", "does_not_support", "cannot_tell")
# Claude verdicts that approve a claim, and the one that rejects it (invariant: the
# register's verdict vocabulary). A disagreement is an approval against
# does_not_support, or a rejection against supports; cannot_tell and could_not_check
# disagree with nothing.
APPROVING: Final = frozenset({"holds", "holds_with_correction"})
REJECTING: Final = frozenset({"not_supported"})

SYSTEM: Final = (
    "You check one factual claim against the exact source passage it cites. Decide only "
    "from the passage. Check the entity, the period, the unit, the scope and the "
    "direction of any change. End your reply with one JSON object and nothing after it: "
    '{"verdict": "supports" | "does_not_support" | "cannot_tell", "reason": "<one line>"}.'
)


class SecondFamilyUnavailable(Exception):
    """Nightshift could not be asked at all (not configured, offline, the key refused, or
    no model routable): recorded once for the run, which carries on."""


class SecondFamilyFailed(Exception):
    """This claim got no usable answer (unreadable, answered by a model other than the
    one pinned, or its passage could not be placed): recorded loudly on the claim, never
    a silent substitute. Carries every attempt made, for the spend."""

    def __init__(self, message: str, attempts: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.attempts = attempts or []


@dataclass(frozen=True)
class Answer:
    verdict: str  # one of ANSWERS
    reason: str
    model_asked: str
    served_model: str | None
    receipt: dict[str, Any]
    attempts: list[dict[str, Any]] = field(default_factory=list)  # every attempt, for spend


# The receipt fields kept in the run's record (review #4): an allowlist, numbers and
# known strings only; headers, ids and anything a gateway could reflect are dropped.
RECEIPT_FIELDS: Final = ("provider", "contract", "purpose", "route", "pinned",
                         "request_sha256", "request_bytes", "messages", "ms", "http_status",
                         "served_model", "finish_reason", "response_sha256", "ok")
USAGE_FIELDS: Final = ("prompt_tokens", "completion_tokens", "total_tokens")


def clean_receipt(receipt: dict[str, Any] | None) -> dict[str, Any] | None:
    """A receipt as the run may keep it: allowlisted fields, every string scrubbed of
    any registered secret, usage as whole numbers only."""
    if receipt is None:
        return None
    from stemma.datalayer.querylog import scrub

    out: dict[str, Any] = {}
    for k in RECEIPT_FIELDS:
        v = receipt.get(k)
        if isinstance(v, bool | int | float) or v is None:
            out[k] = v
        elif isinstance(v, str):
            out[k] = scrub(v)[:200]
    raw_usage = receipt.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    out["usage"] = {k: int(usage[k]) for k in USAGE_FIELDS
                    if isinstance(usage.get(k), int) and not isinstance(usage.get(k), bool)}
    return out


def clean_text(text: str) -> str:
    """A failure message as the run may keep it: scrubbed of registered secrets."""
    from stemma.datalayer.querylog import scrub

    return str(scrub(text))[:2000]


class Asker(Protocol):
    """One question to one model: returns the answer's text, the model the answer names,
    and the call's receipt; raises SecondFamilyUnavailable when it cannot be asked."""

    def __call__(self, model: str, messages: list[dict[str, str]]
                 ) -> tuple[str | None, str | None, dict[str, Any]]: ...


def nightshift_asker(client: Any) -> Asker:
    """An Asker through part D's NightshiftClient: a `meterless/*` route, or a provider
    model id pinned by name (sent only when the runtime lists it)."""
    from stemma.providers.nightshift import (
        NightshiftError,
        NightshiftOffline,
        NightshiftUnauthorized,
    )

    def ask(model: str, messages: list[dict[str, str]]
            ) -> tuple[str | None, str | None, dict[str, Any]]:
        try:
            res = client.chat(messages, route=model, purpose="second_family",
                              temperature=0, max_tokens=MAX_TOKENS)
        except NightshiftUnauthorized as exc:
            raise SecondFamilyUnavailable(f"provider_auth_failed: {exc}") from exc
        except NightshiftOffline as exc:
            raise SecondFamilyUnavailable(f"nightshift_offline: {exc}") from exc
        except NightshiftError as exc:
            if "is not listed by GET /v1/models" in str(exc):  # not routable: the run's
                raise SecondFamilyUnavailable(f"model_not_listed: {exc}") from exc
            raise SecondFamilyUnavailable(f"nightshift_error: {exc}") from exc
        return res.text, res.served_model, dict(res.receipt)

    return ask


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def passages(run_dir: Path, claim: dict[str, Any], evidence: Evidence) -> list[dict[str, str]]:
    """Each support item's quotation with its stored context: the exact stored text
    around the first place the quotation appears (whitespace compared loosely, as the
    filing check does). A quotation that cannot be placed is given alone, marked so."""
    out: list[dict[str, str]] = []
    for s in claim.get("support") or []:
        rid, quote = str(s.get("receipt_id", "")), str(s.get("quote", ""))
        try:
            text = evidence.read_text(run_dir, rid)
        except QuoteCheckFailed:
            out.append({"receipt_id": rid, "quote": quote, "passage": quote,
                        "placed": "no: the stored text could not be read"})
            continue
        pattern = r"\s+".join(re.escape(w) for w in quote.split())
        m = re.search(pattern, text) if pattern else None
        if m is None:
            out.append({"receipt_id": rid, "quote": quote, "passage": quote,
                        "placed": "no: the quotation was not found in the stored text"})
            continue
        a, b = max(0, m.start() - CONTEXT_CHARS), min(len(text), m.end() + CONTEXT_CHARS)
        out.append({"receipt_id": rid, "quote": quote, "passage": text[a:b], "placed": "yes"})
    return out


def messages(claim: dict[str, Any], found: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    value = claim.get("value") or {}
    lines = [f"Claim: {claim['statement']}"]
    if value:
        lines.append("Figure as filed: " + json.dumps(
            {k: value.get(k) for k in ("number", "unit", "period", "kind", "as_written",
                                       "denominator") if value.get(k) is not None},
            ensure_ascii=False))
    for i, p in enumerate(found, start=1):
        head = (f"Source passage {i} (receipt {p['receipt_id']}; the quoted words: "
                f"\"{p['quote']}\"):")
        lines += ["", head, _norm(p["passage"])]
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "\n".join(lines)}]


_FENCE_END = re.compile(r"\s*```\s*$")
_FENCE_START = re.compile(r"```(?:json)?\s*$", re.IGNORECASE)


def parse(text: str | None) -> tuple[str, str]:
    """The model's verdict and reason, from its final answer only (Codex review of N1,
    #1): every complete think block is removed; an unfinished one is refused; then the
    reply must END with exactly one JSON object, optionally in a code fence, nothing
    after it, holding only `verdict` (one of the three answers) and `reason` (text).
    An object found anywhere else (in reasoning, in an example, nested inside another
    value) is never the verdict. Anything else is a typed error."""
    if not text:
        raise ValueError("the answer is empty")
    # A reasoning model's reply is its reasoning, closed by `</think>` (the opening tag
    # may be the chat template's, not in the reply), then its answer: only what follows
    # the last `</think>` is the answer, and it must be the object alone. A `<think>`
    # with no close after it is unfinished reasoning (seen live on nemotron-3-ultra).
    closes = [m.end() for m in re.finditer(r"</think>", text, re.IGNORECASE)]
    after = text[closes[-1]:] if closes else text
    if re.search(r"<think>", after, re.IGNORECASE):
        raise ValueError("the answer holds unfinished reasoning")
    body = _FENCE_END.sub("", after.rstrip()).rstrip()
    if closes:
        body = _FENCE_START.sub("", body.lstrip()) if body.lstrip().startswith("```") \
            else body.lstrip()
        if not body.startswith("{"):
            raise ValueError("after the reasoning, the answer is not one JSON object")
    if not body.endswith("}"):
        raise ValueError("the answer does not end with a JSON object")
    decoder = json.JSONDecoder()
    found: dict[str, Any] | None = None
    start = -1
    for m in re.finditer(r"\{", body):  # the outermost object that ends the reply
        try:
            obj, end = decoder.raw_decode(body, m.start())
        except json.JSONDecodeError:
            continue
        if end == len(body):
            found, start = obj, m.start()
            break
    if not isinstance(found, dict) or (closes and start != 0):
        raise ValueError("the answer does not end with one complete JSON object")
    if not set(found) <= {"verdict", "reason"} or found.get("verdict") not in ANSWERS:
        raise ValueError("the final object is not {verdict: one of "
                         f"{', '.join(ANSWERS)}, reason: text}}")
    reason = found.get("reason", "")
    if not isinstance(reason, str):
        raise ValueError("the final object's reason is not text")  # noqa: TRY004
    _ = _FENCE_START.sub("", body[:start])  # what precedes is reasoning, never read
    return str(found["verdict"]), reason[:REASON_MAX_CHARS]


def _attempt(model: str, receipt: dict[str, Any] | None, problem: str | None) -> dict[str, Any]:
    return {"model": model, "receipt": receipt, "problem": problem}


def ask(asker: Asker, run_dir: Path, claim: dict[str, Any], evidence: Evidence,
        models: Sequence[str] = (PRIMARY_MODEL, FALLBACK_MODEL)) -> Answer:
    """Ask the first model that answers usably. The fallback is tried only when the
    primary could not be asked or answered unreadably; an answer named by a model other
    than the one pinned ends this claim's look at once (review #7). A passage that could
    not be placed in the stored text ends it before asking (review #6). Every attempt's
    receipt is kept (review #8). Raises SecondFamilyUnavailable when no model could be
    asked at all, SecondFamilyFailed otherwise."""
    found = passages(run_dir, claim, evidence)
    unplaced = [f"{p['receipt_id']}: {p['placed']}" for p in found if p["placed"] != "yes"]
    if unplaced:
        raise SecondFamilyFailed("the stored passage could not be placed (" +
                                 "; ".join(unplaced) + "); nothing was asked", [])
    msgs = messages(claim, found)
    problems: list[str] = []
    attempts: list[dict[str, Any]] = []
    answered = False  # some model answered: a failure is this claim's, not the run's
    for model in models:
        try:
            text, served, receipt = asker(model, msgs)
        except SecondFamilyUnavailable as exc:
            problems.append(f"{model}: {exc}")
            attempts.append(_attempt(model, None, str(exc)))
            if str(exc).startswith("provider_auth_failed"):
                break  # the same key fails for every model
            if str(exc).startswith("nightshift_error"):
                answered = True  # the gateway answered, badly: this claim's failure
            continue
        except Exception as exc:  # noqa: BLE001 - a malformed answer is this claim's failure
            answered = True
            problems.append(f"{model}: malformed response ({type(exc).__name__})")
            attempts.append(_attempt(model, None, f"malformed response ({type(exc).__name__})"))
            continue
        answered = True
        if not model.startswith("meterless/") and served != model:
            problem = (f"{model}: answered by {served or 'an unnamed model'}, not the "
                       f"model pinned")
            attempts.append(_attempt(model, receipt, problem))
            raise SecondFamilyFailed("; ".join([*problems, problem]), attempts)
        finish = receipt.get("finish_reason")
        try:
            if finish not in (None, "stop"):
                raise ValueError(f"the answer did not complete (finish_reason {finish})")
            verdict, reason = parse(text)
        except (ValueError, json.JSONDecodeError) as exc:
            problems.append(f"{model}: unreadable answer ({exc})")
            attempts.append(_attempt(model, receipt, f"unreadable answer ({exc})"))
            continue
        attempts.append(_attempt(model, receipt, None))
        return Answer(verdict, reason, model, served, receipt, attempts)
    if answered:
        raise SecondFamilyFailed("; ".join(problems), attempts)
    raise SecondFamilyUnavailable("; ".join(problems))


def disagrees(claude_verdict: str | None, second: str) -> bool:
    """Whether the two opinions disagree (labels only): an approval against
    does_not_support, or a rejection against supports."""
    return ((claude_verdict in APPROVING and second == "does_not_support")
            or (claude_verdict in REJECTING and second == "supports"))
