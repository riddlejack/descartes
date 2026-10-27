"""Operator continuation and terminal review exhaustion, with fake sessions only."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from driverkit import Fake, write_task

from stemma.driver import events, hostio
from stemma.driver.config import STATE_FILE, DriverConfig, Limits
from stemma.driver.errors import RunStateError
from stemma.driver.runfolder import create_run
from stemma.driver.runner import OUTCOME_CODES, Driver, resume
from stemma.hooks.stop import decide
from stemma.register import checks_api, jsonl
from stemma.register.store import Caller, Register


def stopped(cfg: DriverConfig, tmp: Path, limits: Limits) -> Path:
    run = create_run(cfg, write_task(tmp), limits=limits)
    Register(run)
    hostio.write_json(run, STATE_FILE, {
        'run_id': run.name, 'lead': {'session_id': 'prior-session', 'launches': 1,
                                   'running': False, 'outcome': 'completed'},
        'artifacts': {}, 'check_attempts': {}, 'lead_resumes': limits.max_lead_resumes,
        'outcome': 'ended_without_release',
    })
    return run


def final_draft(run: Path) -> Register:
    reg = Register(run)
    (run / 'criteria.md').write_text('- C1: Answer the question.\n')
    (run / 'draft').mkdir(exist_ok=True)
    (run / 'draft/answer.v1.md').write_text('A bounded answer.\n')
    reg.submit_draft(Caller('stemma:lead', None, 'prior-session'), 1)
    return reg


def settle(run: Path) -> None:
    for st in checks_api.states(run).values():
        if st.status == 'pending':
            checks_api.mark_launched(run, st.request.request_id, 'checker-' + st.request.request_id,
                                     'test-model', 'test-effort')
        if st.status != 'ended':
            checks_api.mark_ended(run, st.request.request_id, 0, 'completed')


def test_driver_and_stop_hook_wait_for_checks_then_end_without_resuming(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> None:
    lim = dataclasses.replace(limits, max_review_rounds=1)
    run = stopped(cfg, tmp, lim)
    final_draft(run)
    d = Driver(cfg, run, checks_api, lim)
    assert not d._stop_for_review_exhaustion()  # pending checks may repair the gate
    settle(run)
    assert not decide({'session_id': 'prior-session'}, run, wait_s=0).block
    assert d.step() is False
    assert d._finish(d._run_outcome()) == OUTCOME_CODES['review_rounds_exhausted']
    status = json.loads((run / 'status.json').read_text())
    assert status['outcome'] == 'review_rounds_exhausted'
    assert fake.calls() == []
    assert not [e for e in events.read(run) if e['event'] == 'lead_resumed_after_end']
    assert [e for e in events.read(run) if e['event'] == 'review_rounds_exhausted']


def test_driver_stops_an_active_lead_at_terminal_exhaustion(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> None:
    lim = dataclasses.replace(limits, max_review_rounds=1)
    run = stopped(cfg, tmp, lim)
    final_draft(run)
    settle(run)
    fake.configure({'lead@resume': {'sleep_s': 30}})
    d = Driver(cfg, run, checks_api, lim)
    d.start_lead(resume=True)
    try:
        assert d.watch() == OUTCOME_CODES['review_rounds_exhausted']
        assert not d.live
        assert len(fake.calls()) == 1
    finally:
        for live in list(d.live.values()):
            live.session.stop('driver_stopped', lim.stop_grace_s)
            live.session.reap(lim.stop_grace_s)


def test_manual_resume_records_reason_grants_once_and_preserves_automatic_budget(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> None:
    lim = dataclasses.replace(limits, max_review_rounds=2)
    run = stopped(cfg, tmp, lim)
    before = (run / 'run.json').read_bytes()
    fake.configure({'lead@resume': {'sleep_s': 0.1}})
    reason = 'Permit one reviewed correction.'
    resume(cfg, run, checks_api, lim, reason=reason, grant_from=2, grant_id='approval')
    state = json.loads((run / STATE_FILE).read_text())
    assert state['lead_resumes'] == lim.max_lead_resumes
    assert (run / 'run.json').read_bytes() == before
    assert Register.read_only(run).max_review_rounds() == 3
    record = [e for e in events.read(run) if e['event'] == 'run_resumed'][-1]
    assert record['operator_reason'] == reason and not record['automatic']
    assert record['lead_remaining_s'] <= lim.lead_max_s
    notices = jsonl.read_all(run / '.stemma/arrivals.jsonl')
    notice = next(n for n in notices if n.get('name') == 'operator_review_grant_approval')
    assert notice['text'] == ('The operator granted one more review round. Submission now runs '
                              'the release checks first; fix anything it lists, then submit '
                              'your final version.')


@pytest.mark.parametrize('reason', ['', '   '])
def test_manual_resume_requires_reason_before_any_launch(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits, reason: str) -> None:
    run = stopped(cfg, tmp, limits)
    with pytest.raises(RunStateError, match='operator reason'):
        resume(cfg, run, checks_api, limits, reason=reason)
    assert fake.calls() == []


def test_no_time_left_refuses_manual_resume_and_does_not_grant(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> None:
    lim = dataclasses.replace(limits, lead_max_s=60, max_review_rounds=2)
    run = stopped(cfg, tmp, lim)
    jsonl.append(run / 'events.jsonl',
                 {'event': 'session_launched', 'role': 'lead', 'session': 'old',
                  'at': '2000-01-01T00:00:00Z'},
                 {'event': 'session_ended', 'role': 'lead', 'session': 'old',
                  'at': '2000-01-01T00:01:00Z'})
    with pytest.raises(RunStateError, match='time allowance is exhausted'):
        resume(cfg, run, checks_api, lim, reason='Continue.', grant_from=2, grant_id='approval')
    assert Register.read_only(run).max_review_rounds() == 2
    assert fake.calls() == []


def test_repeated_grant_cannot_buy_a_further_round_after_failure(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> None:
    lim = dataclasses.replace(limits, max_review_rounds=1)
    run = stopped(cfg, tmp, lim)
    reg = final_draft(run)
    settle(run)
    reg.grant_review_round(reason='Continue.', expected_limit=1, grant_id='approval')
    (run / 'draft/answer.v2.md').write_text('A revised answer.\n')
    reg.submit_draft(Caller('stemma:lead', None, 'prior-session'), 2)
    settle(run)
    with pytest.raises(RunStateError, match='review_rounds_exhausted'):
        resume(cfg, run, checks_api, lim, reason='Continue.', grant_from=1, grant_id='approval')
    assert reg.max_review_rounds() == 2
    assert fake.calls() == []


def test_exhausted_retry_is_terminal_without_another_launch(
        cfg: DriverConfig, tmp: Path, fake: Fake, limits: Limits) -> None:
    lim = dataclasses.replace(limits, max_review_rounds=1)
    run = stopped(cfg, tmp, lim)
    final_draft(run)
    pending = checks_api.pending_checks(run)
    failed = pending[0].request_id
    for req in pending:
        checks_api.mark_launched(run, req.request_id, 'checker-' + req.request_id,
                                 'test-model', 'test-effort')
        checks_api.mark_ended(run, req.request_id, 1 if req.request_id == failed else 0,
                             'failed' if req.request_id == failed else 'completed')
    d = Driver(cfg, run, checks_api, lim)
    d.state['check_attempts'][failed] = lim.max_check_attempts
    assert d._stop_for_review_exhaustion()
    assert checks_api.states(run)[failed].outcome == 'given_up'
    with pytest.raises(checks_api.UnknownCheck):
        checks_api.mark_ended(run, failed, -1, 'given_up')
    assert fake.calls() == []


def test_outstanding_second_family_work_prevents_terminal_exhaustion(
        cfg: DriverConfig, tmp: Path, limits: Limits, monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import Future

    lim = dataclasses.replace(limits, max_review_rounds=1)
    run = stopped(cfg, tmp, lim)
    final_draft(run)
    settle(run)
    d = Driver(cfg, run, checks_api, lim)
    pending: Future[object] = Future()

    def start_second_look() -> None:
        d._sf_futures['synthetic-claim'] = pending

    monkeypatch.setattr(d, '_second_family_step', start_second_look)
    assert not d._stop_for_review_exhaustion()
    assert 'review_rounds_exhausted' not in d.state
    pending.cancel()
