"""The Codex recheck's last items on a6929ff, each reproduced, now refused."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_filing import EV, estimated, lookup_ab
from test_store import LEAD, FakeEvidence, checker_for, ready_run

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
    return Register(tmp_path / "run", evidence=FakeEvidence())


def test_unverified_checker_verdict_does_not_release(reg: Register) -> None:
    cid = ready_run(reg)
    req = next(r for r in checks_api.pending_checks(reg.run_dir) if r.prompt == f"claim:{cid}")
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
