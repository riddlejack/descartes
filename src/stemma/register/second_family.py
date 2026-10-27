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
from dataclasses import dataclass
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
    """This claim got no usable answer (unreadable, or answered by a model other than
    the one pinned): recorded loudly on the claim, never a silent substitute."""


@dataclass(frozen=True)
class Answer:
    verdict: str  # one of ANSWERS
    reason: str
    model_asked: str
    served_model: str | None
    receipt: dict[str, Any]


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


def parse(text: str | None) -> tuple[str, str]:
    """The model's verdict and reason, read from its final answer only: reasoning the
    model writes first (in a think block or as plain text, which may itself quote the
    answer format) is never read as the verdict. The last JSON object in the text whose
    `verdict` is one of the three answers is the answer; anything else is a typed error."""
    if not text:
        raise ValueError("the answer is empty")
    body = re.sub(r"<think>.*?</think>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    decoder = json.JSONDecoder()
    found: tuple[str, str] | None = None
    for m in re.finditer(r"\{", body):
        try:
            obj, _end = decoder.raw_decode(body, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("verdict") in ANSWERS:
            reason = obj.get("reason")
            found = (str(obj["verdict"]),
                     (reason if isinstance(reason, str) else "")[:REASON_MAX_CHARS])
    if found is None:
        raise ValueError("the answer holds no JSON object with a verdict of "
                         f"{', '.join(ANSWERS)}")
    return found


def ask(asker: Asker, run_dir: Path, claim: dict[str, Any], evidence: Evidence,
        models: Sequence[str] = (PRIMARY_MODEL, FALLBACK_MODEL)) -> Answer:
    """Ask the first model that answers usably; the fallback is tried when the primary
    cannot be asked or answers unreadably. Raises SecondFamilyUnavailable when none can."""
    msgs = messages(claim, passages(run_dir, claim, evidence))
    problems: list[str] = []
    answered = False  # some model answered: a failure is this claim's, not the run's
    for model in models:
        try:
            text, served, receipt = asker(model, msgs)
        except SecondFamilyUnavailable as exc:
            problems.append(f"{model}: {exc}")
            if str(exc).startswith("provider_auth_failed"):
                break  # the same key fails for every model
            continue
        answered = True
        if not model.startswith("meterless/") and served != model:
            # A pinned model answered by another one: never a silent substitute.
            problems.append(f"{model}: answered by {served or 'an unnamed model'}, not the "
                            f"model pinned")
            continue
        try:
            verdict, reason = parse(text)
        except (ValueError, json.JSONDecodeError) as exc:
            problems.append(f"{model}: unreadable answer ({exc})")
            continue
        return Answer(verdict, reason, model, served, receipt)
    if answered:
        raise SecondFamilyFailed("; ".join(problems))
    raise SecondFamilyUnavailable("; ".join(problems))


def disagrees(claude_verdict: str | None, second: str) -> bool:
    """Whether the two opinions disagree (labels only): an approval against
    does_not_support, or a rejection against supports."""
    return ((claude_verdict in APPROVING and second == "does_not_support")
            or (claude_verdict in REJECTING and second == "supports"))
