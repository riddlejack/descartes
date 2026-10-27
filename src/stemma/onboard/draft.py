"""The onboarding draft: one model call, through the driver's clean launch.

The model drafts, for one question (and the user's files):
- the question's type (a fact, a cause, a decision, or other), which picks the frame (D2);
- the broader decision the question may inform, so the scope question (D1) can name it;
- each ambiguous term, its readings, whether both are cheap to research, and whether the
  readings would change the whole research (D3);
- three to five plain criteria the answer must meet, drawn only from the question's words
  (D4: no threshold the user did not give);
- the options, only when the question itself names them (D2).

Code checks only the draft's shape; whether the criteria are faithful to the question is
the user's call at the prompt, and the reviewers' later. The call runs on the pinned
harness with the clean configuration, no tools, no plugin, no MCP server, and its init
message must pass the canary; the user's own Claude configuration is never used.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

# Logical invariants: the question types D2 names, and D4's three to five criteria.
QUESTION_TYPES: Final = ("fact", "cause", "decision", "other")
MIN_CRITERIA: Final = 3
MAX_CRITERIA: Final = 5
# Resource limits: characters of each attached file shown to the model, of the question,
# and seconds the call may take.
FILE_CHARS: Final = 20_000
QUESTION_CHARS: Final = 2_000
CALL_TIMEOUT_S: Final = 600
DEFAULT_MODEL: Final = "claude-opus-5-5"
DEFAULT_EFFORT: Final = "high"


class OnboardError(Exception):
    """The draft could not be made or is not in the agreed shape; the message says which."""


@dataclass(frozen=True)
class Term:
    term: str
    readings: tuple[str, ...]
    both_cheap: bool
    changes_whole_research: bool
    why_it_matters: str
    pick: str | None  # the reading the model would take when it need not ask


@dataclass(frozen=True)
class Draft:
    question_type: str
    question_type_reason: str
    broader_decision: str | None
    terms: tuple[Term, ...]
    criteria: tuple[str, ...]
    options_named: tuple[str, ...] = field(default_factory=tuple)

    def to_json(self) -> dict[str, Any]:
        return {"question_type": self.question_type,
                "question_type_reason": self.question_type_reason,
                "broader_decision": self.broader_decision,
                "ambiguous_terms": [{"term": t.term, "readings": list(t.readings),
                                     "both_cheap": t.both_cheap,
                                     "changes_whole_research": t.changes_whole_research,
                                     "why_it_matters": t.why_it_matters, "pick": t.pick}
                                    for t in self.terms],
                "criteria": list(self.criteria), "options_named": list(self.options_named)}


PROMPT = """You help a research team start work on a question. Draft what the team must \
confirm with the person who asked it. Do not research or answer the question.

The question, exactly as asked:
<question>
{question}
</question>
{files}
Return only one JSON object, with no other text, in this shape:
{{
  "question_type": "fact" | "cause" | "decision" | "other",
  "question_type_reason": "one sentence: why this type",
  "broader_decision": "the broader decision this question may inform, in one short \
sentence, or null if none is plausible",
  "ambiguous_terms": [
    {{"term": "a word or phrase in the question",
      "readings": ["first reasonable reading", "second reasonable reading"],
      "both_cheap": true or false,
      "changes_whole_research": true or false,
      "why_it_matters": "one clause: what changes between the readings",
      "pick": "the reading to use if the person is not asked, or null"}}
  ],
  "criteria": ["three to five plain criteria a finished answer must meet"],
  "options_named": ["each option the question itself names, if it names any"]
}}

Rules:
- "fact": the question asks whether something is so, or how much. "cause": it asks why. \
"decision": it asks what to do, or which of options to choose. Otherwise "other".
- Answer only the question asked. The broader decision is context to ask about, never \
something to add to the research.
- A term is ambiguous only if it has two reasonable readings that would give different \
answers. "changes_whole_research" is true only if the readings would change most of \
the research, not one figure.
- Draw every criterion only from the question's own words and the files. Add no \
threshold, date, source or standard the question does not give. Each criterion is one \
plain sentence a reviewer can judge met or not met.
- "options_named" lists options only when the question names them; otherwise it is [].
"""


def _files_block(files: list[Path]) -> str:
    if not files:
        return ""
    parts = ["\nThe person also gave these files (their text, cut at a length limit):"]
    for f in files:
        try:
            text = f.read_bytes()[: FILE_CHARS * 4].decode("utf-8")[:FILE_CHARS]
        except UnicodeDecodeError:
            parts.append(f'<file name="{f.name}">(not text; the research team will read it)'
                         f"</file>")
            continue
        parts.append(f'<file name="{f.name}">\n{text}\n</file>')
    return "\n".join(parts) + "\n"


def prompt_for(question: str, files: list[Path]) -> str:
    q = question.strip()
    if not q:
        raise OnboardError("the question is empty")
    if len(q) > QUESTION_CHARS:
        raise OnboardError(f"the question is longer than {QUESTION_CHARS} characters "
                           f"(a resource limit)")
    return PROMPT.format(question=q, files=_files_block(files))


# ---------------------------------------------------------------------- parsing
def _text(v: Any, what: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise OnboardError(f"the draft's {what} is not text")
    return v.strip()


def _json_object(raw: str) -> dict[str, Any]:
    """The one JSON object in the model's reply (a fenced block is tolerated)."""
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        s = s.rsplit("```", 1)[0]
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end < start:
        raise OnboardError("the model's reply holds no JSON object")
    try:
        data = json.loads(s[start:end + 1])
    except json.JSONDecodeError as exc:
        raise OnboardError(f"the model's reply is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise OnboardError("the model's reply is not a JSON object")
    return data


def parse_draft(raw: str | dict[str, Any]) -> Draft:
    data = raw if isinstance(raw, dict) else _json_object(raw)
    qtype = data.get("question_type")
    if qtype not in QUESTION_TYPES:
        raise OnboardError(f"the draft's question_type must be one of {list(QUESTION_TYPES)}")
    broader = data.get("broader_decision")
    if broader is not None:
        broader = _text(broader, "broader_decision")
    terms: list[Term] = []
    raw_terms = data.get("ambiguous_terms") or []
    if not isinstance(raw_terms, list):
        raise OnboardError("the draft's ambiguous_terms is not a list")
    for i, t in enumerate(raw_terms, start=1):
        if not isinstance(t, dict):
            raise OnboardError(f"ambiguous term {i} is not an object")
        readings = t.get("readings")
        if (not isinstance(readings, list) or len(readings) < 2
                or not all(isinstance(r, str) and r.strip() for r in readings)):
            raise OnboardError(f"ambiguous term {i} needs at least two readings")
        for flag in ("both_cheap", "changes_whole_research"):
            if not isinstance(t.get(flag), bool):
                raise OnboardError(f"ambiguous term {i}: {flag} is not true or false")
        pick = t.get("pick")
        if pick is not None and pick not in readings:
            raise OnboardError(f"ambiguous term {i}: pick is not one of its readings")
        terms.append(Term(_text(t.get("term"), f"term {i}"),
                          tuple(r.strip() for r in readings), t["both_cheap"],
                          t["changes_whole_research"],
                          _text(t.get("why_it_matters"), f"term {i} why_it_matters"), pick))
    crit = data.get("criteria")
    if not isinstance(crit, list) or not MIN_CRITERIA <= len(crit) <= MAX_CRITERIA:
        raise OnboardError(f"the draft must give {MIN_CRITERIA} to {MAX_CRITERIA} criteria")
    options = data.get("options_named") or []
    if not isinstance(options, list):
        raise OnboardError("the draft's options_named is not a list")
    return Draft(qtype, _text(data.get("question_type_reason"), "question_type_reason"),
                 broader, tuple(terms),
                 tuple(_text(c, f"criterion {i}") for i, c in enumerate(crit, start=1)),
                 tuple(_text(o, f"option {i}") for i, o in enumerate(options, start=1)))


# ------------------------------------------------------------------ the call
ModelCall = Callable[[str], str]  # prompt -> the model's final text


def clean_model_call(prompt: str, model: str = DEFAULT_MODEL,
                     effort: str = DEFAULT_EFFORT) -> str:
    """One headless turn on the pinned harness with the clean configuration
    (`STEMMA_CLAUDE_BIN`, `STEMMA_CLAUDE_CONFIG`), no tools, no plugin and no MCP server;
    the session's init message must pass the canary before its answer is used."""
    from stemma.driver.clean import bare_expectation, check_canary, clean_launch
    from stemma.driver.config import DriverConfig, default_plugin_src
    from stemma.driver.errors import DriverError
    from stemma.driver.launch import harness_version

    env = {k: os.environ.get(k, "") for k in ("STEMMA_CLAUDE_BIN", "STEMMA_CLAUDE_CONFIG")}
    missing = [k for k, v in env.items() if not v]
    if missing:
        raise OnboardError(f"{' and '.join(missing)} must name the pinned harness and its "
                           f"clean configuration (see .env.example)")
    binary = Path(env["STEMMA_CLAUDE_BIN"]).expanduser()
    config = Path(env["STEMMA_CLAUDE_CONFIG"]).expanduser()
    with tempfile.TemporaryDirectory(prefix="stemma-new-") as td:
        cwd = Path(td).resolve()
        cfg = DriverConfig(cwd, binary, config, Path(sys.executable), default_plugin_src())
        try:
            version = harness_version(cfg)
            launch = clean_launch(binary, config, prompt, cwd=cwd, model=model, effort=effort,
                                  tools=[], settings_path=cwd / ".settings.json",
                                  env_extra={"CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1"},
                                  strict_mcp_config=True, disable_slash_commands=True)
        except DriverError as exc:
            raise OnboardError(f"the clean launch could not be built: {exc}") from exc
        try:
            done = subprocess.run(launch.argv, env=launch.env, cwd=cwd,
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  timeout=CALL_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired as exc:
            raise OnboardError(f"the model call ran past {CALL_TIMEOUT_S} s") from exc
        init: dict[str, Any] | None = None
        result: dict[str, Any] | None = None
        for line in done.stdout.splitlines():
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and msg.get("type") == "system" and \
                    msg.get("subtype") == "init" and init is None:
                init = msg
            elif isinstance(msg, dict) and msg.get("type") == "result":
                result = msg
        if init is None:
            raise OnboardError(f"the session printed no init message (exit "
                               f"{done.returncode}): {done.stderr.strip()[-400:]}")
        problems = check_canary(init, bare_expectation(
            cwd=cwd, tools=[], harness_version=version,
            session_id=init.get("session_id") if isinstance(init.get("session_id"), str)
            else None))
        if problems:
            raise OnboardError("the session did not start clean, so its draft is not used: "
                               + "; ".join(problems))
        if result is None or result.get("is_error") or not isinstance(result.get("result"),
                                                                        str):
            raise OnboardError(f"the model call ended without an answer (exit "
                               f"{done.returncode}, {result.get('subtype') if result else 'none'})")
        return str(result["result"])


def draft(question: str, files: list[Path], call: ModelCall | None = None) -> Draft:
    """The onboarding draft for `question`, from one model call."""
    return parse_draft((call or clean_model_call)(prompt_for(question, files)))
