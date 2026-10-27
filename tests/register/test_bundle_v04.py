"""The re-run copies the script's bundle, not all of analysis/, and a filed claim re-runs
the copy stored at its filing. These run the real sandbox-exec re-run."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from test_filing import EV, RATIO, check, estimated, lookup_ab
from test_store import WORKER, FakeEvidence, claim

from stemma.register import derivation, filing
from stemma.register.derivation import (
    Bundle,
    DerivationError,
    build_bundle,
    load_bundle,
    rerun,
    store_bundle,
)
from stemma.register.filing import Refused, rerun_filed, run_checks
from stemma.register.store import Register


def no_run(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("the code must be refused before any re-run")


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


# --- A run holding more scripts than a bundle may ---------------------------------------

def test_an_estimate_files_when_analysis_holds_more_than_64_scripts(tmp_path: Path) -> None:
    # Run 1 held 223 scripts: every estimate was refused once a run held more than 64.
    for i in range(derivation.CODE_MAX_FILES + 6):
        write(tmp_path, {f"analysis/other_{i:03d}.py": f"x = {i}\n"})
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET = 1\n")
    (tmp_path / "analysis" / "zz_link.py").symlink_to(outside)  # never imported: never read
    write(tmp_path, {"analysis/helper.py": "def ratio(a, b):\n    return a / b\n",
                     "analysis/calc.py": (
                         "from helper import ratio\nfrom stemma.analysis import inputs, result\n"
                         "v = inputs()\nresult(ratio(v['" + "a" * 64 + "'], v['" + "b" * 64
                         + "']))\n")})
    r = check(estimated("analysis/calc.py", 0.75), tmp_path, rerun_fn=rerun, lookup=lookup_ab)
    done = next(c for c in r.checks if c["check"] == "derivation_reproduces")
    assert done["bundle_files"] == ["calc.py", "helper.py"]
    sha = done["bundle_sha256"]
    assert r.claim["derivation"]["bundle_sha256"] == sha
    stored = tmp_path / "store" / "derivations" / sha
    assert (stored / "files" / "helper.py").read_text().startswith("def ratio")
    assert sorted(p.name for p in (stored / "files").iterdir()) == ["calc.py", "helper.py"]


def test_the_limits_apply_to_the_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(tmp_path, {"analysis/calc.py": "import h0, h1\n" + RATIO,
                     "analysis/h0.py": "a = 1\n", "analysis/h1.py": "b = 2\n",
                     "analysis/unused.py": "c = 3\n"})
    monkeypatch.setattr(derivation, "CODE_MAX_FILES", 3)
    assert sorted(build_bundle(tmp_path, "analysis/calc.py").files) == [
        "calc.py", "h0.py", "h1.py"]
    monkeypatch.setattr(derivation, "CODE_MAX_FILES", 2)
    with pytest.raises(Refused, match="more than the 2 Python files a re-run copies"):
        check(estimated("analysis/calc.py", 0.75), tmp_path, rerun_fn=no_run,
              lookup=lookup_ab)


# --- Sibling modules, packages, transitively, under python -I ---------------------------

def test_sibling_and_package_imports_are_bundled_and_run_under_isolated_python(
        tmp_path: Path) -> None:
    write(tmp_path, {
        "analysis/calc.py": ("import json, statistics\nimport helper\n"
                             "from stemma.analysis import inputs, result\n"
                             "v = inputs()\nresult(helper.scaled(v['" + "a" * 64 + "'], v['"
                             + "b" * 64 + "']))\n"),
        "analysis/helper.py": "from pkg import util\ndef scaled(a, b):\n    return util.f(a, b)\n",
        "analysis/pkg/__init__.py": "",
        "analysis/pkg/util.py": "from . import consts\ndef f(a, b):\n    return a / b * consts.K\n",
        "analysis/pkg/consts.py": "K = 2\n",
        "analysis/pkg/unused.py": "raise SystemExit('never imported')\n",
    })
    b = build_bundle(tmp_path, "analysis/calc.py")
    # The standard library and stemma.analysis are not members.
    assert sorted(b.files) == ["calc.py", "helper.py", "pkg/__init__.py", "pkg/consts.py",
                               "pkg/util.py"]
    r = check(estimated("analysis/calc.py", 1.5), tmp_path, rerun_fn=rerun, lookup=lookup_ab)
    assert any(c["check"] == "derivation_reproduces" for c in r.checks)


def test_a_script_in_a_subfolder_bundles_from_its_own_folder(tmp_path: Path) -> None:
    write(tmp_path, {"analysis/sub/calc.py": "import near\nfrom stemma.analysis import result\n"
                                             "result(near.N)\n",
                     "analysis/sub/near.py": "N = 4\n",
                     "analysis/near.py": "N = 5\n"})
    b = build_bundle(tmp_path, "analysis/sub/calc.py")
    assert sorted(b.files) == ["calc.py", "near.py"]
    assert b.files["near.py"] == b"N = 4\n"
    assert rerun(b, {}).number == 4.0


# --- Imports that would reach outside analysis/ ---------------------------------------

def _outside(tmp_path: Path) -> Path:
    out = tmp_path / "outside"
    write(out, {"helper.py": "SECRET = 1\n", "util.py": "SECRET = 1\n",
                "pkg/__init__.py": "", "pkg/util.py": "SECRET = 1\n"})
    return out


@pytest.mark.parametrize("case", ["sibling_link", "package_link", "module_in_package_link",
                                  "relative_from_script", "relative_climbing_package",
                                  "init_link"])
def test_imports_reaching_outside_analysis_are_refused(tmp_path: Path, case: str) -> None:
    out = _outside(tmp_path)
    a = tmp_path / "analysis"
    a.mkdir()
    if case == "sibling_link":
        (a / "calc.py").write_text("import helper\n" + RATIO)
        (a / "helper.py").symlink_to(out / "helper.py")
        msg = "helper.py is a link"
    elif case == "package_link":
        (a / "calc.py").write_text("from pkg import util\n" + RATIO)
        (a / "pkg").symlink_to(out / "pkg")
        msg = "analysis/pkg is a link"
    elif case == "module_in_package_link":
        write(tmp_path, {"analysis/calc.py": "import pkg.util\n" + RATIO,
                         "analysis/pkg/__init__.py": ""})
        (a / "pkg" / "util.py").symlink_to(out / "util.py")
        msg = "pkg/util.py is a link"
    elif case == "relative_from_script":
        write(tmp_path, {"analysis/calc.py": "from . import helper\n" + RATIO,
                         "analysis/helper.py": "x = 1\n"})
        msg = "climbs above the script's folder"
    elif case == "relative_climbing_package":
        write(tmp_path, {"analysis/calc.py": "import pkg.mod\n" + RATIO,
                         "analysis/pkg/__init__.py": "",
                         "analysis/pkg/mod.py": "from .. import secret\n"})
        msg = "climbs above the script's folder"
    else:
        (a / "calc.py").write_text("import pkg\n" + RATIO)
        (a / "pkg").mkdir()
        (a / "pkg" / "__init__.py").symlink_to(out / "pkg" / "__init__.py")
        msg = "__init__.py is a link"
    with pytest.raises(DerivationError, match=msg):
        build_bundle(tmp_path, "analysis/calc.py")
    with pytest.raises(Refused, match=msg):
        check(estimated("analysis/calc.py", 0.75), tmp_path, rerun_fn=no_run,
              lookup=lookup_ab)


@pytest.mark.parametrize("given", ["Analysis/calc.py", "analysis/CALC.py", "analysis/./calc.py",
                                   "analysis//calc.py", "analysis/../analysis/calc.py",
                                   "/analysis/calc.py", "analysis/calc.py\x00"])
def test_script_paths_must_name_a_real_entry_exactly(tmp_path: Path, given: str) -> None:
    write(tmp_path, {"analysis/calc.py": RATIO})
    with pytest.raises(DerivationError):
        build_bundle(tmp_path, given)


def test_an_unparsable_member_is_named(tmp_path: Path) -> None:
    write(tmp_path, {"analysis/calc.py": "import helper\n" + RATIO,
                     "analysis/helper.py": "def broken(:\n"})
    with pytest.raises(DerivationError, match="analysis/helper.py is not Python that parses"):
        build_bundle(tmp_path, "analysis/calc.py")


# --- The stored copy: written once, by hash, and what a filed claim re-runs -----------------

def test_the_hash_is_over_the_canonical_manifest(tmp_path: Path) -> None:
    write(tmp_path, {"analysis/calc.py": "import helper\n" + RATIO,
                     "analysis/helper.py": "x = 1\n"})
    b = build_bundle(tmp_path, "analysis/calc.py")
    files = {"calc.py": hashlib.sha256(("import helper\n" + RATIO).encode()).hexdigest(),
             "helper.py": hashlib.sha256(b"x = 1\n").hexdigest()}
    canonical = json.dumps({"format": "stemma-bundle/1", "script": "calc.py", "files": files},
                           sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert b.sha256 == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    sha = store_bundle(tmp_path, b)
    stored = tmp_path / "store" / "derivations" / sha
    assert (stored / "bundle.json").read_bytes() == canonical.encode("utf-8")
    assert load_bundle(tmp_path, sha) == b


def test_a_tampered_stored_bundle_is_refused(tmp_path: Path) -> None:
    write(tmp_path, {"analysis/calc.py": RATIO})
    sha = store_bundle(tmp_path, build_bundle(tmp_path, "analysis/calc.py"))
    (tmp_path / "store" / "derivations" / sha / "files" / "calc.py").write_text(
        "from stemma.analysis import result\nresult(0.75)\n")
    with pytest.raises(DerivationError, match="calc.py does not match its hash"):
        load_bundle(tmp_path, sha)
    for bad in ("../" + sha, "a" * 63, sha.upper()):
        with pytest.raises(DerivationError, match="not a bundle's SHA-256"):
            load_bundle(tmp_path, bad)


def test_a_linked_store_folder_is_refused(tmp_path: Path) -> None:
    write(tmp_path, {"analysis/calc.py": RATIO})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "store").symlink_to(elsewhere)
    with pytest.raises(Refused, match="could not be stored"):
        check(estimated("analysis/calc.py", 0.75), tmp_path, rerun_fn=rerun, lookup=lookup_ab)
    assert list(elsewhere.iterdir()) == []


def _run_with_ab(tmp_path: Path) -> tuple[Register, str, str]:
    run = tmp_path / "run"
    run.mkdir()
    reg = Register(run, evidence=FakeEvidence())
    a = reg.file_claim(WORKER, claim())["claim_id"]
    b = reg.file_claim(WORKER, claim(
        statement="Acme Widgets' fiscal 2024 revenue was 1.0bn.",
        value={"number": 1.0e9, "unit": "USD", "period": "FY2024", "kind": "level",
               "as_written": "1.0bn"},
        support=[{"receipt_id": "r1", "quote": "up from 1.0bn"}]))["claim_id"]
    return reg, a, b


def _growth(a: str, b: str, number: float) -> dict[str, Any]:
    return claim(statement="Revenue grew by a factor of 1.2.", tier="estimated", support=[],
                 value={"number": number, "kind": "ratio", "denominator": "FY2024 revenue",
                        "as_written": "1.2"},
                 derivation={"script": "analysis/growth.py", "inputs": [a[:12], b[:12]]})


def test_editing_a_filed_script_changes_nothing_filed(tmp_path: Path) -> None:
    reg, a, b = _run_with_ab(tmp_path)
    run = reg.run_dir
    write(run, {"analysis/growth.py": (
                    "from lib import ratio\nfrom stemma.analysis import inputs, result\n"
                    f"v = inputs('{a[:12]}', '{b[:12]}')\n"
                    f"result(ratio(v['{a[:12]}'], v['{b[:12]}']))\n"),
                "analysis/lib.py": "def ratio(x, y):\n    return x / y\n"})
    filed = reg.file_claim(WORKER, _growth(a, b, 1.2))
    rec = reg.get_claim(filed["claim_id"])
    sha = rec["derivation"]["bundle_sha256"]
    check_ = next(c for c in rec["checks"] if c["check"] == "derivation_reproduces")
    assert check_["bundle_sha256"] == sha

    # The scripts change after filing: the run's analysis/ no longer gives 1.2.
    (run / "analysis" / "lib.py").write_text("def ratio(x, y):\n    return x / y + 1\n")
    assert build_bundle(run, "analysis/growth.py").sha256 != sha
    assert rerun(build_bundle(run, "analysis/growth.py"),
                 {a: 1.2e9, b: 1.0e9}).number == pytest.approx(2.2)

    again = rerun_filed(run, rec, reg._inputs(), reg.evidence)
    assert again.number == rec["value"]["number"] == 1.2

    # Even with analysis/ gone, the filed claim re-runs from its stored bundle.
    for p in sorted((run / "analysis").iterdir()):
        p.unlink()
    (run / "analysis").rmdir()
    assert rerun_filed(run, reg.get_claim(filed["claim_id"]), reg._inputs()).number == 1.2
    assert reg.get_claim(filed["claim_id"]) == rec  # the record itself is unchanged


def test_refiling_after_an_edit_keeps_the_filed_record(tmp_path: Path) -> None:
    reg, a, b = _run_with_ab(tmp_path)
    run = reg.run_dir
    write(run, {"analysis/growth.py": (
        "from stemma.analysis import inputs, result\n"
        f"v = inputs()\nresult(v['{a}'] / v['{b}'])\n")})
    first = reg.file_claim(WORKER, _growth(a, b, 1.2))
    before = reg.get_claim(first["claim_id"])
    (run / "analysis" / "growth.py").write_text(
        "from stemma.analysis import inputs, result\n# a comment\n"
        f"v = inputs()\nresult(v['{a}'] / v['{b}'])\n")
    second = reg.file_claim(WORKER, _growth(a, b, 1.2))  # reproduces, new bundle
    assert second["claim_id"] == first["claim_id"] and second["already_filed"] is True
    assert reg.get_claim(first["claim_id"]) == before  # still the first bundle's hash
    (run / "analysis" / "growth.py").write_text(
        "from stemma.analysis import result\nresult(9)\n")
    with pytest.raises(Refused, match="does not reproduce"):
        reg.file_claim(WORKER, _growth(a, b, 1.2))
    assert rerun_filed(run, before, reg._inputs()).number == 1.2


def test_the_agent_cannot_supply_the_bundle_hash(tmp_path: Path) -> None:
    raw = estimated("analysis/calc.py", 0.75)
    raw["derivation"]["bundle_sha256"] = "0" * 64
    with pytest.raises(Refused, match="bundle_sha256 is written by the register"):
        run_checks(raw, tmp_path, EV, no_run, lookup_ab)


def test_the_hash_is_not_part_of_the_claim_id() -> None:
    raw = filing.normalise_shape(estimated("analysis/calc.py", 0.75))
    with_hash = {**raw, "derivation": {**raw["derivation"], "bundle_sha256": "f" * 64}}
    assert filing.claim_id_of(raw) == filing.claim_id_of(with_hash)


def test_a_claim_filed_without_a_stored_bundle_fails_loudly(tmp_path: Path) -> None:
    rec = {"claim_id": "c" * 64, "derivation": {"script": "analysis/calc.py", "inputs": []}}
    with pytest.raises(DerivationError, match="no stored bundle"):
        rerun_filed(tmp_path, rec, lookup_ab)
    rec["derivation"]["bundle_sha256"] = "d" * 64
    with pytest.raises(DerivationError, match="lacks"):
        rerun_filed(tmp_path, rec, lookup_ab)


def test_concurrent_filings_store_one_bundle(tmp_path: Path) -> None:
    bundles = [Bundle("calc.py", {"calc.py": RATIO.encode(), "h.py": b"x = %d\n" % (i % 2)})
               for i in range(8)]
    barrier = threading.Barrier(len(bundles))
    out: list[str] = []
    errors: list[BaseException] = []

    def go(b: Bundle) -> None:
        barrier.wait()
        try:
            out.append(store_bundle(tmp_path, b))
        except BaseException as exc:  # noqa: BLE001 - the test reports any failure
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(b,)) for b in bundles]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(set(out)) == sorted({bundles[0].sha256, bundles[1].sha256})
    for sha in set(out):
        assert load_bundle(tmp_path, sha).sha256 == sha
