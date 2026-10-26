"""The code checks at filing (CONTRACTS section 5; design step 8). Exact facts only.

- `observed`, `interested_party`, `secondary`: at least one support item; each
  quotation found in its receipt's stored text; `value.as_written` inside a quotation.
  Whether `number` and `unit` correctly read `as_written` is the checker's call: code
  may attach a parse warning, never a refusal.
- `estimated`: a derivation whose re-run reproduces `number` exactly.
- `assumed`: no support required.
- `ratio`, `share`, `rate`: a filled `denominator`.
A failure refuses the claim and names the failed check, nothing more.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from stemma.evidence.quotes import find_quote
from stemma.register.derivation import DOCUMENTS_MAX, DerivationError, RerunResult, read_code
from stemma.register.evidence_port import Evidence, QuoteCheckFailed

TIERS = ("observed", "interested_party", "secondary", "estimated", "assumed")
SOURCED_TIERS = frozenset({"observed", "interested_party", "secondary"})
KINDS = ("level", "count", "ratio", "share", "rate", "change")
NEEDS_DENOMINATOR = frozenset({"ratio", "share", "rate"})
AGENT_FIELDS = ("statement", "value", "tier", "support", "derivation", "branch_id", "entities")
VALUE_FIELDS = frozenset({"number", "unit", "period", "kind", "as_written", "denominator"})


class Refused(Exception):
    """A tool call refused; the message names what failed and nothing else."""


@dataclass
class FilingResult:
    claim: dict[str, Any]
    checks: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def claim_id_of(claim: dict[str, Any]) -> str:
    """Content-derived id over the fields the agent supplies."""
    body = {k: claim.get(k) for k in AGENT_FIELDS}
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


def _str(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def normalise_shape(raw: dict[str, Any]) -> dict[str, Any]:
    """Check the claim's shape and return only the agent-supplied fields."""
    unknown = sorted(set(raw) - set(AGENT_FIELDS))
    if unknown:
        raise Refused(f"Refused: unknown claim field {unknown[0]}.")
    if not _str(raw.get("statement")):
        raise Refused("Refused: the claim has no statement.")
    tier = raw.get("tier")
    if tier not in TIERS:
        raise Refused(f"Refused: tier must be one of {', '.join(TIERS)}.")
    value = raw.get("value")
    if value is not None:
        if not isinstance(value, dict):
            raise Refused("Refused: value is not an object.")
        bad = sorted(set(value) - VALUE_FIELDS)
        if bad:
            raise Refused(f"Refused: unknown value field {bad[0]}.")
        if not _number(value.get("number")):
            raise Refused("Refused: value.number is not a finite number.")
        if value.get("kind") not in KINDS:
            raise Refused(f"Refused: value.kind must be one of {', '.join(KINDS)}.")
        for k in ("unit", "period", "as_written", "denominator"):
            if k in value and value[k] is not None and not isinstance(value[k], str):
                raise Refused(f"Refused: value.{k} is not text.")
    support = raw.get("support") or []
    if not isinstance(support, list) or not all(
        isinstance(s, dict) and _str(s.get("receipt_id")) and _str(s.get("quote"))
        and set(s) <= {"receipt_id", "quote"} for s in support
    ):
        raise Refused("Refused: each support item needs a receipt_id and a quote, nothing else.")
    deriv = raw.get("derivation")
    if deriv is not None and not (
        isinstance(deriv, dict) and _str(deriv.get("script"))
        and isinstance(deriv.get("inputs", []), list)
        and all(_str(i) for i in deriv.get("inputs", []))
        and isinstance(deriv.get("documents", []), list)
        and all(_str(i) for i in deriv.get("documents", []))
        and set(deriv) <= {"script", "inputs", "documents"}
    ):
        raise Refused("Refused: derivation needs a script, a list of input claim ids and, "
                      "optionally, a list of receipt ids it reads.")
    branch = raw.get("branch_id")
    if branch is not None and not _str(branch):
        raise Refused("Refused: branch_id is not text.")
    entities = raw.get("entities") or []
    if not isinstance(entities, list) or not all(_str(e) for e in entities):
        raise Refused("Refused: entities must be a list of names.")
    return {
        "statement": raw["statement"],
        "value": value,
        "tier": tier,
        "support": [{"receipt_id": s["receipt_id"], "quote": s["quote"]} for s in support],
        "derivation": deriv,
        "branch_id": branch,
        "entities": list(entities),
    }


_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "bn": 1e9,
          "b": 1e9, "billion": 1e9, "tn": 1e12, "trn": 1e12, "trillion": 1e12}
_PARSE = re.compile(r"([-+−]?)[^\d]*([\d,]*\.?\d+)\s*([a-zA-Z]+)?")


def parse_warning(value: dict[str, Any]) -> str | None:
    """A warning when `as_written` does not plainly read as `number`. Never a refusal."""
    written = value.get("as_written")
    if not isinstance(written, str):
        return None
    m = _PARSE.search(written)
    if not m:
        return f"parse warning: as_written {written!r} holds no number"
    try:
        base = float(m.group(2).replace(",", ""))
    except ValueError:
        return f"parse warning: as_written {written!r} holds no number"
    if m.group(1) in ("-", "−"):
        base = -base
    scale = _SCALE.get((m.group(3) or "").lower(), 1.0)
    candidates = {base, base * scale}
    if "%" in written:
        candidates |= {base / 100}
    number = float(value["number"])
    if not any(math.isclose(number, c, rel_tol=1e-9, abs_tol=1e-12) for c in candidates):
        return f"parse warning: number {number!r} does not plainly read as_written {written!r}"
    return None


def check_sourced(claim: dict[str, Any], run_dir: Path, evidence: Evidence,
                  result: FilingResult) -> None:
    support = claim["support"]
    if not support:
        raise Refused(f"Refused: a claim of tier {claim['tier']} needs at least one support item.")
    for n, item in enumerate(support, start=1):
        try:
            found = evidence.check_quote(run_dir, item["receipt_id"], item["quote"])
        except QuoteCheckFailed as exc:
            raise Refused(f"Refused: support {n}: {exc}.") from exc
        if not found:
            raise Refused(
                f"Refused: support {n}: the quotation is not in receipt {item['receipt_id']}."
            )
        result.checks.append({"check": f"quote_found:{n}", "passed": True})
    value = claim["value"]
    if value is not None:
        written = value.get("as_written")
        if not _str(written):
            raise Refused("Refused: value.as_written is empty.")
        # The same normalisation and number-token edges as the quotation check itself
        # (part A's find_quote): line breaks in a quote do not hide a figure, and "250"
        # is not inside "1,250".
        if not any(find_quote(item["quote"], written)["found"] for item in support):
            raise Refused("Refused: value.as_written does not appear inside any quotation.")
        result.checks.append({"check": "as_written_in_quote", "passed": True})
        warning = parse_warning(value)
        if warning:
            result.warnings.append(warning)


Rerun = Callable[..., RerunResult]  # (script, values[, documents]) -> RerunResult
InputLookup = Callable[[str], tuple[str, float]]


def check_estimated(claim: dict[str, Any], run_dir: Path, rerun: Rerun,
                    lookup: InputLookup, result: FilingResult,
                    evidence: Evidence | None = None) -> None:
    deriv = claim["derivation"]
    value = claim["value"]
    if deriv is None:
        raise Refused("Refused: an estimated claim needs a derivation.")
    if value is None:
        raise Refused("Refused: an estimated claim needs a value.")
    rel = PurePosixPath(deriv["script"])
    if (rel.is_absolute() or len(rel.parts) < 2 or rel.parts[0] != "analysis"
            or any(x in ("", ".", "..") for x in rel.parts)):
        raise Refused("Refused: the derivation script is not in analysis/.")
    docs_named = deriv.get("documents", [])
    if len(docs_named) > DOCUMENTS_MAX:
        raise Refused(f"Refused: the derivation names {len(docs_named)} documents, more than "
                      f"{DOCUMENTS_MAX} (a resource limit).")
    try:
        code = read_code(run_dir, str(rel))
    except DerivationError as exc:
        raise Refused(f"Refused: the derivation could not be re-run: {exc}.") from exc
    values: dict[str, float] = {}
    for given in deriv.get("inputs", []):
        full, number = lookup(given)
        values[full] = number
    # A derivation may read whole captured documents (a dataset, a table) by receipt id,
    # through stemma.analysis.document(); the re-run gets exactly those stored texts.
    documents: dict[str, str] = {}
    for rid in docs_named:
        if evidence is None:
            raise Refused("Refused: the derivation's documents cannot be read.")
        try:
            documents[rid] = evidence.read_text(run_dir, rid)
        except QuoteCheckFailed as exc:
            raise Refused(f"Refused: derivation document {rid}: {exc}.") from exc
    try:
        rr = rerun(code, values, documents) if documents else rerun(code, values)
    except DerivationError as exc:
        raise Refused(f"Refused: the derivation could not be re-run: {exc}.") from exc
    if rr.number is None:
        raise Refused(f"Refused: the derivation's re-run reported no result (exit {rr.exit_code}).")
    if rr.number != float(value["number"]):
        # Never echo the script's number: a script could encode file bytes in it.
        raise Refused("Refused: the derivation's re-run does not reproduce the claim's number.")
    result.checks.append({"check": "derivation_reproduces", "passed": True,
                          "time_limit_s": rr.time_limit_s, "inputs": sorted(values)})


def check_denominator(claim: dict[str, Any], result: FilingResult) -> None:
    value = claim["value"]
    if value is not None and value.get("kind") in NEEDS_DENOMINATOR:
        if not _str(value.get("denominator")):
            raise Refused(f"Refused: a {value['kind']} needs a filled denominator.")
        result.checks.append({"check": "denominator_filled", "passed": True})


def run_checks(raw: dict[str, Any], run_dir: Path, evidence: Evidence, rerun: Rerun,
               lookup: InputLookup) -> FilingResult:
    claim = normalise_shape(raw)
    result = FilingResult(claim=claim)
    check_denominator(claim, result)
    if claim["tier"] in SOURCED_TIERS:
        check_sourced(claim, run_dir, evidence, result)
    elif claim["tier"] == "estimated":
        check_estimated(claim, run_dir, rerun, lookup, result, evidence)
    return result
