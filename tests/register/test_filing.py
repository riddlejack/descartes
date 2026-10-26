"""Code checks at filing, the derivation re-run sandbox, tags and stemma.analysis."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from stemma.register import tags
from stemma.register.derivation import rerun
from stemma.register.evidence_port import QuoteCheckFailed, stub_check_quote
from stemma.register.filing import Refused, claim_id_of, parse_warning, run_checks

TEXT = "Acme Widgets reported revenue of 1.2bn in fiscal 2025, up from 1.0bn.\nMargins held."


class FakeEvidence:
    def __init__(self, texts: dict[str, str]) -> None:
        self.texts = texts

    def check_quote(self, run_dir: Path, receipt_id: str, quote: str) -> bool:
        if receipt_id not in self.texts:
            raise QuoteCheckFailed(f"unknown receipt {receipt_id}")
        return " ".join(quote.split()) in " ".join(self.texts[receipt_id].split())

    def read_text(self, run_dir: Path, receipt_id: str) -> str:
        return self.texts[receipt_id]


EV = FakeEvidence({"r1": TEXT})


def no_rerun(script: Path, values: dict[str, float]) -> Any:
    raise AssertionError("no re-run expected")


def no_lookup(i: str) -> tuple[str, float]:
    raise AssertionError("no lookup expected")


def observed(**over: Any) -> dict[str, Any]:
    c: dict[str, Any] = {
        "statement": "Acme Widgets' fiscal 2025 revenue was 1.2bn.",
        "value": {"number": 1.2e9, "unit": "USD", "period": "FY2025", "kind": "level",
                  "as_written": "1.2bn"},
        "tier": "observed",
        "support": [{"receipt_id": "r1", "quote": "reported revenue of 1.2bn in fiscal 2025"}],
        "branch_id": "b1",
        "entities": ["Acme Widgets"],
    }
    c.update(over)
    return c


def check(raw: dict[str, Any], tmp_path: Path, rerun_fn: Any = no_rerun,
          lookup: Any = no_lookup) -> Any:
    return run_checks(raw, tmp_path, EV, rerun_fn, lookup)


def test_observed_claim_passes(tmp_path: Path) -> None:
    r = check(observed(), tmp_path)
    assert [c["check"] for c in r.checks] == ["quote_found:1", "as_written_in_quote"]
    assert r.warnings == []


def test_claim_id_is_content_derived(tmp_path: Path) -> None:
    a = check(observed(), tmp_path).claim
    b = check(dict(reversed(list(observed().items()))), tmp_path).claim
    assert claim_id_of(a) == claim_id_of(b)
    assert claim_id_of(a) != claim_id_of(check(observed(branch_id="b2"), tmp_path).claim)


@pytest.mark.parametrize("over, failed", [
    ({"support": []}, "needs at least one support item"),
    ({"support": [{"receipt_id": "r1", "quote": "revenue of 1.3bn"}]}, "quotation is not in"),
    ({"support": [{"receipt_id": "r9", "quote": "revenue"}]}, "unknown receipt r9"),
    ({"value": {"number": 1.2e9, "kind": "level", "as_written": "1.25bn"}}, "as_written"),
    ({"value": {"number": 1.2e9, "kind": "level", "as_written": ""}}, "as_written is empty"),
    ({"value": {"number": 0.2, "kind": "rate", "as_written": "1.2bn"}}, "denominator"),
    ({"value": {"number": 0.2, "kind": "share", "as_written": "1.2bn", "denominator": " "}},
     "denominator"),
    ({"tier": "fact"}, "tier must be one of"),
    ({"tier": "model_knowledge"}, "tier must be one of"),
    ({"statement": ""}, "no statement"),
    ({"author": "stemma:claim-checker"}, "unknown claim field author"),
    ({"claim_id": "abc"}, "unknown claim field claim_id"),
    ({"value": {"number": float("nan"), "kind": "level", "as_written": "1.2bn"}}, "finite"),
    ({"value": {"number": True, "kind": "level", "as_written": "1.2bn"}}, "finite"),
    ({"value": {"number": 1, "kind": "guess", "as_written": "1.2bn"}}, "value.kind"),
    ({"support": [{"receipt_id": "r1", "quote": "revenue", "verified": True}]}, "nothing else"),
])
def test_hostile_claims_refused(tmp_path: Path, over: dict[str, Any], failed: str) -> None:
    with pytest.raises(Refused, match=failed):
        check(observed(**over), tmp_path)


def test_as_written_must_sit_inside_a_quotation_not_just_the_source(tmp_path: Path) -> None:
    # "1.0bn" is in the source text but not in the quotation the claim rests on.
    raw = observed(value={"number": 1.0e9, "kind": "level", "as_written": "1.0bn"})
    with pytest.raises(Refused, match="does not appear inside any quotation"):
        check(raw, tmp_path)


def test_parse_mismatch_is_a_warning_not_a_refusal(tmp_path: Path) -> None:
    raw = observed(value={"number": 1.2e6, "kind": "level", "as_written": "1.2bn"})
    r = check(raw, tmp_path)
    assert r.warnings and r.warnings[0].startswith("parse warning")


@pytest.mark.parametrize("value, ok", [
    ({"number": 1.2e9, "as_written": "1.2bn"}, True),
    ({"number": 0.125, "as_written": "12.5%"}, True),
    ({"number": 12.5, "as_written": "12.5%"}, True),
    ({"number": 3400, "as_written": "3,400"}, True),
    ({"number": -2.0, "as_written": "-2"}, True),
    ({"number": 5, "as_written": "seven"}, False),
])
def test_parse_warning(value: dict[str, Any], ok: bool) -> None:
    assert (parse_warning(value) is None) is ok


def test_assumed_needs_no_support(tmp_path: Path) -> None:
    raw = observed(tier="assumed", support=[],
                   statement="We assume 3% growth because the plan says so.",
                   value={"number": 0.03, "kind": "change", "as_written": "3%"})
    assert check(raw, tmp_path).checks == []


def write_script(tmp_path: Path, body: str) -> str:
    (tmp_path / "analysis").mkdir(exist_ok=True)
    (tmp_path / "analysis" / "calc.py").write_text(body)
    return "analysis/calc.py"


RATIO = "from stemma.analysis import inputs, result\nv = inputs()\nresult(v['" + "a" * 64 + \
    "'] / v['" + "b" * 64 + "'])\n"


def lookup_ab(i: str) -> tuple[str, float]:
    return {"a" * 12: ("a" * 64, 3.0), "b" * 12: ("b" * 64, 4.0)}[i]


def estimated(script: str, number: float) -> dict[str, Any]:
    return observed(tier="estimated", support=[], statement="Ratio of a to b is 0.75.",
                    value={"number": number, "kind": "ratio", "denominator": "b",
                           "as_written": "0.75"},
                    derivation={"script": script, "inputs": ["a" * 12, "b" * 12]})


def test_estimated_rerun_reproduces(tmp_path: Path) -> None:
    script = write_script(tmp_path, RATIO)
    r = check(estimated(script, 0.75), tmp_path, rerun_fn=rerun, lookup=lookup_ab)
    assert any(c["check"] == "derivation_reproduces" for c in r.checks)


def test_estimated_rerun_mismatch_refused(tmp_path: Path) -> None:
    script = write_script(tmp_path, RATIO)
    with pytest.raises(Refused, match="gives 0.75, not 0.8"):
        check(estimated(script, 0.8), tmp_path, rerun_fn=rerun, lookup=lookup_ab)


def test_estimated_without_result_refused(tmp_path: Path) -> None:
    script = write_script(tmp_path, "print('no result')\n")
    with pytest.raises(Refused, match="reported no result"):
        check(estimated(script, 0.75), tmp_path, rerun_fn=rerun, lookup=lookup_ab)


def test_estimated_script_outside_analysis_refused(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text(RATIO)
    with pytest.raises(Refused, match="not in analysis/"):
        check(estimated("analysis/../x.py", 0.75), tmp_path, rerun_fn=rerun, lookup=lookup_ab)


def test_rerun_sandbox_blocks_network_and_outside_writes(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    script = tmp_path / "analysis" / "evil.py"
    script.parent.mkdir()
    script.write_text(
        "import socket, pathlib, sys\nfrom stemma.analysis import result\nout=[]\n"
        "try:\n    socket.create_connection(('1.1.1.1', 80), timeout=3); out.append('net')\n"
        "except OSError: pass\n"
        f"try:\n    pathlib.Path({str(victim)!r}).write_text('x'); out.append('write')\n"
        "except OSError: pass\n"
        "pathlib.Path('scratch.txt').write_text('fine')\n"
        "result(len(out))\n"
    )
    r = rerun(script, {})
    assert r.number == 0.0  # neither the connection nor the outside write succeeded
    assert not victim.exists()


def test_rerun_time_limit(tmp_path: Path) -> None:
    script = tmp_path / "analysis" / "slow.py"
    script.parent.mkdir()
    script.write_text("import time\ntime.sleep(30)\n")
    from stemma.register.derivation import DerivationError
    with pytest.raises(DerivationError, match="limit"):
        rerun(script, {}, time_limit_s=2)


def test_analysis_inputs_only_named_and_one_result(tmp_path: Path) -> None:
    inputs = tmp_path / "in.json"
    inputs.write_text(json.dumps({"a" * 64: 2.0}))
    out = tmp_path / "out.json"
    code = ("from stemma.analysis import inputs, result, AnalysisError\n"
            "v = inputs('aaaaaaaaaaaa')\n"
            "try:\n    inputs('cccccccccccc')\nexcept AnalysisError: pass\n"
            "else:\n    raise SystemExit('unknown input allowed')\n"
            "result(v['aaaaaaaaaaaa'] * 2)\n"
            "try:\n    result(1)\nexcept AnalysisError: pass\n"
            "else:\n    raise SystemExit('second result allowed')\n")
    env = {"PATH": os.environ["PATH"], "STEMMA_ANALYSIS_INPUTS": str(inputs),
           "STEMMA_ANALYSIS_RESULT": str(out)}
    p = subprocess.run([sys.executable, "-I", "-c", code], env=env, capture_output=True,
                       text=True, check=False)
    assert p.returncode == 0, p.stderr
    assert json.loads(out.read_text()) == {"number": 4.0}


def test_stub_evidence_reads_contract_layout(tmp_path: Path) -> None:
    (tmp_path / "store/receipts").mkdir(parents=True)
    (tmp_path / "store/text").mkdir(parents=True)
    (tmp_path / "store/receipts/r1.json").write_text(json.dumps({"sha256": "f" * 64}))
    (tmp_path / "store/text" / ("f" * 64 + ".txt")).write_text("a  b\nc")
    assert stub_check_quote(tmp_path, "r1", "a b c")["found"] is True
    assert stub_check_quote(tmp_path, "r1", "a c")["found"] is False
    for bad in ("../r1", "r2"):
        with pytest.raises(QuoteCheckFailed):
            stub_check_quote(tmp_path, bad, "a")


# --- Tags -----------------------------------------------------------------------------

DOC = """# Revenue grew 20% [[c:0123456789ab]]

Acme sold 3,400 units [[c:aaaaaaaaaaaa]]. The plan dates from 2024 [[nc:date]].
Untagged sentence with 15 widgets. Plain sentence without numbers.

- A list item with 7 items
- Tagged item 8 [[c:bbbbbbbbbbbb]]

| Year | Units |
|------|-------|
| 2025 [[nc:label]] | 3,400 [[c:aaaaaaaaaaaa]] |
| 2026 | 5 |
"""


def test_untagged_number_segments() -> None:
    bad = [s.text for s in tags.untagged_number_segments(DOC)]
    assert bad == ["Untagged sentence with 15 widgets.", "A list item with 7 items",
                   "2026", "5"]


def test_tag_digits_are_not_number_tokens() -> None:
    assert tags.segments("See [[c:0123456789ab]] above.")[0].number_tokens == []


def test_claim_tags_found() -> None:
    assert tags.claim_tags(DOC) == ["0123456789ab", "aaaaaaaaaaaa", "bbbbbbbbbbbb",
                                    "aaaaaaaaaaaa"]
