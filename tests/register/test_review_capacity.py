"""Submission preflight and explicit operator capacity preserve reviewed releases."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from test_store import (
    LEAD,
    WORKER,
    FakeEvidence,
    checker_for,
    claim,
    frames_and_reviews,
    ready_run,
)

from stemma.register import checks_api
from stemma.register.filing import Refused
from stemma.register.store import Register


def prepared(tmp_path: Path, limit: int = 2, *, check: bool = True) -> tuple[Register, str]:
    root = tmp_path / 'run'
    root.mkdir()
    (root / 'criteria.md').write_text('- C1: The answer says whether the target is met.\n')
    (root / 'run.json').write_text(json.dumps({'limits': {'max_review_rounds': limit}}))
    reg = Register(root, evidence=FakeEvidence())
    cid = ready_run(reg)
    if check:
        reg.record_claim_verdict(checker_for(reg, cid), cid, 'holds', 'The source supports it.')
    return reg, cid


def settle(reg: Register) -> None:
    for st in checks_api.states(reg.run_dir).values():
        checks_api.mark_ended(reg.run_dir, st.request.request_id, 0, 'completed')


def test_legacy_deadlock_is_recovered_only_by_an_operator_grant_and_new_reviews(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg, cid = prepared(tmp_path)
    bad = f'Revenue was 1.2bn [[c:{cid[:12]}]].\nThere are 3 labels.\n'
    (reg.run_dir / 'draft/answer.v2.md').write_text(bad)
    # Reproduce an already persisted legacy run; current submission refuses this text.
    with monkeypatch.context() as m:
        m.setattr(reg, 'draft_preflight', lambda _text: [])
        reg.submit_draft(LEAD, 2)
    frames_and_reviews(reg, 2)
    settle(reg)
    assert reg.review_exhaustion() is not None
    original_config = (reg.run_dir / 'run.json').read_bytes()
    repaired = bad.replace('3 labels.', '3 labels [[nc:label count]].')
    (reg.run_dir / 'draft/answer.v3.md').write_text(repaired)
    with pytest.raises(Refused, match='review-round limit'):
        reg.submit_draft(LEAD, 3)
    reg.grant_review_round(reason='Permit one reviewed correction.', expected_limit=2,
                           grant_id='reviewed-correction')
    assert reg.review_exhaustion() is None
    reg.submit_draft(LEAD, 3)
    with pytest.raises(Refused, match='review of version 3'):
        reg.finish_run(LEAD, 3)
    with pytest.raises(Refused, match='version 4 was not submitted'):
        reg.finish_run(LEAD, 4)
    frames_and_reviews(reg, 3)
    assert reg.finish_run(LEAD, 3)['finished']
    assert (reg.run_dir / 'deliverable/answer.md').read_text() == repaired
    record = json.loads((reg.run_dir / 'deliverable/record.json').read_text())
    assert record['review_rounds']['configured_limit'] == 2
    assert record['review_rounds']['limit'] == 3
    assert len(record['review_rounds']['operator_grants']) == 1
    assert (reg.run_dir / 'run.json').read_bytes() == original_config


def test_preflight_lists_all_content_failures_and_schedules_nothing(tmp_path: Path) -> None:
    reg, cid = prepared(tmp_path, check=False)
    reg.record_claim_verdict(checker_for(reg, cid, 'correction'), cid,
                            'holds_with_correction', 'Narrow the statement.',
                            corrected_claim=claim(statement='The source states the revenue.'))
    (reg.run_dir / 'draft/answer.v2.md').write_text(
        f'Bad tag [[c:BAD]].\n3 labels.\nOld [[c:{cid[:12]}]].\nUnknown [[c:000000000000]].\n')
    before = {p: (reg.run_dir / p).read_bytes()
              for p in ('register/log.jsonl', 'register/checks.jsonl')}
    with pytest.raises(Refused) as e:
        reg.submit_draft(LEAD, 2)
    for expected in ('malformed tag', 'untagged number', 'is superseded', 'names no claim'):
        assert expected in str(e.value)
    assert reg.submitted_versions() == [1]
    assert not (reg.run_dir / 'register/drafts/answer.v2.md').exists()
    assert all((reg.run_dir / p).read_bytes() == data for p, data in before.items())


def test_unchecked_claim_is_accepted_for_checking_but_cannot_release(tmp_path: Path) -> None:
    reg, _ = prepared(tmp_path)
    cid = reg.file_claim(WORKER, claim(statement='A separately stated revenue claim.'))['claim_id']
    (reg.run_dir / 'draft/answer.v2.md').write_text(f'Revenue was 1.2bn [[c:{cid[:12]}]].\n')
    result = reg.submit_draft(LEAD, 2)
    assert result['claim_checks_requested']
    assert reg.review_exhaustion() is None  # checks and reviews are still running
    frames_and_reviews(reg, 2)
    with pytest.raises(Refused, match='verdict none'):
        reg.finish_run(LEAD, 2)
    reg.record_claim_verdict(checker_for(reg, cid, 'new-claim'), cid, 'holds', 'Supported.')
    settle(reg)
    assert reg.review_exhaustion() is None  # leave a releasable memo to its lead
    assert reg.finish_run(LEAD, 2)['finished']


def test_final_rejected_claim_exhausts_after_checks_settle(tmp_path: Path) -> None:
    reg, cid = prepared(tmp_path, 1, check=False)
    reg.record_claim_verdict(checker_for(reg, cid), cid, 'not_supported', 'Wrong scope.')
    assert reg.review_exhaustion() is None
    settle(reg)
    exhausted = reg.review_exhaustion()
    assert exhausted is not None and exhausted.limit == 1
    assert any('not_supported' in f for f in exhausted.unmet)
    with pytest.raises(Refused, match='not_supported'):
        reg.finish_run(LEAD, 1)


def test_preflight_is_rechecked_at_release(tmp_path: Path) -> None:
    reg, cid = prepared(tmp_path, check=False)
    reg.record_claim_verdict(checker_for(reg, cid, 'correction'), cid,
                            'holds_with_correction', 'Narrow the statement.',
                            corrected_claim=claim(statement='The source states the revenue.'))
    with pytest.raises(Refused, match='superseded'):
        reg.finish_run(LEAD, 1)


def test_grant_retry_is_idempotent_and_seen_by_an_existing_reader(tmp_path: Path) -> None:
    reg, _ = prepared(tmp_path)
    reader = Register.read_only(reg.run_dir)
    grant = {'reason': 'Permit another reviewed round.', 'expected_limit': 2, 'grant_id': 'approval'}
    assert reg.grant_review_round(**grant) == reg.grant_review_round(**grant)
    assert reader.max_review_rounds() == 3
    assert len(reg.run_facts('review_round_granted')) == 1
    assert len((reg.run_dir / 'events.jsonl').read_text().splitlines()) == 1
    with pytest.raises(Refused, match='changed'):
        reg.grant_review_round(**{**grant, 'grant_id': 'another'})
    with pytest.raises(Refused, match='another grant'):
        reg.grant_review_round(**{**grant, 'reason': 'A different decision.'})


def test_concurrent_grants_are_conditional(tmp_path: Path) -> None:
    reg, _ = prepared(tmp_path)
    barrier = Barrier(2)

    def grant(ident: str) -> bool:
        other = Register(reg.run_dir)
        barrier.wait(timeout=10)
        try:
            other.grant_review_round(reason='One further round.', expected_limit=2, grant_id=ident)
            return True
        except Refused:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(grant, ['first', 'second']))
    assert sorted(results) == [False, True]
    assert reg.max_review_rounds() == 3


def test_grant_cannot_write_through_an_event_log_symlink(tmp_path: Path) -> None:
    reg, _ = prepared(tmp_path)
    outside = tmp_path / 'outside'
    outside.write_text('keep')
    (reg.run_dir / 'events.jsonl').symlink_to(outside)
    with pytest.raises(OSError):
        reg.grant_review_round(reason='One round.', expected_limit=2, grant_id='approval')
    assert outside.read_text() == 'keep'


def test_release_refuses_a_post_seal_grant(tmp_path: Path) -> None:
    reg, _ = prepared(tmp_path)
    reg.finish_run(LEAD, 1)
    with pytest.raises(Refused, match='released and sealed'):
        reg.grant_review_round(reason='One round.', expected_limit=2, grant_id='approval')


def test_grant_survives_missing_event_mirror_and_is_in_sealed_snapshot(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from stemma.register import jsonl

    reg, _ = prepared(tmp_path)
    grant = {'reason': 'One reviewed correction.', 'expected_limit': 2, 'grant_id': 'approval'}
    with monkeypatch.context() as patch:
        def interrupted(*args: object, **kwargs: object) -> None:
            raise OSError('interrupted event mirror')
        patch.setattr(jsonl, 'append', interrupted)
        with pytest.raises(OSError, match='interrupted'):
            reg.grant_review_round(**grant)
    assert reg.max_review_rounds() == 3
    reg.grant_review_round(**grant)
    assert len(reg.run_facts('review_round_granted')) == 1
    assert len(jsonl.read_all(reg.run_dir / 'events.jsonl')) == 1
    sealed = Register.from_sealed(reg.run_dir, (reg.dir / 'log.jsonl').read_bytes(),
                                  (reg.dir / 'checks.jsonl').read_bytes())
    assert sealed.max_review_rounds() == 3
    with pytest.raises(Refused):
        sealed.grant_review_round(**grant)
