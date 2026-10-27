"""`run_analysis`: an author runs a script under `analysis/` the way filing re-runs it.

Same bundle rule (`derivation.build_bundle`), same sandbox (`derivation.rerun`: no
network, no writes outside a temporary folder, a recorded time limit). Returns the
exact value the script passed to `result()` and its standard output, capped. A path
outside `analysis/` (absolute, `..`, a link, a case or Unicode-form alias of a real
name) is refused. Inputs and documents are resolved as `file_claim` resolves them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from stemma.register import roles
from stemma.register.derivation import (
    DEFAULT_TIME_LIMIT_S,
    DOCUMENTS_MAX,
    DerivationError,
    build_bundle,
    rerun,
)
from stemma.register.evidence_port import Evidence, QuoteCheckFailed
from stemma.register.filing import InputLookup, Refused, Rerun
from stemma.register.store import Caller, Register


def run_analysis(run_dir: Path, script: str, inputs: list[str] | None,
                 documents: list[str] | None, lookup: InputLookup,
                 evidence: Evidence | None, rerun_fn: Rerun = rerun,
                 time_limit_s: int = DEFAULT_TIME_LIMIT_S) -> dict[str, Any]:
    """Run run_dir/script in the re-run sandbox. `inputs` are claim ids (resolved through
    `lookup`, as file_claim does); `documents` are receipt ids the script reads with
    stemma.analysis.document(). Refuses (Refused) with what failed."""
    if not isinstance(script, str):
        raise Refused("Refused: script is not a path.")
    named_inputs = inputs or []
    named_docs = documents or []
    if not all(isinstance(i, str) and i.strip() for i in named_inputs):
        raise Refused("Refused: inputs must be a list of claim ids.")
    if not all(isinstance(d, str) and d.strip() for d in named_docs):
        raise Refused("Refused: documents must be a list of receipt ids.")
    if len(named_docs) > DOCUMENTS_MAX:
        raise Refused(f"Refused: {len(named_docs)} documents named, more than "
                      f"{DOCUMENTS_MAX} (a resource limit).")
    try:
        bundle = build_bundle(run_dir, script)
    except DerivationError as exc:
        raise Refused(f"Refused: run_analysis: {exc}.") from exc
    values: dict[str, float] = {}
    for given in named_inputs:
        full, number = lookup(given)
        values[full] = number
    texts: dict[str, str] = {}
    for rid in named_docs:
        if evidence is None:
            raise Refused("Refused: the documents cannot be read.")
        try:
            texts[rid] = evidence.read_text(run_dir, rid)
        except QuoteCheckFailed as exc:
            raise Refused(f"Refused: document {rid}: {exc}.") from exc
    try:
        rr = rerun_fn(bundle, values, texts, time_limit_s=time_limit_s)
    except DerivationError as exc:
        raise Refused(f"Refused: run_analysis: {exc}.") from exc
    return {"script": script, "result": rr.reported, "exit_code": rr.exit_code,
            "stdout": rr.stdout, "stdout_truncated": rr.stdout_truncated,
            "stderr_tail": rr.stderr_tail, "time_limit_s": rr.time_limit_s,
            "inputs": sorted(values), "bundle_sha256": bundle.sha256,
            "bundle_files": sorted(bundle.files)}


def run_for(register: Register, caller: Caller, script: str, inputs: list[str] | None,
            documents: list[str] | None) -> dict[str, Any]:
    """The host tool's body: authors only, inputs through the register's own lookup."""
    if caller.agent_type not in roles.AUTHORS:
        raise Refused(f"Refused: {roles.short(caller.agent_type)} sessions do not run "
                      f"analysis scripts.")
    evidence = register.evidence if documents else None
    return run_analysis(register.run_dir, script, inputs, documents, register._inputs(),
                        evidence)
