"""The head-to-head scorer of run 3's pre-registration (hypothesis H5).

Both answers are counted by the same rules:

- **Figures stated.** Every number token (the register's own `Segment.number_tokens`) in
  every segment (sentence, list item, table cell, heading), after removing claim tags,
  link targets, bare URLs and bracketed reference markers. A plain four-digit year from
  1900 to 2100 is not counted as a figure (a logical rule, applied to both answers).
- **A figure's cited sources.** The sources cited in its own segment: for Stemma, the
  pages behind the claims its tags name (a claim's own sources, or for an estimate the
  sources of its inputs); for the other answer, the links and the references its
  markers point to.
- **Citations that resolve.** Every distinct cited page is fetched now, through Stemma's
  evidence service, as a capture whose bytes and text are kept; it resolves when the
  capture holds text.
- **Figures a cited source contains.** Code: the figure as written, or its bare numeral,
  is found in the stored text of one of its resolved sources by part A's `find_quote`
  (which never matches inside a longer number).
- **Figures a cited source contradicts.** Judged, for each figure a resolved source does
  not contain, by a cross-family judge (Codex, `gpt-6-astra`, read-only) given the
  stored text. Counted only when the judge quotes the passage that contradicts it and
  code finds that quote in the stored text.
- **Gaps and limits admitted.** Judged by the same judge from the answer alone, as exact
  quotations, counted only when code finds each in the answer.

Every judge prompt and reply is recorded in `score.json`, with every figure, citation
and capture. `summary.txt` says the counts in sentences, for the owner.

    python -m hackathon.headtohead.score --stemma ANSWER.md --stemma-run RUN \\
        --bare BARE.md --out DIR [--judge codex|none]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

from stemma.evidence.errors import EvidenceError
from stemma.evidence.quotes import find_quote
from stemma.register import tags

JUDGE_MODEL: Final = "gpt-6-astra"
# Resource limits: seconds one judge call may take; judge calls in one scoring;
# characters of an answer shown to the judge; depth of estimate inputs followed.
JUDGE_TIMEOUT_S: Final = 900
MAX_JUDGE_CALLS: Final = 200
MAX_ANSWER_CHARS: Final = 200_000
MAX_INPUT_DEPTH: Final = 8
_YEAR = re.compile(r"(?:19|20)\d\d|2100")
_URL = re.compile(r"https?://[^\s<>()\"'`\]]+")
_LINK = re.compile(r"\[([^\]\n]*)\]\((https?://[^\s)]+)\)")
_REF_MARK = re.compile(r"\[\^?(\d{1,3})\]")
_REF_DEF = re.compile(r"^\s*(?:\[\^?(\d{1,3})\]:?|(\d{1,3})[.)])\s+(.*)$")


class ScoreError(Exception):
    """An answer or a run cannot be scored; the message says why."""


# ------------------------------------------------------------------- figures
@dataclass
class Figure:
    arm: str
    line: int
    segment: str
    token: str
    sources: list[str] = field(default_factory=list)
    calculated: bool = False  # its segment cites a claim backed by a recorded calculation
    contained_in: str | None = None
    contradicted_by: str | None = None
    judge_quote: str | None = None


def _clean(text: str) -> str:
    """The segment without claim tags, link targets, URLs and reference markers."""
    text = tags.ANY_TAG.sub(" ", text)
    text = _LINK.sub(lambda m: m.group(1), text)
    text = _URL.sub(" ", text)
    return _REF_MARK.sub(" ", text)


def figures_in(markdown: str, arm: str) -> list[tuple[tags.Segment, list[str]]]:
    """Each segment with the figures it states (the same rule for every answer)."""
    out = []
    for seg in tags.segments(markdown):
        tokens = [t for t in tags.Segment(seg.line, seg.kind, _clean(seg.text)).number_tokens
                  if not _YEAR.fullmatch(t.strip())]
        if tokens:
            out.append((seg, tokens))
    return out


# ----------------------------------------------------------------- citations
def bare_references(markdown: str) -> dict[str, list[str]]:
    """Reference markers ([1], [^1]) and the URLs their definitions give."""
    refs: dict[str, list[str]] = {}
    for line in markdown.splitlines():
        m = _REF_DEF.match(line)
        if m:
            urls = _URL.findall(m.group(3))
            if urls:
                refs.setdefault(m.group(1) or m.group(2), []).extend(urls)
    return refs


def bare_sources(segment: str, refs: dict[str, list[str]]) -> list[str]:
    urls = [m.group(2) for m in _LINK.finditer(segment)] + _URL.findall(segment)
    for m in _REF_MARK.finditer(segment):
        urls += refs.get(m.group(1), [])
    return list(dict.fromkeys(u.rstrip(".,;:") for u in urls))


def stemma_calculated(segment: tags.Segment, run: Any) -> bool:
    """Whether a segment's tags name a claim backed by a recorded calculation (an
    estimate with a derivation), which no cited page can contain by design."""
    for short in segment.claim_tags:
        c = run.claim(short)
        if c is not None and c.get("tier") == "estimated" and c.get("derivation"):
            return True
    return False


def stemma_sources(segment: tags.Segment, run: Any) -> list[str]:
    """The pages behind the claims a segment's tags name: each claim's own support, or,
    for an estimate, its inputs' (followed to a recorded depth)."""
    from stemma.report import runread

    urls: list[str] = []

    def add(claim: dict[str, Any] | None, depth: int) -> None:
        if claim is None or depth > MAX_INPUT_DEPTH:
            return
        for s in claim.get("support") or []:
            rec = runread.receipt(run.dir, s.get("receipt_id"))
            url = (rec or {}).get("final_url") or (rec or {}).get("requested_url")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                urls.append(url)
        for given in (claim.get("derivation") or {}).get("inputs") or []:
            add(run.claim(str(given)), depth + 1)

    for short in segment.claim_tags:
        add(run.claim(short), 0)
    return list(dict.fromkeys(urls))


# ------------------------------------------------------------------ captures
@dataclass
class Captured:
    url: str
    status: str
    receipt_id: str | None = None
    sha256: str | None = None
    text_path: str | None = None
    text: str | None = field(default=None, repr=False)

    @property
    def resolves(self) -> bool:
        return self.text is not None


Capture = Callable[[str], Captured]


def evidence_capture(folder: Path) -> Capture:
    """Each URL fetched now (no cache) by Stemma's evidence service into `folder`, a
    capture whose bytes, text and receipt are kept."""
    from stemma.evidence.service import EvidenceService
    from stemma.report import runread

    folder.mkdir(parents=True, exist_ok=True)
    svc = EvidenceService(folder, cache_dir=None)

    def capture(url: str) -> Captured:
        try:
            rec = svc.capture(url, "head-to-head scorer: the cited source, fetched now")
        except EvidenceError as exc:
            return Captured(url, f"refused: {exc.code}")
        text = runread.stored_text(folder, rec) if rec.get("text_id") else None
        return Captured(url, str(rec.get("status")), rec.get("receipt_id"), rec.get("sha256"),
                        rec.get("text_path"), text)

    return capture


def contains(text: str, token: str) -> bool:
    """The figure as written, or its bare numeral, in the text (never inside a longer
    number)."""
    bare = re.sub(r"^[+\-−]?[$£€¥]?|(?:%|\s?(?:bn|mn|m|k|tn|trn|x))$", "", token.strip())
    for form in dict.fromkeys([token.strip(), bare]):
        if form and any(ch.isdigit() for ch in form):
            try:
                if find_quote(text, form)["found"]:
                    return True
            except EvidenceError:
                continue
    return False


# --------------------------------------------------------------------- judge
Judge = Callable[[str, Path], str]  # (prompt, folder it may read) -> its reply


def codex_judge(model: str = JUDGE_MODEL) -> Judge:
    """Codex, read-only, the given model; its last message is the reply."""

    def judge(prompt: str, folder: Path) -> str:
        with tempfile.TemporaryDirectory(prefix="h2h-judge-") as td:
            out = Path(td) / "reply.txt"
            done = subprocess.run(
                ["codex", "exec", "-m", model, "-s", "read-only", "--skip-git-repo-check",
                 "--ephemeral", "-C", str(folder), "-o", str(out), prompt],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=JUDGE_TIMEOUT_S, check=False)
            if done.returncode != 0 or not out.exists():
                raise ScoreError(f"the judge failed (exit {done.returncode}): "
                                 f"{done.stderr.strip()[-400:]}")
            return out.read_text(encoding="utf-8")

    return judge


CONTRADICT_PROMPT: Final = """\
You are checking figures in a research answer against one source document. Judge only
from the document. Do not use outside knowledge, and do not browse.

The source document's stored text is the file `{text_file}` in your working folder.
Its address: {url}

For each figure below, the sentence it appears in is given. The document does not
contain the figure as written. Decide whether the document contradicts it: it states,
about the same thing, a different value that cannot be reconciled with the figure (a
different year, unit, scope or basis is not a contradiction; say "not_addressed").
The document was fetched today and may be newer than the answer: a quantity that
changes over time (a running count, a price, a total to date) that differs only because
the document is newer is not a contradiction either; say "not_addressed".

{items}

Reply with only a JSON list, one object per figure, in this shape:
[{{"id": <the figure's id>, "verdict": "contradicts" | "consistent" | "not_addressed",
  "quote": "<for contradicts: the exact words of the document that contradict it,
  copied character for character; otherwise an empty string>"}}]
"""

GAPS_PROMPT: Final = """\
Read the research answer in the file `{answer_file}` in your working folder.

List every place where the answer admits a gap or a limit of its own: something it did
not research, could not find, did not check, could not verify, or is uncertain about,
or a caveat on its own figures. Do not list limits of the world it describes, only of
the answer itself.

Reply with only a JSON list of strings, each an exact quotation from the answer (one
sentence or list item, copied character for character), one per admitted gap or limit.
"""


def _json_reply(reply: str) -> Any:
    s = reply.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        s = s.rsplit("```", 1)[0]
    start = min((i for i in (s.find("["), s.find("{")) if i >= 0), default=-1)
    if start < 0:
        raise ScoreError("the judge's reply holds no JSON")
    try:
        return json.loads(s[start:])
    except json.JSONDecodeError as exc:
        raise ScoreError(f"the judge's reply is not valid JSON: {exc}") from exc


# --------------------------------------------------------------------- score
@dataclass
class ArmScore:
    arm: str
    answer: str
    figures: list[Figure] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    gaps_rejected: list[str] = field(default_factory=list)

    def counts(self, captures: dict[str, Captured]) -> dict[str, int]:
        cited = {u for f in self.figures for u in f.sources}
        return {
            "figures_stated": len(self.figures),
            "figures_backed_by_a_recorded_calculation": sum(1 for f in self.figures
                                                            if f.calculated),
            "figures_with_a_cited_source": sum(1 for f in self.figures if f.sources),
            "figures_a_cited_source_contains": sum(1 for f in self.figures
                                                   if f.contained_in),
            "figures_a_cited_source_contradicts": sum(1 for f in self.figures
                                                      if f.contradicted_by),
            "citations": len(cited),
            "citations_that_resolve": sum(1 for u in cited if captures[u].resolves),
            "gaps_and_limits_admitted": len(self.gaps),
        }


@dataclass
class Scoring:
    arms: list[ArmScore]
    captures: dict[str, Captured]
    judge_calls: list[dict[str, Any]] = field(default_factory=list)
    rules: dict[str, str] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "rules": self.rules,
            "counts": {a.arm: a.counts(self.captures) for a in self.arms},
            "figures": [asdict(f) for a in self.arms for f in a.figures],
            "gaps": {a.arm: {"counted": a.gaps, "quote_not_found": a.gaps_rejected}
                     for a in self.arms},
            "captures": {u: {k: v for k, v in asdict(c).items() if k != "text"}
                         for u, c in sorted(self.captures.items())},
            "judge_calls": self.judge_calls,
        }


RULES: Final = {
    "figure": "every number token (stemma.register.tags) in a segment, after removing tags, "
              "links, URLs and reference markers; a plain year 1900-2100 is not a figure",
    "cited_sources": "the sources cited in the figure's own segment",
    "resolves": "fetched now by Stemma's evidence service; the capture holds text",
    "contains": "the figure as written or its bare numeral, found by find_quote",
    "contradicts": "judged by the cross-family judge; counted only when its quote is "
                   "found in the stored text",
    "gaps": "judged by the cross-family judge; counted only when its quote is found in "
            "the answer",
}


class _Calls:
    def __init__(self, judge: Judge | None, log: list[dict[str, Any]]) -> None:
        self.judge, self.log, self.n = judge, log, 0

    def ask(self, prompt: str, folder: Path, about: str) -> Any:
        if self.judge is None:
            return None
        if self.n >= MAX_JUDGE_CALLS:
            raise ScoreError(f"more than {MAX_JUDGE_CALLS} judge calls (a resource limit)")
        self.n += 1
        entry: dict[str, Any] = {"about": about, "model": JUDGE_MODEL, "prompt": prompt}
        self.log.append(entry)
        reply = self.judge(prompt, folder)
        entry["reply"] = reply
        return _json_reply(reply)


Answer = tuple[str, str, Callable[[tags.Segment], list[str]]] | tuple[
    str, str, Callable[[tags.Segment], list[str]], Callable[[tags.Segment], bool]]


def score(answers: list[Answer], capture: Capture, judge: Judge | None,
          work: Path) -> Scoring:
    """`answers`: (arm, markdown, the sources a segment cites[, whether a segment cites a
    claim backed by a recorded calculation]) for each answer. The last is informational,
    outside the shared comparison: only Stemma records calculations."""
    log: list[dict[str, Any]] = []
    calls = _Calls(judge, log)
    captures: dict[str, Captured] = {}
    arms = []
    for answer in answers:
        arm, markdown, sources_of = answer[0], answer[1], answer[2]
        calculated_in = answer[3] if len(answer) > 3 else None
        if len(markdown) > MAX_ANSWER_CHARS:
            raise ScoreError(f"the {arm} answer is longer than {MAX_ANSWER_CHARS} characters")
        a = ArmScore(arm, markdown)
        for seg, tokens in figures_in(markdown, arm):
            srcs = sources_of(seg)
            calc = bool(calculated_in(seg)) if calculated_in is not None else False
            for t in tokens:
                a.figures.append(Figure(arm, seg.line, seg.text, t, srcs, calc))
        for u in dict.fromkeys(u for f in a.figures for u in f.sources):
            if u not in captures:
                captures[u] = capture(u)
        for f in a.figures:
            f.contained_in = next((u for u in f.sources if captures[u].resolves
                                   and contains(captures[u].text or "", f.token)), None)
        arms.append(a)
    texts = work / "texts"
    texts.mkdir(parents=True, exist_ok=True)
    for a in arms:
        _judge_contradictions(a, captures, calls, texts)
        _judge_gaps(a, calls, texts)
    return Scoring(arms, captures, log, dict(RULES))


def _judge_contradictions(a: ArmScore, captures: dict[str, Captured], calls: _Calls,
                          texts: Path) -> None:
    by_source: dict[str, list[tuple[int, Figure]]] = {}
    for i, f in enumerate(a.figures):
        if f.contained_in is None:
            for u in f.sources:
                if captures[u].resolves:
                    by_source.setdefault(u, []).append((i, f))
    for n, (url, items) in enumerate(sorted(by_source.items())):
        name = f"{a.arm}-source-{n}.txt"
        (texts / name).write_text(captures[url].text or "", encoding="utf-8")
        listing = "\n".join(f"- id {i}: figure {f.token!r} in: {f.segment!r}"
                            for i, f in items)
        reply = calls.ask(CONTRADICT_PROMPT.format(text_file=name, url=url, items=listing),
                          texts, f"{a.arm}: contradictions in {url}")
        if not isinstance(reply, list):
            continue
        for v in reply:
            if not isinstance(v, dict) or v.get("verdict") != "contradicts":
                continue
            fid, quote = v.get("id"), v.get("quote")
            if (not isinstance(fid, int) or isinstance(fid, bool)
                    or fid not in {i for i, _f in items}
                    or not isinstance(quote, str) or not quote.strip()):
                continue  # only a figure asked about, with a quotation, can count
            i = fid
            try:
                found = find_quote(captures[url].text or "", quote)["found"]
            except EvidenceError:
                found = False
            if found and a.figures[i].contradicted_by is None:
                a.figures[i].contradicted_by = url
                a.figures[i].judge_quote = quote


def _judge_gaps(a: ArmScore, calls: _Calls, texts: Path) -> None:
    name = f"{a.arm}-answer.md"
    (texts / name).write_text(a.answer, encoding="utf-8")
    reply = calls.ask(GAPS_PROMPT.format(answer_file=name), texts, f"{a.arm}: gaps")
    if not isinstance(reply, list):
        return
    clean = tags.ANY_TAG.sub("", a.answer)
    for q in dict.fromkeys(q for q in reply if isinstance(q, str) and q.strip()):
        try:
            ok = find_quote(a.answer, q)["found"] or find_quote(clean, q)["found"]
        except EvidenceError:
            ok = False
        (a.gaps if ok else a.gaps_rejected).append(q)


# ------------------------------------------------------------------- summary
LABELS: Final = {
    "figures_stated": "Figures stated",
    "figures_with_a_cited_source": "Figures with a cited source",
    "figures_backed_by_a_recorded_calculation": ("Figures in a sentence whose cited claim "
                                                 "is a recorded calculation (Stemma only; "
                                                 "outside the shared comparison)"),
    "figures_a_cited_source_contains": "Figures a cited source contains, as fetched now",
    "figures_a_cited_source_contradicts": "Figures a cited source contradicts",
    "citations": "Distinct sources cited",
    "citations_that_resolve": "Cited sources that resolve now",
    "gaps_and_limits_admitted": "Gaps and limits the answer admits",
}


def summary(s: Scoring) -> str:
    counts = {a.arm: a.counts(s.captures) for a in s.arms}
    arms = [a.arm for a in s.arms]
    lines = ["Head-to-head, counted the same way for both answers.", ""]
    for key, label in LABELS.items():
        parts = []
        for arm in arms:
            c = counts[arm]
            share = ""
            if key == "figures_a_cited_source_contains" and c["figures_stated"]:
                share = f" ({100 * c[key] / c['figures_stated']:.0f}% of its figures)"
            if key == "citations_that_resolve" and c["citations"]:
                share = f" ({100 * c[key] / c['citations']:.0f}% of its sources)"
            parts.append(f"{arm} {c[key]:,}{share}")
        lines.append(f"{label}: " + "; ".join(parts) + ".")
    lines += ["", f"The judge ({JUDGE_MODEL}, read-only) was asked {len(s.judge_calls)} "
                  f"time(s); every prompt and reply is in score.json."
              if s.judge_calls else "", "No judge was used: contradictions and gaps are "
              "not counted." if not s.judge_calls else ""]
    return "\n".join(x for x in lines if x is not None).rstrip() + "\n"


# ------------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None, capture: Capture | None = None,
         judge: Judge | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hackathon.headtohead.score",
                                 description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stemma", type=Path, required=True, help="Stemma's answer (markdown)")
    ap.add_argument("--stemma-run", type=Path, required=True,
                    help="the run folder whose claims the answer's tags name")
    ap.add_argument("--bare", type=Path, required=True, help="the bare arm's answer")
    ap.add_argument("--out", type=Path, required=True, help="a new folder for the score")
    ap.add_argument("--judge", choices=("codex", "none"), default="codex")
    args = ap.parse_args(argv)
    from stemma.report import runread

    try:
        if args.out.exists():
            raise ScoreError(f"{args.out} exists; give a new folder")
        run = runread.load(args.stemma_run)
        stemma_md = args.stemma.read_text(encoding="utf-8")
        bare_md = args.bare.read_text(encoding="utf-8")
        refs = bare_references(bare_md)
        args.out.mkdir(parents=True)
        s = score([("stemma", stemma_md, lambda seg: stemma_sources(seg, run),
                    lambda seg: stemma_calculated(seg, run)),
                   ("bare", bare_md, lambda seg: bare_sources(seg.text, refs))],
                  capture or evidence_capture(args.out / "captures"),
                  judge if judge is not None else (
                      codex_judge() if args.judge == "codex" else None),
                  args.out)
    except (ScoreError, runread.RunReadError, OSError) as exc:
        print(f"score: {exc}", file=sys.stderr)
        return 2
    (args.out / "score.json").write_text(json.dumps(s.as_json(), indent=1,
                                                    ensure_ascii=False), encoding="utf-8")
    text = summary(s)
    (args.out / "summary.txt").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
