"""Lint Stemma's role texts and skills, keep the MCP tool prefix in one place, and
generate docs/ROLE-MAP.md from the files.

Code here tests exact facts only: that every sentence carries its comment naming the
failure it prevents, that each text fits its page, that every tool name exists in the
contract, that the switch fragments are complete, and that each placebo skill matches
its real twin in name, frontmatter and length. Whether a sentence is justified is a
reviewer's judgment, never this file's.

Usage (from the repository root):
  python tools/roles/rolemap.py check            lint everything; exit 1 with each error
  python tools/roles/rolemap.py map              write docs/ROLE-MAP.md
  python tools/roles/rolemap.py map --check      exit 1 if docs/ROLE-MAP.md is stale
  python tools/roles/rolemap.py retool PREFIX    rewrite the MCP tool prefix everywhere
  python tools/roles/rolemap.py sync             copy each shared block into its roles
  python tools/roles/rolemap.py undefer          drop the ToolSearch line from every role
  python tools/roles/rolemap.py assemble KEY=VALUE ...   print one assembled lead text
"""

from __future__ import annotations

import itertools
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugin"
AGENTS_DIR = PLUGIN / "agents"
LEAD_DIR = PLUGIN / "lead"
SKILLS_DIR = PLUGIN / "skills"
STANDING_RULES = PLUGIN / "STANDING-RULES.md"
SKILL_EVIDENCE = ROOT / "tools" / "roles" / "skill-evidence.md"
ROLE_MAP = ROOT / "docs" / "ROLE-MAP.md"
SHARED_DIR = ROOT / "tools" / "roles" / "shared"
CONTRACTS = ROOT / "docs" / "CONTRACTS.md"


def set_root(root: Path) -> None:
    """Point every path at another copy of the repository (used by the tests)."""
    global ROOT, PLUGIN, AGENTS_DIR, LEAD_DIR, SKILLS_DIR, STANDING_RULES
    global SKILL_EVIDENCE, ROLE_MAP, SHARED_DIR, CONTRACTS
    ROOT = root
    PLUGIN = root / "plugin"
    AGENTS_DIR = PLUGIN / "agents"
    LEAD_DIR = PLUGIN / "lead"
    SKILLS_DIR = PLUGIN / "skills"
    STANDING_RULES = PLUGIN / "STANDING-RULES.md"
    SKILL_EVIDENCE = root / "tools" / "roles" / "skill-evidence.md"
    ROLE_MAP = root / "docs" / "ROLE-MAP.md"
    SHARED_DIR = root / "tools" / "roles" / "shared"
    CONTRACTS = root / "docs" / "CONTRACTS.md"


# Logical invariant: the namespace the `stemma` MCP server's tools appear under
# (contract section 2, item 4; part B confirms it by a run). The only place it is set:
# change it with `retool`, which rewrites every role file and this line.
TOOL_PREFIX = "mcp__plugin_stemma_stemma__"

# Logical invariant: the MCP tools the contract defines (sections 4 and 5).
CONTRACT_TOOLS = frozenset(
    {
        "capture",
        "capture_file",
        "sec_filings",
        "sec_capture",
        "sec_facts",
        "sec_search",
        "archived_links",
        "market_series",
        "outline",
        "read",
        "read_section",
        "search_store",
        "check_quote",
        "tables",
        "file_claim",
        "record_claim_verdict",
        "get_claim",
        "find_claims",
        "echo_candidates",
        "entity_review",
        "tree_upsert",
        "propose_branch_done",
        "record_branch_verdict",
        "request_frame_critique",
        "submit_draft",
        "await_checks",
        "proceed_on_assumption",
        "record_frame",
        "record_review",
        "get_tree",
        "claim_dependents",
        "query_data",
        "gate_status",
        "finish_run",
        "run_analysis",
        "check_assignment",
        "revise_criteria",
    }
)

# Logical invariant: the tools decision D6 takes from the lead (it reads branch
# records, never sources), as contract v0.4 section 0 lists them; Bash goes too, since a
# guard cannot parse every way a shell command reads a file. A hook (part B) also
# refuses the lead's Read of store/ and attachments.
LEAD_FORBIDDEN_TOOLS = frozenset(
    {
        "WebSearch",
        TOOL_PREFIX + "capture",
        TOOL_PREFIX + "capture_file",
        TOOL_PREFIX + "sec_filings",
        TOOL_PREFIX + "sec_capture",
        TOOL_PREFIX + "sec_facts",
        TOOL_PREFIX + "sec_search",
        TOOL_PREFIX + "archived_links",
        TOOL_PREFIX + "market_series",
        TOOL_PREFIX + "outline",
        TOOL_PREFIX + "read",
        TOOL_PREFIX + "read_section",
        TOOL_PREFIX + "search_store",
        TOOL_PREFIX + "tables",
        TOOL_PREFIX + "check_quote",
        TOOL_PREFIX + "query_data",
        "Bash",
        "WebFetch",
    }
)

# Logical invariant: the harness tools a role may list. WebFetch is denied (contract
# 4); Grep and Glob are not tools in the pinned 2.1.308 (contract 6).
HARNESS_TOOLS = frozenset(
    {"Read", "Write", "Edit", "Bash", "WebSearch", "Skill", "Agent", "ToolSearch"}
)

# Logical invariant: whether MCP tools load deferred. Without `"alwaysLoad": true` in
# plugin/.mcp.json, 2.1.308 defers them and every role with stemma tools needs this
# one line, identical everywhere; with it (contract 2 item 4, v0.2.3) they load at
# start, so no role carries the line or lists ToolSearch.
MCP_TOOLS_DEFERRED = False
DEFERRED_TOOLS_LINE = (
    "Your stemma tools load on demand: before you first use any, load the ones you need "
    "with ToolSearch, query select: followed by their full names joined by commas. "
    "<!-- prevents: a call to a stemma tool failing because its schema has not loaded; "
    "evidence: contract section 2 item 4, builder B's probe runs on 2.1.308 -->"
)

# Logical invariant: approved models (contract section 6, `availableModels`).
APPROVED_MODELS = frozenset({"claude-opus-5-5", "claude-fable-5-1"})

# Logical invariant: effort is high or above for every role (design step 0; contract 7).
APPROVED_EFFORTS = frozenset({"high", "xhigh", "max"})

# Logical invariant: the roles of contract section 8, in its order.
AGENT_ROLES = (
    "worker",
    "blind-framer",
    "claim-checker",
    "branch-reviewer",
    "deciders-eye-reviewer",
    "evidence-auditor",
    "mechanical-reader",
    "retrospective-writer",
    "idea-generator",
)

# Logical invariant: switches with text fragments, in assembly order (contract 7).
# s5_models is configuration and has no fragment.
SWITCHES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("s1_method", ("decomposition", "placebo", "none")),
    ("s2_shape", ("team", "single_loop")),
    ("s3_hypothesis_in_briefs", ("true", "false")),
    ("s4_orientation", ("frame_first", "basic_facts", "primary_record")),
    ("s6_cross_family", ("off", "during", "final")),
)

# Logical invariant: switch values parked by the orchestrator on 27 October; the
# driver refuses them with a typed error (part B), and the map marks them. single_loop
# needs the source tools D6 takes from the lead; s3 true needs two branches on one node.
PARKED: dict[tuple[str, str], str] = {
    ("s2_shape", "single_loop"): "the lead would need the source-reading tools decision D6 removes",
    ("s3_hypothesis_in_briefs", "true"): "the register does not yet allow two branches on one node",
}

# Resource limit: the design's "a page or less" for a standing role text (step 5),
# taken as this many words of instruction, excluding frontmatter and comments: one
# dense printed page. Raised from 800 on 26 October for lines from the live run and
# E6a; briefly 900, then brought back to 850 on 27 October by merging lead lines that
# prevent overlapping failures (docs/notes/roles-skills.md lists each merge). A
# stricter page (500 to 600 words) fits every role but the lead.
PAGE_WORDS = 850

# Resource limit: how far a placebo skill's body may differ in length from its real
# twin's, as a fraction of the real body's characters (contract 7: "share length").
PLACEBO_LENGTH_TOLERANCE = 0.05

# Logical invariant: words that would make a placebo teach research method. A lexical
# test only; whether a placebo is neutral is the reviewer's judgment.
PLACEBO_FORBIDDEN_WORDS = (
    "evidence",
    "claim",
    "decision",
    "decide",
    "hypothesis",
    "rival",
    "crux",
    "branch",
    "option",
    "source",
    "frame",
    "analysis",
    "analyse",
    "method",
    "verify",
    "check",
    "test",
    "market",
    "profit",
    "question",
)

COMMENT = re.compile(
    r"^(?P<text>.+?)\s*<!--\s*prevents:\s*(?P<prevents>.+);\s*evidence:\s*"
    r"(?P<evidence>.+?)\s*-->\s*$"
)
SHARED_MARK = re.compile(r"^<!-- (?:shared: (?P<name>[a-z-]+)|end shared) -->$")
SENTENCE_BREAK = re.compile(r"[.!?][\"”')\]]*\s+(?=[A-Z0-9`\"'(\[])")
# A snake_case name in prose: a tool, a verdict, a field. Checked against the contract.
IDENTIFIER = re.compile(r"(?<![\w<])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![\w>])")
# A run-folder path in prose: a token with a slash, or a file name with an extension.
PATH_TOKEN = re.compile(
    r"(?<![\w$.])(?:~|\.\.)?/?(?:[\w<>.\-]+/)+[\w<>.\-]*|(?<![\w$./])[\w<>\-]+(?:\.[\w<>]+)*"
    r"\.(?:md|json|jsonl|py|txt)\b"
)
MCP_NAME = re.compile(r"mcp__[A-Za-z0-9_]+")
# A frontmatter tool spec: a bare name, or a name with one parenthesised argument.
TOOL_SPEC = re.compile(r"^(?P<base>[A-Za-z][A-Za-z0-9_]*)(?:\((?P<arg>[^()\s][^()]*)\))?$")
# Logical invariant: the one harness tool a role lists with an argument, naming which
# plugin role it may start (Agent(stemma:worker)); any other argument is refused, so a
# parameterised spec cannot carry a tool past a check on its bare name.
PARAMETERISED_TOOLS = frozenset({"Agent"})
# Everything from the first parenthesis or space on, for naming a malformed spec's tool.
TOOL_SPEC_FALLBACK = re.compile(r"[(\s].*$", re.DOTALL)


def tool_base(spec: str) -> str | None:
    """The tool a spec names, or None when its syntax is not one roles may use."""
    match = TOOL_SPEC.match(spec)
    if match is None:
        return None
    if match["arg"] is not None and match["base"] not in PARAMETERISED_TOOLS:
        return None
    return match["base"]


class RoleFileError(Exception):
    """A role or skill file breaks one of the exact rules this module checks."""


@dataclass
class Line:
    text: str
    prevents: str
    evidence: str

    @property
    def provisional(self) -> bool:
        return "provisional" in self.evidence


@dataclass
class Doc:
    path: Path
    meta: dict[str, str | list[str]]
    body: str
    lines: list[Line] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def split_frontmatter(path: Path, text: str) -> tuple[dict[str, str | list[str]], str]:
    """Parse the small YAML subset role files use: `key: value` and `key:` + `- item`."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        raise RoleFileError(f"{rel(path)}: frontmatter opened and never closed")
    meta: dict[str, str | list[str]] = {}
    key = ""
    for raw in text[4:end].splitlines():
        if not raw.strip():
            continue
        if raw.startswith("  - "):
            current = meta.get(key)
            if not isinstance(current, list):
                raise RoleFileError(f"{rel(path)}: list item outside a list: {raw!r}")
            current.append(raw[4:].strip())
        elif ":" in raw and not raw.startswith(" "):
            key, _, value = raw.partition(":")
            key = key.strip()
            meta[key] = value.strip() if value.strip() else []
        else:
            raise RoleFileError(f"{rel(path)}: frontmatter line not understood: {raw!r}")
    return meta, text[end + 5 :]


def parse_commented(path: Path, text: str) -> Doc:
    """A role text: every non-blank, non-heading line is one sentence plus its comment."""
    meta, body = split_frontmatter(path, text)
    doc = Doc(path, meta, body)
    for number, raw in enumerate(body.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            doc.headings.append(line.lstrip("#").strip())
            continue
        if SHARED_MARK.match(line):
            continue
        match = COMMENT.match(line)
        if match is None:
            raise RoleFileError(
                f"{rel(path)}: body line {number} has no comment naming the failure it "
                f"prevents and its evidence: {line[:80]!r}"
            )
        sentence = match["text"].strip()
        if SENTENCE_BREAK.search(sentence) or sentence[-1] not in ".?!":
            raise RoleFileError(
                f"{rel(path)}: body line {number} is not exactly one sentence: {sentence[:80]!r}"
            )
        doc.lines.append(Line(sentence, match["prevents"].strip(), match["evidence"].strip()))
    return doc


def words(doc: Doc) -> int:
    return sum(len(line.text.split()) for line in doc.lines) + sum(
        len(h.split()) for h in doc.headings
    )


def lead_fragment(switch: str, value: str) -> Path:
    return LEAD_DIR / f"{switch}.{value}.md"


# Logical invariant: S3 needs workers, so with single_loop the driver records it
# not_applicable and loads no S3 fragment (contract 7, v0.2.4).
NOT_APPLICABLE = "not_applicable"


def assemble_lead(choice: dict[str, str]) -> str:
    """The run's agents/lead.md: base.md, then one fragment per switch (contract 7)."""
    parts = [(LEAD_DIR / "base.md").read_text()]
    single = choice.get("s2_shape") == "single_loop"
    for switch, values in SWITCHES:
        value = choice.get(switch)
        if switch == "s3_hypothesis_in_briefs" and (single or value == NOT_APPLICABLE):
            if not (single and value == NOT_APPLICABLE):
                raise RoleFileError(
                    f"switch {switch} is {NOT_APPLICABLE} exactly when s2_shape is "
                    f"single_loop: {value!r}"
                )
            continue
        if value not in values:
            raise RoleFileError(f"switch {switch} has no value among {values}: {value!r}")
        parts.append(lead_fragment(switch, value).read_text())
    return "".join(parts)


def all_lead_choices() -> list[dict[str, str]]:
    names = [s for s, _ in SWITCHES]
    choices = []
    for combo in itertools.product(*(v for _, v in SWITCHES)):
        choice = dict(zip(names, combo))
        if choice["s2_shape"] == "single_loop":
            if choice["s3_hypothesis_in_briefs"] != "true":
                continue
            choice["s3_hypothesis_in_briefs"] = NOT_APPLICABLE
        choices.append(choice)
    return choices


def is_parked(choice: dict[str, str]) -> bool:
    return any((switch, value) in PARKED for switch, value in choice.items())


def max_lead_words(shape: str) -> int:
    """The longest lead a run can get with this shape; a parked shape counts its own."""
    shape_parked = ("s2_shape", shape) in PARKED
    return max(
        words(parse_commented(LEAD_DIR / "base.md", assemble_lead(choice)))
        for choice in all_lead_choices()
        if choice["s2_shape"] == shape and (shape_parked or not is_parked(choice))
    )


def lead_page_limit(choice: dict[str, str]) -> int:
    """A page, plus the worker's shared branch method when the lead works every branch."""
    if choice["s2_shape"] != "single_loop":
        return PAGE_WORDS
    shared = SHARED_DIR / "branch-work.md"
    return PAGE_WORDS + words(parse_commented(shared, shared.read_text()))


def shared_blocks(text: str) -> list[tuple[str, str]]:
    """Each (name, content) between a `shared: name` marker and its `end shared`."""
    blocks: list[tuple[str, str]] = []
    lines: list[str] = []
    name = ""
    for raw in text.splitlines(keepends=True):
        mark = SHARED_MARK.match(raw.strip())
        if mark and mark["name"]:
            name, lines = mark["name"], []
        elif mark:
            blocks.append((name, "".join(lines)))
            name = ""
        elif name:
            lines.append(raw)
    return blocks


def role_texts() -> list[Path]:
    return [*sorted(AGENTS_DIR.glob("*.md")), *sorted(LEAD_DIR.glob("*.md"))]


def check_shared(errors: list[str]) -> None:
    sources = {p.stem: p for p in SHARED_DIR.glob("*.md")} if SHARED_DIR.exists() else {}
    used: set[str] = set()
    for name, source in sources.items():
        load_role(source, errors)
    for path in role_texts():
        for name, content in shared_blocks(path.read_text()):
            used.add(name)
            if name not in sources:
                errors.append(f"{rel(path)}: shared block {name!r} has no source")
            elif content != sources[name].read_text():
                errors.append(
                    f"{rel(path)}: shared block {name!r} differs from its source; run sync"
                )
    for name in sorted(set(sources) - used):
        errors.append(f"{rel(sources[name])}: shared block used by no role")


def check_deferred(doc: Doc, errors: list[str]) -> None:
    tools = doc.meta.get("tools", [])
    has_mcp = isinstance(tools, list) and any(t.startswith("mcp__") for t in tools)
    count = doc.body.count(DEFERRED_TOOLS_LINE)
    if has_mcp and MCP_TOOLS_DEFERRED:
        if count != 1:
            errors.append(f"{rel(doc.path)}: needs the ToolSearch line exactly once")
        if isinstance(tools, list) and "ToolSearch" not in tools:
            errors.append(f"{rel(doc.path)}: lists stemma tools but not ToolSearch")
    else:
        if count:
            errors.append(f"{rel(doc.path)}: carries the ToolSearch line it does not need")
        if isinstance(tools, list) and "ToolSearch" in tools:
            errors.append(f"{rel(doc.path)}: lists ToolSearch, which it does not need")


def check_role_meta(doc: Doc, expected_name: str, errors: list[str]) -> None:
    where = rel(doc.path)
    if doc.meta.get("name") != expected_name:
        errors.append(f"{where}: name is {doc.meta.get('name')!r}, expected {expected_name!r}")
    if not doc.meta.get("description"):
        errors.append(f"{where}: no description")
    if doc.meta.get("model") not in APPROVED_MODELS:
        errors.append(f"{where}: model {doc.meta.get('model')!r} is not an approved model")
    if doc.meta.get("effort") not in APPROVED_EFFORTS:
        errors.append(f"{where}: effort {doc.meta.get('effort')!r} is below high or unknown")
    tools = doc.meta.get("tools")
    if not isinstance(tools, list) or not tools:
        errors.append(f"{where}: tools must be a non-empty list")
        return
    if len(set(tools)) != len(tools):
        errors.append(f"{where}: a tool is listed twice")
    for tool in tools:
        base = tool_base(tool)
        if base is None:
            errors.append(f"{where}: {tool!r} is not a tool spec roles may use")
        elif base.startswith("mcp__"):
            if not base.startswith(TOOL_PREFIX):
                errors.append(f"{where}: {tool} does not use the prefix {TOOL_PREFIX}")
            elif base[len(TOOL_PREFIX) :] not in CONTRACT_TOOLS:
                errors.append(f"{where}: {tool} is not a tool the contract defines")
        elif base not in HARNESS_TOOLS:
            errors.append(f"{where}: {tool} is not an allowed harness tool")


def load_role(path: Path, errors: list[str]) -> Doc | None:
    try:
        return parse_commented(path, path.read_text())
    except RoleFileError as error:
        errors.append(str(error))
        return None
    except FileNotFoundError:
        errors.append(f"{rel(path)}: missing")
        return None


def check_roles(errors: list[str]) -> None:
    present = {p.stem for p in AGENTS_DIR.glob("*.md")}
    for extra in sorted(present - set(AGENT_ROLES)):
        errors.append(f"plugin/agents/{extra}.md: not a role in contract section 8")
    for role in AGENT_ROLES:
        doc = load_role(AGENTS_DIR / f"{role}.md", errors)
        if doc is None:
            continue
        check_role_meta(doc, role, errors)
        check_deferred(doc, errors)
        if words(doc) > PAGE_WORDS:
            errors.append(f"{rel(doc.path)}: {words(doc)} words, over a page ({PAGE_WORDS})")
    rules = load_role(STANDING_RULES, errors)
    if rules is not None and len(rules.lines) != 4:
        errors.append(f"{rel(STANDING_RULES)}: {len(rules.lines)} rules, design step 0 has 4")


def check_lead(errors: list[str]) -> None:
    base = load_role(LEAD_DIR / "base.md", errors)
    if base is not None:
        check_role_meta(base, "lead", errors)
        check_deferred(base, errors)
        tools = base.meta.get("tools", [])
        for tool in tools if isinstance(tools, list) else []:
            # Judged on the name the spec reduces to, so no argument form slips past.
            name = tool_base(tool) or TOOL_SPEC_FALLBACK.sub("", tool)
            if name in LEAD_FORBIDDEN_TOOLS:
                errors.append(f"{rel(base.path)}: the lead holds {tool}, a source-reading tool")
    expected = {"base.md"} | {f"{s}.{v}.md" for s, vs in SWITCHES for v in vs}
    present = {p.name for p in LEAD_DIR.iterdir() if p.is_file()}
    for name in sorted(expected - present):
        errors.append(f"plugin/lead/{name}: missing fragment")
    for name in sorted(present - expected):
        errors.append(f"plugin/lead/{name}: not base.md or a switch fragment")
    if expected - present:
        return
    for switch, values in SWITCHES:
        for value in values:
            path = lead_fragment(switch, value)
            text = path.read_text()
            if text.startswith("---"):
                errors.append(f"{rel(path)}: a fragment may not carry frontmatter")
            load_role(path, errors)
    if (
        lead_fragment("s1_method", "decomposition").read_bytes()
        != lead_fragment("s1_method", "placebo").read_bytes()
    ):
        errors.append("plugin/lead: the decomposition and placebo S1 fragments differ")
    if base is None:
        return
    for choice in all_lead_choices():
        try:
            doc = parse_commented(LEAD_DIR / "base.md", assemble_lead(choice))
        except RoleFileError as error:
            errors.append(f"assembled lead {choice}: {error}")
            continue
        # A parked arm cannot run (the driver refuses it), so it needs no page until it
        # is un-parked; its text must still parse.
        if not is_parked(choice) and words(doc) > lead_page_limit(choice):
            errors.append(
                f"assembled lead {choice}: {words(doc)} words, over its limit "
                f"({lead_page_limit(choice)})"
            )
            return


def skill_body(path: Path) -> tuple[dict[str, str | list[str]], str]:
    return split_frontmatter(path, path.read_text())


def skill_sentences(body: str) -> list[str]:
    """Sentences of a skill body: headings dropped, list numbers and bold run-ins split."""
    sentences: list[str] = []
    for paragraph in re.split(r"\n\s*\n", body):
        lines = [ln.strip() for ln in paragraph.splitlines() if ln.strip()]
        lines = [ln for ln in lines if not ln.startswith("#")]
        if not lines:
            continue
        text = re.sub(r"^\d+\.\s+", "", " ".join(lines))
        bold = re.match(r"^\*\*(.+?)\*\*\s*", text)
        if bold:
            sentences.append(bold.group(1).strip())
            text = text[bold.end() :]
        sentences.extend(split_sentences(text))
    return sentences


def split_sentences(text: str) -> list[str]:
    """Split prose into sentences at a terminator followed by space and a capital."""
    out, start = [], 0
    for match in SENTENCE_BREAK.finditer(text):
        end = match.end() - len(match.group(0)) + len(match.group(0).rstrip())
        out.append(text[start:end].strip())
        start = match.end()
    out.append(text[start:].strip())
    return [s for s in out if s]


def load_skill_evidence(errors: list[str]) -> dict[str, list[Line]]:
    """tools/roles/skill-evidence.md: per real skill, each sentence and its comment."""
    evidence: dict[str, list[Line]] = {}
    current = ""
    if not SKILL_EVIDENCE.exists():
        errors.append(f"{rel(SKILL_EVIDENCE)}: missing")
        return evidence
    for number, raw in enumerate(SKILL_EVIDENCE.read_text().splitlines(), start=1):
        line = raw.strip()
        if line.startswith("## "):
            current = line[3:].strip()
            evidence[current] = []
        elif line.startswith("- "):
            match = COMMENT.match(line[2:])
            if match is None or not current:
                errors.append(f"{rel(SKILL_EVIDENCE)}: line {number} is not a commented entry")
                continue
            evidence[current].append(
                Line(match["text"].strip(), match["prevents"].strip(), match["evidence"].strip())
            )
    return evidence


def real_skills() -> list[Path]:
    return sorted(
        p for p in SKILLS_DIR.iterdir() if p.is_dir() and not p.name.startswith("placebo-")
    )


def placebo_twin(real: Path) -> Path:
    name = "placebo-method" if real.name == "decomposition-method" else f"placebo-{real.name}"
    return SKILLS_DIR / name


def check_skills(errors: list[str]) -> None:
    evidence = load_skill_evidence(errors)
    names = {p.name for p in SKILLS_DIR.iterdir() if p.is_dir()}
    reals = real_skills()
    if "decomposition-method" not in names:
        errors.append("plugin/skills/decomposition-method: missing")
    twins = {placebo_twin(r).name for r in reals}
    for stray in sorted(n for n in names if n.startswith("placebo-") and n not in twins):
        errors.append(f"plugin/skills/{stray}: a placebo with no real twin")
    for real in reals:
        skill_file = real / "SKILL.md"
        if not skill_file.exists():
            errors.append(f"{rel(real)}: no SKILL.md")
            continue
        meta, body = skill_body(skill_file)
        if not meta.get("name") or not meta.get("description"):
            errors.append(f"{rel(skill_file)}: frontmatter needs name and description")
        if "<!--" in body:
            errors.append(f"{rel(skill_file)}: comments belong in {rel(SKILL_EVIDENCE)}")
        found = skill_sentences(body)
        listed = [line.text for line in evidence.get(real.name, [])]
        if found != listed:
            missing = [s for s in found if s not in listed]
            stale = [s for s in listed if s not in found]
            detail = missing[:1] or stale[:1] or ["order differs"]
            errors.append(
                f"{rel(skill_file)}: sentences and {rel(SKILL_EVIDENCE)} disagree "
                f"({len(missing)} without evidence, {len(stale)} stale): {detail[0][:80]!r}"
            )
        twin_file = placebo_twin(real) / "SKILL.md"
        if not twin_file.exists():
            errors.append(f"{rel(twin_file)}: missing placebo twin")
            continue
        twin_meta, twin_body = skill_body(twin_file)
        if twin_meta != meta:
            errors.append(f"{rel(twin_file)}: frontmatter differs from {rel(skill_file)}")
        real_len, twin_len = len(body.strip()), len(twin_body.strip())
        if abs(real_len - twin_len) > PLACEBO_LENGTH_TOLERANCE * real_len:
            errors.append(f"{rel(twin_file)}: {twin_len} characters against {real_len} in its twin")
        lowered = twin_body.lower()
        for word in PLACEBO_FORBIDDEN_WORDS:
            if re.search(rf"\b{word}", lowered):
                errors.append(f"{rel(twin_file)}: placebo uses the method word {word!r}")
        if "<!--" in twin_body or "stemma" in lowered:
            errors.append(f"{rel(twin_file)}: placebo carries a comment or a Stemma name")
    for name in sorted(set(evidence) - {r.name for r in reals}):
        errors.append(f"{rel(SKILL_EVIDENCE)}: section {name!r} names no real skill")


def contract_vocabulary() -> tuple[set[str], set[str], str]:
    """Snake_case names, top-level run-folder entries, and the contract's text."""
    text = CONTRACTS.read_text()
    names = set(IDENTIFIER.findall(text))
    section = text[text.index("## 3. The run folder") : text.index("## 4.")]
    tops = set()
    for token in re.findall(r"`([^`]+)`", section):
        first = token.strip().split("/")[0]
        tops.add(first + "/" if "/" in token else first)
    return names, tops, text


def check_prose(doc: Doc, tools: list[str], errors: list[str]) -> None:
    """Names and paths a role reads must exist in the contract and in its tools."""
    names, tops, contract = contract_vocabulary()
    held = {t.removeprefix(TOOL_PREFIX) for t in tools}
    seen_comments: set[tuple[str, str]] = set()
    for line in doc.lines:
        where = f"{rel(doc.path)}: {line.text[:60]!r}"
        key = (line.prevents, line.evidence)
        if key in seen_comments:
            errors.append(f"{where}: shares its comment with another sentence")
        seen_comments.add(key)
        for name in IDENTIFIER.findall(line.text):
            if name not in names:
                errors.append(f"{where}: names {name!r}, which the contract does not")
            elif name in CONTRACT_TOOLS and name not in held:
                errors.append(f"{where}: tells the role to use {name}, which it does not hold")
        for path in PATH_TOKEN.findall(line.text):
            if path.startswith(("/", "~")) or ".." in path.split("/"):
                errors.append(f"{where}: names {path!r}, outside the run folder")
                continue
            first = path.split("/")[0] + ("/" if "/" in path else "")
            if first not in tops and path not in contract and path.split("/")[-1] not in contract:
                errors.append(f"{where}: names the path {path!r}, which the contract does not")


def prose_targets() -> list[tuple[Path, list[str]]]:
    """Each commented role text with the tools of the role that reads it."""

    def tools_of(path: Path) -> list[str]:
        meta, _ = split_frontmatter(path, path.read_text())
        tools = meta.get("tools", [])
        return tools if isinstance(tools, list) else []

    lead_tools = tools_of(LEAD_DIR / "base.md")
    worker_tools = tools_of(AGENTS_DIR / "worker.md")
    out = [(p, tools_of(p)) for p in sorted(AGENTS_DIR.glob("*.md"))]
    out += [(p, lead_tools) for p in sorted(LEAD_DIR.glob("*.md"))]
    out += [(STANDING_RULES, [])]
    out += [(p, sorted(set(worker_tools) & set(lead_tools))) for p in SHARED_DIR.glob("*.md")]
    return out


def check_all_prose(errors: list[str]) -> None:
    for path, tools in prose_targets():
        doc = load_role(path, errors)
        if doc is None:
            continue
        if path == STANDING_RULES:
            tools = [TOOL_PREFIX + t for t in ("capture", "capture_file", "sec_capture")]
        check_prose(doc, tools, errors)


def check_all() -> list[str]:
    errors: list[str] = []
    check_roles(errors)
    check_lead(errors)
    check_skills(errors)
    check_shared(errors)
    if not errors:
        check_all_prose(errors)
    return errors


def short_tool(tool: str) -> str:
    return tool.removeprefix(TOOL_PREFIX)


def render_lines(doc: Doc, out: list[str]) -> None:
    for line in doc.lines:
        mark = " (provisional: not yet settled by a test)" if line.provisional else ""
        out.append(f"- {line.text}")
        out.append(f"  - Prevents: {line.prevents}{mark}. Evidence: {line.evidence}.")


def role_summary(doc: Doc, out: list[str]) -> None:
    tools = doc.meta.get("tools", [])
    tool_names = ", ".join(short_tool(t) for t in tools) if isinstance(tools, list) else ""
    out.append(
        f"Model {doc.meta.get('model')}, effort {doc.meta.get('effort')}. Tools: {tool_names}."
    )
    out.append("")


def build_map() -> str:
    out = [
        "# What each role is told",
        "",
        "Generated by `tools/roles/rolemap.py map` from the files in `plugin/`; do not",
        "edit by hand. Each line a role reads is followed by the failure it prevents and",
        'its evidence. Evidence names: "design" is DESIGN-v1; "lesson" is one of the',
        'practice lessons (a private design record); "P1", "P3" and "D1" are the',
        'research files of those names; "contract" is docs/CONTRACTS.md; "decision D1"',
        'to "decision D10" are the owner\'s decisions of 27 October (V2-CHANGES);',
        '"run 1" is the first live run and its diagnosis (a private design record); the',
        "Opus 5.5 and Fable 5.1 guides are Anthropic's prompting pages. Lines marked",
        "provisional are not yet settled by a test; the E6a reviewer study (26 October)",
        "set the reviewers' model and effort but tested none of those lines directly.",
        "",
    ]
    errors: list[str] = []
    out += ["## At a glance", ""]
    glance = [("lead", LEAD_DIR / "base.md")] + [(r, AGENTS_DIR / f"{r}.md") for r in AGENT_ROLES]
    for role, path in glance:
        doc = load_role(path, errors)
        if doc is None:
            continue
        tools = doc.meta.get("tools", [])
        count = len(tools) if isinstance(tools, list) else 0
        provisional = sum(line.provisional for line in doc.lines)
        note = f", {provisional} provisional" if provisional else ""
        out.append(
            f"- **{role}** ({doc.meta.get('model')}, effort {doc.meta.get('effort')}, "
            f"{count} tools, {len(doc.lines)} lines{note}): {doc.meta.get('description')}"
        )
    out += [
        "",
        "Every role also reads the four standing rules. The lead's text is base.md plus",
        f"one fragment per switch: at most {max_lead_words('team')} words with workers, "
        + f"against a page of {PAGE_WORDS}, and at most {max_lead_words('single_loop')} "
        + "when it works every branch itself and so also carries the worker's method "
        + "(single_loop, parked this round).",
        "",
        "## Standing rules (every role, as the run folder's CLAUDE.md)",
        "",
    ]
    rules = load_role(STANDING_RULES, errors)
    if rules is not None:
        render_lines(rules, out)
    base = load_role(LEAD_DIR / "base.md", errors)
    out += ["", "## Lead", ""]
    if base is not None:
        role_summary(base, out)
        render_lines(base, out)
    for switch, values in SWITCHES:
        for value in values:
            frag = load_role(lead_fragment(switch, value), errors)
            parked = PARKED.get((switch, value))
            note = f" (parked: {parked}; the driver refuses it)" if parked else ""
            out += ["", f"### Lead, when {switch} is {value}{note}", ""]
            if frag is None or not frag.lines:
                out.append("- Adds nothing.")
            else:
                render_lines(frag, out)
    for role in AGENT_ROLES:
        doc = load_role(AGENTS_DIR / f"{role}.md", errors)
        if doc is None:
            continue
        title = doc.headings[0] if doc.headings else role
        out += ["", f"## {title}", ""]
        role_summary(doc, out)
        render_lines(doc, out)
    evidence = load_skill_evidence(errors)
    out += [
        "",
        "## Skills",
        "",
        "Skills carry no comments, so their placebo twins can match them exactly; each",
        "sentence's evidence is kept in tools/roles/skill-evidence.md and shown here.",
        "Each real skill has a placebo twin of the same name, frontmatter and length,",
        "loaded instead in the placebo arm of switch S1.",
    ]
    for real in real_skills():
        meta, _ = skill_body(real / "SKILL.md")
        out += ["", f"### {meta.get('name')} ({real.name})", ""]
        for line in evidence.get(real.name, []):
            out.append(f"- {line.text}")
            out.append(f"  - Prevents: {line.prevents}. Evidence: {line.evidence}.")
    if errors:
        raise RoleFileError("; ".join(errors))
    return "\n".join(out) + "\n"


def retool(new_prefix: str) -> list[Path]:
    """Rewrite TOOL_PREFIX in every role file and in this module; return what changed."""
    if not re.fullmatch(r"mcp__[A-Za-z0-9_]+__", new_prefix):
        raise RoleFileError(f"not an MCP tool prefix: {new_prefix!r}")
    targets = [*AGENTS_DIR.glob("*.md"), *LEAD_DIR.glob("*.md"), ROOT / "tools/roles/rolemap.py"]
    targets = [path for path in targets if path.exists()]
    changed = []
    for path in targets:
        text = path.read_text()
        if TOOL_PREFIX in text:
            path.write_text(text.replace(TOOL_PREFIX, new_prefix))
            changed.append(path)
    return changed


def sync() -> list[Path]:
    """Copy each shared source into every block that names it; return what changed."""
    changed = []
    for path in role_texts():
        text = path.read_text()
        out: list[str] = []
        name = ""
        for raw in text.splitlines(keepends=True):
            mark = SHARED_MARK.match(raw.strip())
            if mark and mark["name"]:
                name = mark["name"]
                out.append(raw)
                out.append((SHARED_DIR / f"{name}.md").read_text())
            elif mark:
                name = ""
                out.append(raw)
            elif not name:
                out.append(raw)
        new = "".join(out)
        if new != text:
            path.write_text(new)
            changed.append(path)
    return changed


def undefer() -> list[Path]:
    """Remove the ToolSearch line from every role and record that tools load at start."""
    changed = []
    for path in role_texts():
        text = path.read_text()
        new = text.replace(DEFERRED_TOOLS_LINE + "\n", "").replace("  - ToolSearch\n", "")
        if new != text:
            path.write_text(new)
            changed.append(path)
    module = ROOT / "tools/roles/rolemap.py"
    if module.exists():
        text = module.read_text()
        flag = "MCP_TOOLS_DEFERRED = "  # split so this line never matches itself
        module.write_text(text.replace(flag + "True", flag + "False", 1))
        changed.append(module)
    return changed


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    command, rest = argv[0], argv[1:]
    if command == "check":
        errors = check_all()
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        if not errors:
            print("role files: all checks passed")
        return 1 if errors else 0
    if command == "map":
        text = build_map()
        if rest == ["--check"]:
            if not ROLE_MAP.exists() or ROLE_MAP.read_text() != text:
                print(f"error: {rel(ROLE_MAP)} is stale; run `map`", file=sys.stderr)
                return 1
            return 0
        ROLE_MAP.write_text(text)
        print(f"wrote {rel(ROLE_MAP)}")
        return 0
    if command == "retool" and len(rest) == 1:
        for path in retool(rest[0]):
            print(f"rewrote {rel(path)}")
        return 0
    if command in ("sync", "undefer") and not rest:
        for path in sync() if command == "sync" else undefer():
            print(f"rewrote {rel(path)}")
        return 0
    if command == "assemble":
        choice = dict(arg.split("=", 1) for arg in rest)
        sys.stdout.write(assemble_lead(choice))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
