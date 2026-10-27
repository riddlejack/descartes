"""The Codex recheck's last items on a6929ff, each reproduced, now refused."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_filing import EV, estimated, lookup_ab
from test_store import LEAD, WORKER, FakeEvidence, checker_for, claim, ready_run

from stemma.register import checks_api
from stemma.register.derivation import rerun
from stemma.register.filing import Refused, run_checks
from stemma.register.store import Caller, Register

# --- 5. The derivation sandbox reads only the script, its inputs and the runtime -------

def test_derivation_cannot_read_home_run_or_repo(tmp_path: Path) -> None:
    home_decoy = Path.home() / f".stemma-deriv-decoy-{tmp_path.name}.txt"
    home_decoy.write_text("7")
    run_file = tmp_path / "frame.md"
    run_file.write_text("8")
    repo_file = Path(__file__).resolve().parents[2] / "pyproject.toml"
    script = tmp_path / "analysis" / "leak.py"
    script.parent.mkdir()
    script.write_text(
        "from stemma.analysis import result\nn = 0\n"
        f"for i, p in enumerate([{str(home_decoy)!r}, {str(run_file)!r}, {str(repo_file)!r}, "
        "'/etc/hosts']):\n"
        "    try:\n        open(p).read(); n += 10 ** i\n    except OSError:\n        pass\n"
        "result(n)\n")
    try:
        assert rerun(script, {}).number == 0.0  # not one of the four files was readable
    finally:
        home_decoy.unlink()


def test_mismatch_never_echoes_the_scripts_number(tmp_path: Path) -> None:
    (tmp_path / "analysis").mkdir()
    (tmp_path / "analysis/calc.py").write_text(
        "from stemma.analysis import result\nresult(123456789.0)\n")
    with pytest.raises(Refused) as e:
        run_checks(estimated("analysis/calc.py", 0.75), tmp_path, EV, rerun, lookup_ab)
    assert "123456789" not in str(e.value) and "does not reproduce" in str(e.value)


# --- 3. Only verified sessions' records count; a later revocation withdraws ----------


@pytest.fixture
def reg(tmp_path: Path) -> Register:
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "criteria.md").write_text("- C1: The answer says whether the target is met.\n")
    return Register(tmp_path / "run", evidence=FakeEvidence())


def test_unverified_checker_verdict_does_not_release(reg: Register) -> None:
    cid = ready_run(reg)
    req = next(r for r in checks_api.pending_checks(reg.run_dir) if r.prompt == "doc:r1")
    checks_api.mark_launched(reg.run_dir, req.request_id, "s-unv", "claude-opus-5-5", "high")
    reg.record_claim_verdict(Caller("stemma:claim-checker", None, "s-unv"), cid, "holds", "ok")
    with pytest.raises(Refused, match="not verified yet"):
        reg.finish_run(LEAD, 1)
    assert reg.await_checks([cid], 0)["landed"] == {}
    checks_api.mark_verified(reg.run_dir, "s-unv", "claude-opus-5-5", "high")
    assert reg.finish_run(LEAD, 1)["finished"] and reg.release_state() == "released"


def test_revocation_after_release_withdraws_it(reg: Register) -> None:
    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid, "s-late"), cid, "holds", "ok")
    reg.finish_run(LEAD, 1)
    assert reg.release_state() == "released"
    reg.revoke_session("s-late", "a rescan found effort low")
    assert reg.release_state() == "withdrawn"
    from stemma.driver.runner import register_release_state, register_released
    assert register_release_state(reg.run_dir) == "withdrawn"
    assert not register_released(reg.run_dir)


def test_recorded_release_block_refuses_and_withdraws(reg: Register) -> None:
    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    checks_api.block_release(reg.run_dir, "revoking session s-x failed")
    with pytest.raises(Refused, match="release is blocked"):
        reg.finish_run(LEAD, 1)


def test_no_holds_after_this_session_s_correction_was_refused(reg: Register) -> None:
    # Walking-skeleton run, 12:40-12:41: a checker found an estimated figure wrong in a
    # detail; the register refused its correction twice (no derivation, then one that did
    # not reproduce), and it recorded holds on the claim it had just found wrong.
    cid = reg.file_claim(WORKER, claim())["claim_id"]
    chk = checker_for(reg, cid)
    wrong_estimate = claim(tier="estimated", support=[],
                           statement="Acme Widgets' fiscal 2025 revenue was about 1.1bn.")
    with pytest.raises(Refused, match="needs a derivation"):
        reg.record_claim_verdict(chk, cid, "holds_with_correction", "low end overstated",
                                 corrected_claim=wrong_estimate)
    with pytest.raises(Refused, match="correction of this claim was refused"):
        reg.record_claim_verdict(chk, cid, "holds", "holds; the author should refile")
    assert reg.get_claim(cid)["verdict"] is None
    out = reg.record_claim_verdict(chk, cid, "not_supported", "low end is 1.1bn, not 1.2bn")
    assert out["verdict"] == "not_supported"


def test_a_refused_correction_binds_only_its_own_session_and_claim(reg: Register) -> None:
    cid = reg.file_claim(WORKER, claim())["claim_id"]
    chk = checker_for(reg, cid, "s-first")
    with pytest.raises(Refused):
        reg.record_claim_verdict(chk, cid, "holds_with_correction", "x",
                                 corrected_claim=claim(tier="estimated", support=[]))
    # The driver relaunches the check: a fresh session judges afresh.
    req = next(s for s in checks_api.states(reg.run_dir).values() if s.request.prompt == "doc:r1").request
    checks_api.mark_launched(reg.run_dir, req.request_id, "s-second", "claude-opus-5-5",
                             "high", relaunch=True)
    checks_api.mark_verified(reg.run_dir, "s-second", "claude-opus-5-5", "high")
    second = Caller("stemma:claim-checker", None, "s-second")
    assert reg.record_claim_verdict(second, cid, "holds", "ok")["verdict"] == "holds"
