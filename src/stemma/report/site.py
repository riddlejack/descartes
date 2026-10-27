"""`stemma report <run> <out_dir>`: a static, self-contained report of a recorded run.

One `index.html` (no scripts or fonts from elsewhere, nothing to install), deployable
as it is on GitHub Pages. In order:
1. the answer, first, with every tagged figure a link to its claim;
2. what "done" means (the criteria) and what the final reviewers said against each;
3. the question broken down: the tree, each node's state and, if set aside, why;
4. how it was checked: checks run, claims the checkers refused and why, figures not
   checked, the reviewers' points round by round, what was not researched;
5. run facts: time, sessions, tokens and cost by role;
6. every cited claim: its statement, tier and verdicts, and the exact stored passage it
   rests on (locator, offsets, the text with context, the capture's SHA-256), or for an
   estimate its script and inputs.

Labelled RECORDED with the run id and date. The page shows what the run recorded and
judges nothing; the one computed status ("not released") is the register's own gate.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from stemma.evidence.errors import EvidenceError
from stemma.evidence.quotes import find_quote
from stemma.register import tags
from stemma.report import md, runread, safefs
from stemma.report.errors import RunReadError
from stemma.report.runread import FINAL_REVIEW_FILES, Run

# Resource limits: characters of context shown each side of a quotation; claims included
# as inputs of cited estimates (followed transitively); characters of a script shown.
CONTEXT_CHARS = 280
MAX_CLAIMS = 3000
MAX_SCRIPT_BYTES = 256 * 1024
# Resource limits: levels of the tree drawn, and of branch folders (branches/<b>/sub/...)
# looked in.
MAX_TREE_DEPTH = 64
MAX_BRANCH_DEPTH = 8
REVIEWER_NAMES = {"stemma:deciders-eye-reviewer": "Decider's-eye reviewer",
                  "stemma:evidence-auditor": "Evidence auditor",
                  "stemma:mechanical-reader": "Mechanical reader"}
TIER_WORDS = {"observed": "Observed in a source, by its author's own measurement",
              "interested_party": "Stated by a party with a stake",
              "secondary": "Reported by a source from someone else's figure",
              "estimated": "Estimated: computed by the run, with a recorded calculation",
              "assumed": "Assumed: an input the run chose"}
VERDICT_WORDS = {"holds": "Holds", "holds_with_correction": "Holds, with a correction",
                 "not_supported": "Not supported", "could_not_check": "Not checked",
                 None: "No checker verdict"}
STATE_WORDS = {"answered": "Answered", "gap_accepted": "Gap accepted",
               "stopped_by_limit": "Stopped by a resource limit", "set_aside": "Set aside",
               "bounded": "Bounded", "conditional": "Conditional",
               "in_progress": "Not finished", "open": "Not started",
               "proposed_done": "Awaiting its reviewer", "waiting_user": "Waiting on the user",
               "not_needed": "Nothing filed"}
KIND_WORDS = {"option": "option", "condition": "condition", "rival": "rival",
              "open_lane": "open lane", "question": ""}


def esc(v: object) -> str:
    return html.escape(str(v), quote=True)


def short(cid: str) -> str:
    return cid[:12]


def _date(iso: str | None) -> str:
    if not iso:
        return "unknown date"
    try:
        return datetime.fromisoformat(iso).strftime("%-d %B %Y")
    except ValueError:
        return iso[:10]


def _verdict(c: dict[str, Any]) -> str | None:
    v = (c.get("verdict") or {}).get("verdict")
    return str(v) if v else None


# --------------------------------------------------------------------- claims
class Claims:
    """The claims the page shows (cited, and the inputs of cited estimates) and their
    numbers in order of first use in the answer."""

    def __init__(self, run: Run) -> None:
        self.run = run
        self.cited = run.cited()
        self.number: dict[str, int] = {cid: i for i, cid in enumerate(self.cited, start=1)}
        shown: list[str] = []
        todo = list(self.cited)
        while todo and len(shown) < MAX_CLAIMS:
            cid = todo.pop(0)
            if cid in shown:
                continue
            shown.append(cid)
            c = run.claims[cid]
            for given in (c.get("derivation") or {}).get("inputs") or []:
                inp = run.claim(str(given))
                if inp is not None and inp["claim_id"] not in shown:
                    todo.append(str(inp["claim_id"]))
        self.shown = shown
        self.by_short = {short(cid): cid for cid in shown}

    def chip(self, s: str) -> str:
        """A figure's tag in the answer: a numbered link to its claim."""
        cid = self.by_short.get(s)
        if cid is None:
            return f'<span class="chip broken" title="This tag names no claim">c:{esc(s)}</span>'
        c = self.run.claims[cid]
        v = _verdict(c)
        cls = {"holds": "ok", "holds_with_correction": "ok", "not_supported": "bad"}.get(
            v or "", "warn")
        n = self.number.get(cid)
        label = str(n) if n else s
        extra = " not checked" if v in ("could_not_check", None) else ""
        return (f'<a class="chip {cls}" href="#c-{s}" data-claim="{s}" '
                f'title="{esc(c["statement"][:200])}">{label}{extra}</a>')

    def ref(self, s: str) -> str:
        """A tag inside another text (a verdict, a review): a link if the page holds it."""
        if s in self.by_short:
            return f'<a class="ref" href="#c-{s}" data-claim="{s}">c:{s}</a>'
        return f"<code>c:{esc(s)}</code>"


def _locator(rec: dict[str, Any], start: int) -> str:
    kind = None
    where = None
    for o in rec.get("outline") or []:
        try:
            if int(o["start"]) <= start <= int(o.get("end", start)):
                if o.get("kind") == "page":
                    kind, where = "page", str(o.get("title") or o.get("anchor"))
                elif kind != "page":
                    kind, where = "section", str(o.get("title"))
        except (KeyError, TypeError, ValueError):
            continue
    return f"{where}, " if where else ""


def passage_html(run: Run, item: dict[str, Any]) -> str:
    rid = str(item.get("receipt_id"))
    quote = str(item.get("quote") or "")
    try:
        rec = runread.receipt(run.dir, rid)
    except RunReadError as exc:
        return f'<div class="passage missing">Receipt {esc(rid)} is refused: {esc(exc)}</div>'
    if rec is None:
        return f'<div class="passage missing">Receipt {esc(rid)} is not in this run.</div>'
    url = rec.get("final_url") or rec.get("requested_url") or rec.get("path") or ""
    link = (f'<a href="{esc(url)}" rel="noopener noreferrer">{esc(url)}</a>'
            if str(url).startswith(("http://", "https://")) else esc(url))
    meta = (f'<div class="meta">Source: {link}<br>Captured {esc(_date(rec.get("fetched_at")))}'
            f' · receipt <code>{esc(rid)}</code> · SHA-256 of the captured bytes '
            f'<code class="hash">{esc(rec.get("sha256"))}</code></div>')
    try:
        text = runread.stored_text(run.dir, rec)
    except RunReadError as exc:
        return f'<div class="passage missing">{meta}The stored text is refused: {esc(exc)}</div>'
    if text is None:
        return f'<div class="passage missing">{meta}The stored text is missing.</div>'
    try:
        found = find_quote(text, quote, runread.flagged_ranges(rec))
    except (EvidenceError, RunReadError) as exc:
        return (f'<div class="passage missing">{meta}<p>The quotation cannot be checked: '
                f"{esc(exc)}</p></div>")
    if not found["found"]:
        return (f'<div class="passage missing">{meta}<p>The quotation is <strong>not found'
                f'</strong> in the stored text now:</p><blockquote>{esc(quote)}</blockquote></div>')
    m = found["matches"][0]
    start, end = int(m["start"]), int(m["end"])
    a, b = max(0, start - CONTEXT_CHARS), min(len(text), end + CONTEXT_CHARS)
    more = (f" It occurs {found['match_count']} times; the first is shown."
            if found.get("match_count", 1) > 1 else "")
    warn = "".join(f'<p class="warn">{esc(w)}</p>' for w in found.get("warnings") or [])
    return (f'<div class="passage">{meta}<p class="loc">{esc(_locator(rec, start))}characters '
            f'{start:,}–{end:,} of {len(text):,} in the stored text.{more}</p>'
            f'<pre class="source">{"…" if a else ""}{esc(text[a:start])}<mark>'
            f'{esc(text[start:end])}</mark>{esc(text[end:b])}{"…" if b < len(text) else ""}'
            f"</pre>{warn}</div>")


def _value_line(c: dict[str, Any]) -> str:
    v = c.get("value") or {}
    if not v:
        return ""
    parts = [f"<strong>{esc(v.get('as_written') or v.get('number'))}</strong>"]
    if v.get("number") is not None and str(v.get("number")) != str(v.get("as_written")):
        parts.append(f"(recorded as {esc(v['number'])})")
    for k, label in (("unit", "unit"), ("period", "period"), ("kind", "kind"),
                     ("denominator", "denominator")):
        if v.get(k):
            parts.append(f"{label}: {esc(v[k])}")
    return f'<p class="value">{" · ".join(parts)}</p>'


def claim_html(run: Run, claims: Claims, cid: str) -> str:
    c = run.claims[cid]
    s = short(cid)
    v = _verdict(c)
    num = claims.number.get(cid)
    title = f"Figure {num}" if num else "Input to a figure"
    out = [f'<section class="claim" id="c-{s}"><h3>{title} <code>c:{s}</code></h3>',
           f'<p class="statement">{md.inline(c["statement"], claims.ref)}</p>',
           _value_line(c),
           f'<p class="tier">{esc(TIER_WORDS.get(c.get("tier", ""), c.get("tier")))}</p>']
    cls = {"holds": "ok", "holds_with_correction": "ok", "not_supported": "bad"}.get(v or "",
                                                                                     "warn")
    out.append(f'<p class="verdict {cls}">Claim checker: {esc(VERDICT_WORDS.get(v, v))}</p>')
    ev = (c.get("verdict") or {}).get("evidence")
    if ev:
        out.append(f"<details><summary>The checker's reasons</summary>"
                   f"{md.render(str(ev), claims.ref, heading_shift=3)}</details>")
    hist = c.get("verdict_history") or []
    if len(hist) > 1:
        out.append(f'<p class="small">Earlier verdicts: '
                   f'{esc(", ".join(VERDICT_WORDS.get(h.get("verdict"), "?") for h in hist[:-1]))}'
                   f"</p>")
    if c.get("superseded_by"):
        sup = c["superseded_by"]
        nxt = str(sup.get("claim_id", "") if isinstance(sup, dict) else sup)
        out.append(f'<p class="warn">Superseded by {claims.ref(nxt[:12])}.</p>')
    for item in c.get("support") or []:
        out.append(passage_html(run, item))
    deriv = c.get("derivation")
    if deriv:
        out.append(_derivation_html(run, claims, deriv))
    if c.get("warnings"):
        out.append("".join(f'<p class="warn">{esc(w)}</p>' for w in c["warnings"]))
    passed = [k.get("check") for k in c.get("checks") or [] if k.get("passed")]
    if passed:
        out.append(f'<p class="small">Code checks passed at filing: {esc(", ".join(passed))}.</p>')
    out.append("</section>")
    return "\n".join(out)


def _derivation_html(run: Run, claims: Claims, deriv: dict[str, Any]) -> str:
    script = str(deriv.get("script"))
    rows = []
    for given in deriv.get("inputs") or []:
        inp = run.claim(str(given))
        if inp is None:
            rows.append(f"<li><code>{esc(given)}</code>: no such claim</li>")
            continue
        val = (inp.get("value") or {}).get("number")
        rows.append(f"<li>{claims.ref(short(inp['claim_id']))} = <strong>{esc(val)}</strong>: "
                    f"{md.inline(inp['statement'], claims.ref)}</li>")
    docs = "".join(f"<li><code>{esc(d)}</code></li>" for d in deriv.get("documents") or [])
    code = _script_text(run.dir, script)
    return (f'<div class="derivation"><p><strong>Calculation</strong> <code>{esc(script)}</code>'
            f", re-run by the register at filing, which reproduced this number exactly.</p>"
            f"<p>Inputs ({len(rows)}):</p><ul>{''.join(rows)}</ul>"
            + (f"<p>Documents it reads:</p><ul>{docs}</ul>" if docs else "")
            + f"<details><summary>The script, as stored in the run</summary>"
              f'<pre class="code">{esc(code)}</pre></details></div>')


def _script_text(run_dir: Path, script: str) -> str:
    """A recorded script's text: a .py file under analysis/, read confined and bounded."""
    if not (script.startswith("analysis/") and script.endswith(".py")):
        return "(the recorded script path is not a .py file in analysis/; not shown)"
    try:
        text = safefs.read_text(run_dir, script, MAX_SCRIPT_BYTES)
    except safefs.Missing:
        return "(the script is not in the run folder)"
    except RunReadError as exc:
        return f"(the script cannot be shown: {exc})"
    return text


# ----------------------------------------------------------------------- tree
def _branch_folders(run_dir: Path) -> list[str]:
    """branches/<b> and branches/<b>/sub/<b2>/..., found iteratively, depth-limited."""
    out: list[str] = []
    stack = [(f"branches/{n}", 0) for n in safefs.listdir(run_dir, "branches")]
    while stack:
        rel, depth = stack.pop()
        if depth > MAX_BRANCH_DEPTH or not safefs.SEGMENT.fullmatch(rel.rsplit("/", 1)[-1]):
            continue
        out.append(rel)
        try:
            subs = safefs.listdir(run_dir, f"{rel}/sub")
        except RunReadError:
            continue  # not a folder, or a link: nothing below it is read
        stack.extend((f"{rel}/sub/{n}", depth + 1) for n in subs)
    return sorted(out)


def _branch_verdicts(run_dir: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for folder in _branch_folders(run_dir):
        try:
            names = safefs.listdir(run_dir, f"{folder}/verdicts")
        except RunReadError:
            continue
        numbers = sorted(int(n[:-5]) for n in names
                         if n.endswith(".json") and n[:-5].isdigit() and len(n) < 16)
        if numbers:
            v = runread.read_json(run_dir, f"{folder}/verdicts/{numbers[-1]}.json")
            if isinstance(v, dict) and v.get("branch_id"):
                latest[str(v["branch_id"])] = v
    return latest


def node_state(n: dict[str, Any]) -> tuple[str, str]:
    """(state key, the reason a node was set aside or bounded, if recorded)."""
    if n.get("closes_by") == "ancestor_set_aside":
        return "set_aside", f"it sits under {n.get('set_aside_by')}, which was set aside"
    reason = str(n.get("set_aside_reason") or n.get("reason_set_aside") or
                 (n.get("set_aside") if isinstance(n.get("set_aside"), str) else "") or "")
    if n.get("set_aside") or reason or n.get("status") == "set_aside":
        return "set_aside", reason
    for key in ("answer_form", "form", "state"):
        if n.get(key) in ("bounded", "conditional"):
            return str(n[key]), str(n.get("condition") or n.get("bound") or "")
    return str(n.get("status") or "open"), ""


def tree_html(run: Run, claims: Claims) -> str:
    if not run.nodes:
        return "<p>No tree was recorded.</p>"
    verdicts = _branch_verdicts(run.dir)
    children: dict[str | None, list[dict[str, Any]]] = {}
    ids = {str(n["id"]) for n in run.nodes}
    for n in run.nodes:
        parent = n.get("parent")
        children.setdefault(str(parent) if parent is not None and str(parent) in ids
                            else None, []).append(n)

    def item(n: dict[str, Any]) -> str:
        state, reason = node_state(n)
        kind = KIND_WORDS.get(str(n.get("kind")), str(n.get("kind")))
        badge = f'<span class="kind">{esc(kind)}</span> ' if kind else ""
        crux = '<span class="kind">crux</span> ' if n.get("crux") else ""
        v = n.get("branch_verdict") or verdicts.get(str(n.get("branch_id")))
        v = v if isinstance(v, dict) else None
        forms = sorted({str(r.get("status")) for r in (v or {}).get("resolutions") or []
                        if isinstance(r, dict)} & {"bounded", "conditional"})
        extra = "".join(f' <span class="state s-{f}">{esc(STATE_WORDS[f])}</span>'
                        for f in forms) if state == "answered" else ""
        body = [(f'<span class="state s-{esc(state)}">{esc(STATE_WORDS.get(state, state))}'
                f"</span>{extra} {badge}{crux}<code>{esc(n['id'])}</code> "
                f"{md.inline(str(n.get('statement', '')), claims.ref)}")]
        if reason:
            body.append(f'<div class="why">Set aside because: {esc(reason)}</div>')
        if v is not None and v.get("reason"):
            gap = f", gap type {esc(v['gap_type'])}" if v.get("gap_type") else ""
            body.append(f"<details><summary>Branch reviewer: {esc(v.get('verdict'))}{gap}"
                        f" (branch {esc(v.get('branch_id'))}, round {esc(v.get('round'))})"
                        f"</summary>{md.render(str(v['reason']), claims.ref, 3)}</details>")
        return "".join(body)

    # Iterative depth-first rendering; a node is shown once, so a cycle in the parents
    # cannot loop, and nodes deeper than the limit are listed flat with a note.
    out: list[str] = []
    seen: set[str] = set()
    stack_nodes: list[tuple[str, Any]] = [("kids", (None, 0))]
    while stack_nodes:
        what, arg = stack_nodes.pop()
        if what == "emit":
            out.append(arg)
            continue
        parent, depth = arg
        kids = [n for n in children.get(parent, []) if str(n["id"]) not in seen]
        if not kids:
            continue
        if depth > MAX_TREE_DEPTH:
            out.append(f'<p class="small">{len(kids)} more node(s) deeper than '
                       f"{MAX_TREE_DEPTH} levels are not drawn (a resource limit).</p>")
            continue
        out.append('<ul class="tree">')
        stack_nodes.append(("emit", "</ul>"))
        for n in reversed(kids):
            seen.add(str(n["id"]))
            stack_nodes.append(("emit", "</li>"))
            stack_nodes.append(("kids", (str(n["id"]), depth + 1)))
            stack_nodes.append(("emit", f"<li>{item(n)}"))
    unreached = [n for n in run.nodes if str(n["id"]) not in seen]
    if unreached:
        out.append('<p class="small">Nodes not reachable from a root (their parents form a '
                   "cycle):</p><ul>" + "".join(f"<li>{item(n)}</li>" for n in unreached)
                   + "</ul>")

    old = any(n.get("kind") in ("option", "condition", "rival") for n in run.nodes)
    note = ('<p class="small">This run used the earlier tree form, with options, conditions '
            "and rivals as nodes; later runs break the question into sub-questions only.</p>"
            if old else "")
    return note + "".join(out)


# ------------------------------------------------------------------ criteria
def _points(r: dict[str, Any]) -> list[dict[str, Any]]:
    pts = r.get("points") or []
    return [p for p in pts if isinstance(p, dict)] if isinstance(pts, list) else []


def _reviews(run: Run) -> dict[int, dict[str, dict[str, Any]]]:
    by_version: dict[int, dict[str, dict[str, Any]]] = {}
    for r in run.run_facts.get("review_recorded", []):
        v = r.get("version") if isinstance(r, dict) else None
        if isinstance(v, int) and not isinstance(v, bool) and isinstance(r.get("role"), str):
            by_version.setdefault(v, {})[r["role"]] = r
    return by_version


def criteria_html(run: Run) -> str:
    """Each criterion and what the final reviewers of the displayed draft recorded
    against it. Green only when all three required reviews of that draft exist and none
    holds a material point against the criterion; a missing review leaves it unchecked."""
    if not run.criteria:
        return ("<p>This run recorded no criteria for what a finished answer must meet "
                "(runs since the onboarding step record three to five).</p>")
    version = run.deliverable.version
    have = _reviews(run).get(version, {}) if version is not None else {}
    missing = [REVIEWER_NAMES[r] for r in FINAL_REVIEW_FILES if r not in have]
    points = [p for r in have.values() for p in _points(r)]
    rows = []
    for cr in run.criteria:
        mine = [p for p in points if p.get("criterion") == cr.cid]
        material = [p for p in mine if p.get("material") is True]
        if version is None or len(missing) == len(FINAL_REVIEW_FILES):
            said, cls = "Not checked: no final review of the shown draft was recorded.", "warn"
        elif material:
            said = (f"{len(material)} open material point{'s' if len(material) > 1 else ''} "
                    f"raised against it in the reviews of draft v{version}:")
            cls = "bad"
        elif missing:
            said = (f"Not checked: the review{'s' if len(missing) > 1 else ''} of draft "
                    f"v{version} by {' and '.join(missing)} "
                    f"{'are' if len(missing) > 1 else 'is'} not recorded.")
            cls = "warn"
        elif mine:
            said, cls = (f"Only points the reviewers marked not material, in the reviews of "
                         f"draft v{version}."), "ok"
        else:
            said, cls = (f"All three reviewers of draft v{version} recorded no material "
                         f"point against it."), "ok"
        pts = "".join(f"<li>{esc(p.get('point'))}</li>" for p in material)
        rows.append(f'<li><strong>{esc(cr.cid)}</strong> {esc(cr.text)}<div class="said '
                    f'{cls}">{esc(said)}</div>{"<ul>" + pts + "</ul>" if pts else ""}</li>')
    src = f" (from {esc(run.criteria_source)})" if run.criteria_source else ""
    return (f"<p>What the answer had to do to count as done{src}. The reviewers judge "
            f"each; this page only reports what they recorded about the draft shown.</p>"
            f"<ol class=\"criteria\">{''.join(rows)}</ol>")


# --------------------------------------------------------- how it was checked
def _record_section(text: str, heading: str) -> str:
    m = re.search(rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", text, re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ""


def checked_html(run: Run, claims: Claims) -> str:
    out: list[str] = []
    all_claims = list(run.claims.values())
    tiers = Counter(str(c.get("tier")) for c in all_claims)
    verdicts = Counter(_verdict(c) for c in all_claims)
    cited = [run.claims[c] for c in claims.cited]
    cited_v = Counter(_verdict(c) for c in cited)
    checks = runread.jsonl_records(run.dir, "register/checks.jsonl")
    requests = Counter(str(r.get("role")) for r in checks if r.get("kind") == "request")
    out.append("<h3>Checks run</h3><ul>")
    out.append(f"<li>{len(all_claims):,} claims filed: "
               + ", ".join(f"{n:,} {esc(t.replace('_', ' '))}" for t, n in tiers.most_common())
               + ".</li>")
    out.append("<li>Code checked every one at filing: each quotation found in its stored "
               "source, each figure inside its quotation, each estimate re-run.</li>")
    out.append("<li>Claim-checker verdicts on all claims: "
               + ", ".join(f"{n:,} {esc(VERDICT_WORDS.get(v, v)).lower()}"
                           for v, n in verdicts.most_common()) + ".</li>")
    out.append(f"<li>The answer cites {len(cited):,} claims: "
               + ", ".join(f"{n:,} {esc(VERDICT_WORDS.get(v, v)).lower()}"
                           for v, n in cited_v.most_common()) + ".</li>")
    if requests:
        out.append("<li>Checking sessions the host asked for, by role: "
                   + ", ".join(f"{esc(r)} {n:,}" for r, n in requests.most_common()) + ".</li>")
    out.append("</ul>")

    refused = [c for c in all_claims if _verdict(c) == "not_supported"]
    out.append(f"<h3>Claims the checkers refused ({len(refused)})</h3>")
    if refused:
        out.append('<p class="small">A refused claim may not appear in a released answer. '
                   "Those the answer cites are marked.</p><ul class=\"refused\">")
        for c in sorted(refused, key=lambda c: c["claim_id"] not in claims.number):
            mark = (' <span class="state s-bad">cited</span>'
                    if c["claim_id"] in claims.number else "")
            ev = str((c.get("verdict") or {}).get("evidence") or "")
            out.append(f"<li>{claims.ref(short(c['claim_id']))}{mark} "
                       f"{md.inline(c['statement'], claims.ref)}<details><summary>Why"
                       f"</summary>{md.render(ev, claims.ref, 3)}</details></li>")
        out.append("</ul>")
    unchecked = [c for c in cited if _verdict(c) in ("could_not_check", None)]
    out.append(f"<h3>Figures in the answer not checked ({len(unchecked)})</h3>")
    if unchecked:
        out.append("<ul>" + "".join(
            f"<li>{claims.ref(short(c['claim_id']))} {md.inline(c['statement'], claims.ref)} "
            f"({esc(VERDICT_WORDS.get(_verdict(c)))})</li>" for c in unchecked) + "</ul>")
    else:
        out.append("<p>None: every cited figure has a checker's verdict.</p>")

    out.append("<h3>The reviewers' points, round by round</h3>")
    out.append(reviews_html(run))

    crit = runread.read_text(run.dir, "framer/critique.md")
    resp = runread.read_text(run.dir, "framer/lead-response.md")
    if crit:
        out.append("<h3>The blind framer's challenge</h3><p>A separate agent framed the "
                   "question without seeing the lead's frame, then critiqued it.</p>"
                   f"<details><summary>The critique</summary>{md.render(crit, claims.ref, 3)}"
                   "</details>")
        out.append(f"<details><summary>The lead's response</summary>"
                   f"{md.render(resp, claims.ref, 3)}</details>" if resp
                   else "<p>The lead's response was not recorded.</p>")

    out.append("<h3>What was not researched or not checked</h3>")
    gaps = []
    for n in run.nodes:
        state, reason = node_state(n)
        if state == "set_aside":
            gaps.append(f"<li><code>{esc(n['id'])}</code> set aside: "
                        f"{esc(reason or 'no reason recorded')}</li>")
        elif state in ("gap_accepted", "stopped_by_limit"):
            gaps.append(f"<li><code>{esc(n['id'])}</code> {esc(STATE_WORDS[state]).lower()}: "
                        f"{md.inline(str(n.get('statement', '')), claims.ref)}</li>")
    if gaps:
        out.append("<ul>" + "".join(gaps) + "</ul>")
    for folder in _branch_folders(run.dir):
        try:
            text = runread.read_text(run.dir, f"{folder}/record.md") or ""
        except RunReadError as exc:
            text = f"## What was not checked\n\n(the record cannot be read: {exc})"
        body = _record_section(text, "What was not checked")
        if body:
            name = folder.rsplit("/", 1)[-1]
            out.append(f"<details><summary>Branch {esc(name)}: what was not "
                       f"checked</summary>{md.render(body, claims.ref, 3)}</details>")
    return "\n".join(out)


def reviews_html(run: Run) -> str:
    reviews = _reviews(run)
    if not reviews:
        return "<p>No final review was recorded.</p>"
    submitted = sorted(runread.submitted_versions(run.run_facts))
    out = []
    for v in sorted(reviews):
        later = [s for s in submitted if s > v]
        then = (f"The lead then submitted draft v{later[0]}." if later
                else "No later draft was submitted.")
        pts = [(role, p) for role, r in reviews[v].items() for p in _points(r)]
        material = sum(1 for _r, p in pts if p.get("material"))
        out.append(f"<details{' open' if v == max(reviews) else ''}><summary>Draft v{v}: "
                   f"{len(pts)} points, {material} material. {esc(then)}</summary>")
        for role in FINAL_REVIEW_FILES:
            r = reviews[v].get(role)
            if r is None:
                out.append(f"<p>{esc(REVIEWER_NAMES[role])}: not recorded.</p>")
                continue
            items = []
            for p in _points(r):
                tag = ('<span class="state s-bad">material</span>' if p.get("material")
                       else '<span class="state s-open">not material</span>')
                where = " ".join(f"<code>{esc(p[k])}</code>" for k in ("criterion", "node")
                                 if p.get(k))
                items.append(f"<li>{tag} {where} {esc(p.get('point'))}</li>")
            out.append(f"<p><strong>{esc(REVIEWER_NAMES[role])}</strong></p>"
                       f"<ul>{''.join(items) or '<li>No points.</li>'}</ul>")
        responses = [f"reviews/v{v}/{n}" for n in safefs.listdir(run.dir, f"reviews/v{v}")
                     if "response" in n and n.endswith(".md")]
        if later:
            responses.append(f"draft/response.v{later[0]}.md")
        for rel in responses:
            text = runread.read_text(run.dir, rel)
            if text is not None:
                out.append(f"<details><summary>The lead's response ({esc(rel)})</summary>"
                           f"{md.render(text, heading_shift=3)}</details>")
        out.append("</details>")
    return "\n".join(out)


# ------------------------------------------------------- the driver and the task
# The driver's outcomes and exit codes (W2's OUTCOME_CODES, contract v0.4), in words.
OUTCOME_WORDS = {
    "released": "released",
    "ended_without_release": "the lead stopped and nothing released the run",
    "canary_failed": "the lead did not start clean (the canary failed)",
    "stopped_by_limit": "stopped at a recorded resource limit",
    "lead_failed": "the lead could not start or crashed",
    "release_withdrawn": "released, then withdrawn: a check it rested on was revoked",
    "revocation_failed": "a wrong-model check could not be revoked, so release is blocked",
    "usage_limited": "stopped by the account's usage limit; a resume continues it",
    "frame_complete": "a frame-only run: it ended once the frame, the tree and the answer "
                      "to the frame critique existed",
    "driver_stopped": "the driver was stopped",
}


def driver_outcome(run: Run) -> tuple[str | None, int | None]:
    """The last `driver_finished` event's outcome and exit code, as recorded."""
    for e in reversed(_events(run.dir)):
        if e.get("event") == "driver_finished":
            code = e.get("exit_code")
            return (str(e.get("outcome")) if e.get("outcome") else None,
                    code if isinstance(code, int) and not isinstance(code, bool) else None)
    return None, None


def waiting_html(run: Run) -> str:
    st = runread.read_json(run.dir, "status.json")
    if isinstance(st, dict) and st.get("state") == "waiting_for_usage_reset":
        until = str(st.get("resume_at") or "an unrecorded time")
        return (f'<div class="banner warn"><strong>Paused for the usage limit.</strong> The '
                f"account's usage limit stopped the run; the driver resumes it at "
                f"{esc(until.replace('T', ' '))}. This page shows the run as it stood.</div>")
    return ""


def task_html(run: Run) -> str:
    t = run.task
    out = []
    options = t.get("options")
    if isinstance(options, list) and options:
        out.append("<p>Options the user named, compared on the same criteria: "
                   + "; ".join(esc(o) for o in options) + ".</p>")
    if t.get("stop_after") == "frame":
        out.append("<p>Asked for the frame only: the run was to stop once the frame, the "
                   "tree and the answer to the frame critique existed.</p>")
    intake = t.get("intake")
    if isinstance(intake, dict) and intake:
        rows = "".join(f"<li><strong>{esc(k)}</strong>: {esc(v)}</li>"
                       for k, v in intake.items())
        out.append(f"<details><summary>What the user said at the start (intake and "
                   f"onboarding)</summary><ul>{rows}</ul></details>")
    return "".join(out)


def record_html(run: Run, claims: Claims) -> str:
    """deliverable/record.json (written by finish_run, v0.4), shown only with a valid
    release: what the run left open."""
    if run.release.state != "released":
        return ""
    rec = runread.read_json(run.dir, "deliverable/record.json")
    if not isinstance(rec, dict):
        return ""
    out = ["<h3>What the released answer left open</h3><ul>"]
    for n in rec.get("set_aside") or []:
        if isinstance(n, dict):
            why = (f"set aside: {esc(n.get('reason'))}" if n.get("reason")
                   else f"under {esc(n.get('under'))}, which was set aside")
            out.append(f"<li><code>{esc(n.get('id'))}</code> {esc(n.get('statement'))} "
                       f"({why})</li>")
    for a in rec.get("assumptions") or []:
        if isinstance(a, dict):
            out.append(f"<li>Assumed in place of the user's answer (branch "
                       f"{esc(a.get('branch_id'))}): {esc(a.get('assumption'))}"
                       + (f". The question was: {esc(a.get('question'))}"
                          if a.get("question") else "") + "</li>")
    for b in rec.get("branch_limits") or []:
        if isinstance(b, dict):
            out.append(f"<li>An open limit of branch {esc(b.get('branch_id'))}: "
                       f"{esc(b.get('point'))}</li>")
    omp = rec.get("open_material_points")
    seen: set[str] = set()
    if isinstance(omp, dict):
        for group in ("by_criterion", "by_node"):
            for key, pts in (omp.get(group) or {}).items():
                for p in pts if isinstance(pts, list) else []:
                    if isinstance(p, dict) and str(p.get("id") or p.get("point")) not in seen:
                        seen.add(str(p.get("id") or p.get("point")))
                        out.append(f"<li>A material point left open at the review-round "
                                   f"limit ({esc(p.get('by'))}, {esc(key)}): "
                                   f"{esc(p.get('point'))}</li>")
    rounds = rec.get("review_rounds")
    if isinstance(rounds, dict):
        out.append(f"<li>Review rounds used: {esc(rounds.get('used'))} of "
                   f"{esc(rounds.get('limit'))} ({esc(rounds.get('note'))}).</li>")
    out.append("</ul>")
    return "".join(out) if len(out) > 2 else ""


# ----------------------------------------------------------------- run facts
def _events(run_dir: Path) -> list[dict[str, Any]]:
    return runread.jsonl_records(run_dir, "events.jsonl")


def facts_html(run: Run) -> str:
    events = _events(run.dir)
    times = [str(e["at"]) for e in events if isinstance(e.get("at"), str)]
    start, end = (min(times), max(times)) if times else (None, None)
    out = ["<ul>"]
    out.append(f"<li>Run id <code>{esc(run.run_id)}</code>, started "
               f"{esc((start or run.run.get('created_at') or '')[:16].replace('T', ' '))} UTC"
               + (f", last event {esc(end[:16].replace('T', ' '))} UTC" if end else "")
               + _span(start, end) + ".</li>")
    outcome, code = driver_outcome(run)
    if outcome:
        out.append(f"<li>The driver's outcome: {esc(OUTCOME_WORDS.get(outcome, outcome))}"
                   + (f" (exit {code})" if code is not None else "") + ".</li>")
    harness = (run.run.get("harness") or {}).get("version")
    if harness:
        out.append(f"<li>Harness: Claude Code "
                   f"{esc(str(harness).replace(' (Claude Code)', ''))}.</li>")
    out.append("</ul>")
    out.append(_usage_html(run, events))
    return "\n".join(out)


def _span(a: str | None, b: str | None) -> str:
    if not a or not b:
        return ""
    try:
        d = (datetime.fromisoformat(b)
             - datetime.fromisoformat(a))
    except ValueError:
        return ""
    h, m = divmod(int(d.total_seconds()) // 60, 60)
    return f" ({h} h {m} min)"


def _usage_html(run: Run, events: list[dict[str, Any]]) -> str:
    from stemma.driver import usage

    models = run.run.get("models") or {}
    records = [r for r in runread.jsonl_records(run.dir, "usage.jsonl")
               if all(isinstance(r.get(k), t) for k, t in
                      (("kind", str), ("role", str), ("session", str), ("tokens", dict)))]
    if records:
        s = usage.summary(records)
        rows = []
        for role, row in sorted(s["by_role"].items(), key=lambda kv: -kv[1]["tokens"][
                "output_tokens"]):
            t = row["tokens"]
            total = sum(t.values())
            dollars = ("inside its parent session" if "subagent" in role
                       else f"${row['cost_estimate_usd']:,.2f}")
            rows.append(f"<tr><td>{esc(role)}</td><td>{row['sessions']}</td>"
                        f"<td>{total:,}</td><td>{t['output_tokens']:,}</td><td>{dollars}</td>"
                        f"<td>{row['wall_s'] / 3600:.1f} h</td></tr>")
        tot = s["tokens"]
        return (f"<p>Tokens and cost by role, counted from every session's transcript. Cost is "
                f"{esc(s['cost_note'])}. Whole run: {sum(tot.values()):,} tokens "
                f"({tot['output_tokens']:,} output), cost estimate "
                f"${s['cost_estimate_usd']:,.2f}.</p><div class=\"table\"><table><thead><tr>"
                "<th>Role</th><th>Sessions</th><th>Tokens</th><th>Output</th><th>Cost</th>"
                f"<th>Time</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
                + _models_line(models))
    launched: Counter[str] = Counter()
    cost: dict[str, float] = {}
    for e in events:
        role = str(e.get("role"))
        if e.get("event") == "session_launched":
            launched[role] += 1
        elif e.get("event") == "session_ended":
            c = (e.get("result") or {}).get("total_cost_usd")
            if isinstance(c, int | float):
                cost[role] = cost.get(role, 0.0) + float(c)
    if not launched:
        return "<p>No token accounting or session events were recorded.</p>" + \
            _models_line(models)
    table = "".join(f"<tr><td>{esc(r)}</td><td>{n:,}</td><td>"
                    f"{'$' + format(cost[r], ',.2f') if r in cost else 'not recorded'}</td></tr>"
                    for r, n in launched.most_common())
    return ("<p>This run predates per-role token accounting, so tokens are not shown. Sessions "
            "and cost come from the driver's events; cost is the harness's own estimate at list "
            f"prices, not a bill. Total recorded: ${sum(cost.values()):,.2f}.</p>"
            "<div class=\"table\"><table><thead><tr><th>Role</th><th>Sessions launched</th>"
            f"<th>Cost estimate</th></tr></thead><tbody>{table}</tbody></table></div>"
            + _models_line(models))


def _models_line(models: dict[str, Any]) -> str:
    if not models:
        return ""
    parts = [f"{esc(r)}: {esc(m.get('model'))} at {esc(m.get('effort'))} effort"
             for r, m in sorted(models.items()) if isinstance(m, dict)]
    return f'<p class="small">Models set for each role: {"; ".join(parts)}.</p>'


# ---------------------------------------------------------------------- page
def status_html(run: Run) -> str:
    r = run.release
    d = run.deliverable
    if r.state == "released":
        return (f'<div class="banner ok"><strong>Released</strong> (draft v{r.version}). '
                f"{esc(r.detail)}. To check that nothing changed since, run verify (below)."
                f"</div>")
    if d.kind == "none":
        if driver_outcome(run)[0] == "frame_complete" or run.task.get("stop_after") == "frame":
            return ('<div class="banner warn"><strong>A frame-only run.</strong> It stopped '
                    "once the frame, the tree and the answer to the frame critique existed, "
                    "as asked; there is no drafted answer. The frame is shown instead.</div>")
        return f'<div class="banner bad">This run produced no draft. {esc(r.detail)}.</div>'
    what = (f"the latest draft submitted for review, v{d.version}" if d.kind == "submitted_draft"
            else f"the latest working draft, v{d.version}, never submitted for review")
    head = {"withdrawn": "Release withdrawn.", "mismatch": "Release record does not match.",
            "not_released": "Not released."}[r.state]
    unmet: list[str] = []
    if d.kind == "submitted_draft" and d.version is not None and run.snapshot is not None:
        try:
            unmet = list(run.snapshot.register.finish_failures(d.version))
        except Exception as exc:  # noqa: BLE001 - shown on the page, never hidden
            unmet = [f"the gate could not be read ({type(exc).__name__})"]
    items = "".join(f"<li>{esc(u)}</li>" for u in unmet)
    gate = (f"<details><summary>The release gate lists {len(unmet)} unmet condition"
            f"{'s' if len(unmet) != 1 else ''}</summary><ul>{items}</ul></details>"
            if unmet else "")
    return (f'<div class="banner {"bad" if r.state != "not_released" else "warn"}"><strong>'
            f"{head}</strong> {esc(r.detail)}. Shown below is {esc(what)}.{gate}</div>")


CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--mute:#5d5b55;--line:#dedad0;--card:#fff;--accent:#1f5f8b;
--ok:#1e6b3a;--okbg:#e4f2e8;--warn:#8a5a00;--warnbg:#fbf0d9;--bad:#a02a1f;--badbg:#f8e3e0;
--mark:#ffe58a;--code:#f1eee6}
@media (prefers-color-scheme:dark){:root{--bg:#141517;--fg:#e8e6e1;--mute:#a19e96;
--line:#33353a;--card:#1c1d20;--accent:#7cb8e4;--ok:#7fd29a;--okbg:#17301f;--warn:#e8c06a;
--warnbg:#33290f;--bad:#f09a8f;--badbg:#3a1a17;--mark:#6b5a10;--code:#25272b}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:17px/1.55 -apple-system,
BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:780px;margin:0 auto;padding:16px 16px 80px;overflow-wrap:anywhere}
header.top{border-bottom:1px solid var(--line);padding-bottom:12px;margin-bottom:8px}
.recorded{display:inline-block;font:600 12px/1 ui-monospace,Menlo,monospace;letter-spacing:.08em;
border:1.5px solid var(--fg);padding:5px 7px;border-radius:3px}
.small,.meta,.loc{font-size:14px;color:var(--mute)}
h1{font-size:26px;line-height:1.25;margin:.6em 0 .3em}h2{font-size:21px;margin:2.2em 0 .5em;
padding-top:.6em;border-top:1px solid var(--line)}h3{font-size:18px;margin:1.6em 0 .4em}
h4,h5,h6{font-size:16px}
nav.toc{font-size:15px;margin:8px 0 0}nav.toc a{margin-right:14px;white-space:nowrap}
a{color:var(--accent)}code{font:14px ui-monospace,Menlo,monospace;background:var(--code);
padding:1px 4px;border-radius:3px;word-break:break-all}
pre{background:var(--code);padding:12px;border-radius:6px;overflow-x:auto;font:13px/1.5
ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-word}
pre.code{white-space:pre;word-break:normal}
mark{background:var(--mark);color:inherit;padding:0 1px}
.table{overflow-x:auto}table{border-collapse:collapse;font-size:15px;margin:.6em 0}
th,td{border-bottom:1px solid var(--line);padding:6px 10px 6px 0;text-align:left;
vertical-align:top}
.chip{display:inline-block;font:600 11px/1.6 ui-monospace,Menlo,monospace;padding:0 5px;
margin:0 1px;border-radius:9px;text-decoration:none;vertical-align:2px;white-space:nowrap}
.chip.ok{background:var(--okbg);color:var(--ok)}.chip.warn{background:var(--warnbg);
color:var(--warn)}.chip.bad,.chip.broken{background:var(--badbg);color:var(--bad)}
.banner{border-radius:8px;padding:12px 14px;margin:14px 0;font-size:15px}
.banner.ok{background:var(--okbg)}.banner.warn{background:var(--warnbg)}
.banner.bad{background:var(--badbg)}
.answer{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:4px 18px}
.answer h1{font-size:22px}.answer h2{border:0;font-size:19px;margin-top:1.4em}
.state{font-size:12px;font-weight:600;padding:1px 6px;border-radius:9px;background:var(--code);
white-space:nowrap}.s-answered,.s-gap_accepted,.s-bounded,.s-conditional{background:var(--okbg);
color:var(--ok)}.s-set_aside,.s-stopped_by_limit,.s-in_progress,.s-open,.s-waiting_user,
.s-proposed_done{background:var(--warnbg);color:var(--warn)}.s-bad{background:var(--badbg);
color:var(--bad)}
.kind{font-size:12px;color:var(--mute);border:1px solid var(--line);padding:0 5px;
border-radius:9px}
ul.tree{list-style:none;padding-left:18px;border-left:1px solid var(--line)}
main>section>ul.tree,.treewrap>ul.tree{padding-left:0;border:0}
ul.tree li{margin:.5em 0}.why{font-size:15px;color:var(--warn);margin-left:4px}
.said{font-size:15px}.said.ok{color:var(--ok)}.said.bad{color:var(--bad)}
.said.warn{color:var(--warn)}
details{margin:.4em 0}summary{cursor:pointer;color:var(--accent)}
.claim{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:2px 16px
12px;margin:14px 0}.claim h3{margin-top:.8em}.statement{font-size:17px}
.verdict.ok{color:var(--ok)}.verdict.warn{color:var(--warn)}.verdict.bad{color:var(--bad)}
.verdict{font-weight:600}.warn{color:var(--warn)}.passage{border-top:1px dashed var(--line);
margin-top:10px;padding-top:6px}.passage.missing{color:var(--bad)}
.hash{font-size:12px}
dialog{max-width:min(760px,96vw);width:96vw;max-height:88vh;border:1px solid var(--line);
border-radius:10px;padding:0;background:var(--bg);color:var(--fg)}
dialog::backdrop{background:rgba(0,0,0,.45)}dialog .bar{position:sticky;top:0;
background:var(--bg);padding:8px 12px;border-bottom:1px solid var(--line);text-align:right}
dialog .body{padding:0 12px 12px}dialog .claim{border:0;margin:0;padding:0}
button{font:inherit;background:var(--code);color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:4px 12px;cursor:pointer}
@media (max-width:520px){body{font-size:16px}h1{font-size:22px}
.answer{padding:2px 12px}}
"""

JS = """
(function(){var d=document.getElementById('claimdlg');if(!d||!d.showModal)return;
var body=d.querySelector('.body');
document.addEventListener('click',function(e){var a=e.target.closest('a[data-claim]');
if(!a)return;var s=document.getElementById('c-'+a.getAttribute('data-claim'));if(!s)return;
e.preventDefault();body.innerHTML='';var c=s.cloneNode(true);c.removeAttribute('id');
body.appendChild(c);if(!d.open)d.showModal();d.scrollTop=0;});
d.querySelector('button').addEventListener('click',function(){d.close();});
d.addEventListener('click',function(e){if(e.target===d)d.close();});})();
"""


def _csp_hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


# The page's only script and style are its own, allowed by hash; nothing else loads or
# runs: no other script, style, image, frame, font, form target or base URL.
CSP = ("default-src 'none'; " f"script-src {_csp_hash(JS)}; style-src {_csp_hash(CSS)}; "
       "img-src 'none'; base-uri 'none'; form-action 'none'")


def build(run_dir: Path, verify_result: dict[str, Any] | None = None) -> str:
    run = runread.load(run_dir)
    claims = Claims(run)
    d = run.deliverable
    date = _date(run.run.get("created_at"))
    if d.text:
        answer = md.render(d.text, claims.chip, heading_shift=0)
    else:
        frame = runread.read_text(run.dir, "frame.md")
        answer = (f"<h2>The frame</h2>{md.render(frame, claims.chip, heading_shift=1)}"
                  if frame else "<p>No text.</p>")
    toc = ('<nav class="toc"><a href="#answer">Answer</a><a href="#criteria">Criteria</a>'
           '<a href="#tree">Tree</a><a href="#checked">How it was checked</a>'
           '<a href="#facts">Run facts</a><a href="#verify">Verify</a>'
           '<a href="#claims">Every figure</a></nav>')
    verify_block = ("<p>Every figure below can be re-checked offline, with no keys: the "
                    "captured bytes re-hashed, every quotation found again in the stored "
                    "text, every calculation re-run.</p><pre>python run.py verify "
                    "&lt;run folder&gt;</pre>")
    if verify_result:
        lines = "".join(f"<li>{esc(s['label'])}: {esc(s['summary'])}</li>"
                        for s in verify_result.get("sections", []))
        verify_block += (f"<p>Result when this page was built: <strong>"
                         f"{esc(verify_result.get('outcome'))}</strong></p><ul>{lines}</ul>")
        fails = [f for s in verify_result.get("sections", []) for f in s.get("failures", [])]
        if fails:
            verify_block += ("<details><summary>What did not reproduce</summary><ul>"
                             + "".join(f"<li>{esc(f)}</li>" for f in fails) + "</ul></details>")
    scope = run.task.get("scope")
    scope_line = {"question_only": "Scope: only this question.",
                  "broader_decision": "Scope: this question and the broader decision it "
                                      "informs."}.get(str(scope), "")
    claims_html = "\n".join(claim_html(run, claims, cid) for cid in claims.shown)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta http-equiv="Content-Security-Policy" content="{CSP}">
<title>Stemma: {esc(run.question[:80])}</title>
<style>{CSS}</style></head><body><main>
<header class="top"><span class="recorded">RECORDED · run {esc(run.run_id)} · {esc(date)}</span>
<h1>{esc(run.question)}</h1>
<p class="small">A recorded research run of Stemma, shown from its run folder: nothing on this page
is live. Whether the folder still matches what was checked is what verify (below) tests.
{esc(scope_line)}</p>{task_html(run)}{toc}</header>
{waiting_html(run)}{status_html(run)}
<section id="answer"><h2>The answer</h2>
<p class="small">Each numbered tag after a figure opens the claim behind it: what the source
says, the exact stored passage, and the checker's verdict.</p>
<div class="answer">{answer}</div></section>
<section id="criteria"><h2>What "done" means</h2>{criteria_html(run)}</section>
<section id="tree"><h2>The question, broken down</h2>
<p class="small">The issue tree: the sub-questions the answer rests on, and each one's state
when the run stopped.</p><div class="treewrap">{tree_html(run, claims)}</div></section>
<section id="checked"><h2>How this was checked</h2>{record_html(run, claims)}
{checked_html(run, claims)}</section>
<section id="facts"><h2>Run facts</h2>{facts_html(run)}</section>
<section id="verify"><h2>Verify it yourself</h2>{verify_block}</section>
<section id="claims"><h2>Every figure in the answer</h2>
<p class="small">{len(claims.cited)} cited claims, and {len(claims.shown) - len(claims.cited)}
more that their calculations use.</p>{claims_html}</section>
<p class="small">Generated by <code>stemma report</code> from the run folder.</p>
</main>
<dialog id="claimdlg" aria-label="Claim"><div class="bar"><button type="button">Close</button>
</div><div class="body"></div></dialog>
<script>{JS}</script></body></html>
"""


def write(run_dir: Path, out_dir: Path, with_verify: bool = False) -> Path:
    result = None
    if with_verify:
        from stemma.report.verify import verify

        result = verify(run_dir).as_json()
    page = build(run_dir, result)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(page, encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")  # GitHub Pages: serve as is
    return out_dir / "index.html"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stemma report", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--verify", action="store_true",
                    help="also run stemma verify and show its result on the page")
    args = ap.parse_args(argv)
    try:
        path = write(args.run_dir, args.out_dir, args.verify)
    except runread.RunReadError as exc:
        print(f"stemma report: cannot read the run: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {path} ({path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["build", "json", "main", "tags", "write"]
