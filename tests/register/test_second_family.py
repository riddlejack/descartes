"""N1: a second model family's look at each cited claim, against the fake Nightshift
transport. It only adds a check: a disagreement starts a third Claude look, which
decides; it never approves, never lowers, and its absence never blocks a release."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_store import (
    LEAD,
    TEXT,
    WORKER,
    FakeEvidence,
    checker_for,
    claim,
    launched,
    ready_run,
)

from stemma.datalayer.querylog import QueryLog
from stemma.providers.nightshift import HttpAnswer, NightshiftClient, NightshiftOffline
from stemma.register import checks_api
from stemma.register import second_family as sf
from stemma.register.filing import Refused
from stemma.register.store import Register

TOKEN = "rt-test-0123456789abcdef"


@pytest.fixture
def reg(tmp_path: Path) -> Register:
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "criteria.md").write_text("- C1: Says whether the target is met.\n")
    return Register(tmp_path / "run", evidence=FakeEvidence())


def answer(verdict: str, reason: str = "the passage says so") -> sf.Answer:
    return sf.Answer(verdict, reason, "meterless/nemotron", "nvidia/nemotron-3-ultra-550b-a55b",
                     {"provider": "nightshift", "route": "meterless/nemotron",
                      "served_model": "nvidia/nemotron-3-ultra-550b-a55b"})


class FakeTransport:
    """Speaks the Nightshift contract: answers chat with the next queued content."""

    def __init__(self, contents: list[str], status: int = 200, offline: bool = False,
                 served: str = "nvidia/nemotron-3-ultra-550b-a55b",
                 listed: tuple[str, ...] = (sf.PRIMARY_MODEL, sf.FALLBACK_MODEL)) -> None:
        self.contents, self.status, self.offline = list(contents), status, offline
        self.served, self.listed = served, listed
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, body: bytes | None, headers: dict[str, str],
                 timeout: float) -> HttpAnswer:
        self.calls.append({"method": method, "url": url,
                           "body": json.loads(body) if body else None, "headers": headers})
        if self.offline:
            raise NightshiftOffline("refused")
        if self.status != 200:
            return HttpAnswer(self.status, {}, b'{"error": "provider_auth_failed"}')
        if method == "GET":
            ids = [{"id": "meterless/deep"}] + [{"id": m} for m in self.listed]
            return HttpAnswer(200, {}, json.dumps({"object": "list", "data": ids}).encode())
        content = self.contents.pop(0)
        raw = {"id": "r1", "model": self.served,
               "choices": [{"message": {"role": "assistant", "content": content},
                            "finish_reason": "stop"}], "usage": {"total_tokens": 9}}
        return HttpAnswer(200, {"meterless-route": "nemotron"}, json.dumps(raw).encode())


def client(t: FakeTransport) -> NightshiftClient:
    return NightshiftClient("http://127.0.0.1:8088", TOKEN, QueryLog(), t)


# --- The question and the answer ---------------------------------------------------

def test_the_question_carries_the_exact_stored_passage(reg: Register) -> None:
    c = claim()
    found = sf.passages(reg.run_dir, c, FakeEvidence())
    assert found[0]["placed"] == "yes" and found[0]["passage"] == TEXT  # all within context
    msgs = sf.messages(c, found)
    assert msgs[0]["role"] == "system" and "entity, the period, the unit" in msgs[0]["content"]
    user = msgs[1]["content"]
    assert c["statement"] in user and TEXT in user and '"as_written": "1.2bn"' in user


def test_an_unplaceable_quotation_is_marked_not_hidden(reg: Register) -> None:
    c = claim(support=[{"receipt_id": "r1", "quote": "words not in the text"}])
    assert sf.passages(reg.run_dir, c, FakeEvidence())[0]["placed"].startswith("no:")


@pytest.mark.parametrize(("text", "ok"), [
    ('{"verdict": "supports", "reason": "matches"}', True),
    ('Sure: {"verdict": "does_not_support", "reason": "FY2024 not FY2025"}', True),
    ('{"verdict": "cannot_tell"}', True),
    ('{"verdict": "maybe"}', False), ("supports", False), ("", False), (None, False),
    (("<think>The format is {\"verdict\": \"supports\"}; but FY2024 is not FY2025."
      "</think>{\"verdict\": \"does_not_support\", \"reason\": \"wrong year\"}"), True),
    (("The user wants me to reply with {\"verdict\": \"supports\" | ...}. Looking at the "
      "passage, the year differs.\n{\"verdict\": \"cannot_tell\", \"reason\": \"x\"}"), True),
])
def test_only_a_three_way_json_answer_is_read(text: str | None, ok: bool) -> None:
    if ok:
        assert sf.parse(text)[0] in sf.ANSWERS
    else:
        with pytest.raises((ValueError, json.JSONDecodeError)):
            sf.parse(text)


def test_asking_through_nightshift_names_the_served_model(reg: Register) -> None:
    t = FakeTransport(['{"verdict": "supports", "reason": "the passage gives 1.2bn"}'])
    ans = sf.ask(sf.nightshift_asker(client(t)), reg.run_dir, claim(), FakeEvidence(),
                 models=("meterless/nemotron",))
    assert (ans.verdict, ans.served_model) == ("supports", "nvidia/nemotron-3-ultra-550b-a55b")
    assert ans.receipt["purpose"] == "second_family" and "base_url" in ans.receipt
    assert TOKEN not in json.dumps(ans.receipt)
    assert t.calls[0]["body"]["model"] == "meterless/nemotron"
    assert t.calls[0]["body"]["temperature"] == 0


def test_the_fallback_is_asked_when_the_primary_answers_unreadably(reg: Register) -> None:
    t = FakeTransport(["no idea", '{"verdict": "cannot_tell", "reason": "no period"}'])
    ans = sf.ask(sf.nightshift_asker(client(t)), reg.run_dir, claim(), FakeEvidence(),
                 models=("meterless/a", "meterless/b"))
    assert ans.model_asked == "meterless/b" and ans.verdict == "cannot_tell"


@pytest.mark.parametrize(("transport", "why"), [
    (FakeTransport([], status=401), "provider_auth_failed"),
    (FakeTransport([], offline=True), "nightshift_offline"),
])
def test_an_unreachable_or_refusing_nightshift_is_unavailable(reg: Register,
                                                              transport: FakeTransport,
                                                              why: str) -> None:
    with pytest.raises(sf.SecondFamilyUnavailable, match=why):
        sf.ask(sf.nightshift_asker(client(transport)), reg.run_dir, claim(), FakeEvidence(),
               models=("meterless/a", "meterless/b"))
    if why == "provider_auth_failed":
        assert len(transport.calls) == 1  # the same key fails for every model: asked once


def test_nemotron_is_pinned_by_name_when_the_runtime_lists_it(reg: Register) -> None:
    t = FakeTransport(['{"verdict": "supports", "reason": "ok"}'])
    ans = sf.ask(sf.nightshift_asker(client(t)), reg.run_dir, claim(), FakeEvidence())
    post = [c for c in t.calls if c["method"] == "POST"]
    assert post[0]["body"]["model"] == sf.PRIMARY_MODEL
    assert ans.receipt["pinned"] is True and ans.served_model == sf.PRIMARY_MODEL


def test_a_model_the_runtime_does_not_list_is_never_sent(reg: Register) -> None:
    t = FakeTransport(['{"verdict": "supports"}'], listed=())
    with pytest.raises(sf.SecondFamilyUnavailable, match="not listed by GET /v1/models"):
        sf.ask(sf.nightshift_asker(client(t)), reg.run_dir, claim(), FakeEvidence())
    assert not [c for c in t.calls if c["method"] == "POST"]


def test_an_answer_from_another_model_is_a_loud_failure(reg: Register) -> None:
    t = FakeTransport(['{"verdict": "supports"}', '{"verdict": "supports"}'],
                      served="some/other-model")
    with pytest.raises(sf.SecondFamilyFailed, match="answered by some/other-model"):
        sf.ask(sf.nightshift_asker(client(t)), reg.run_dir, claim(), FakeEvidence())


def test_a_meterless_route_still_works(reg: Register) -> None:
    t = FakeTransport(['{"verdict": "cannot_tell", "reason": "x"}'], served="anything")
    ans = sf.ask(sf.nightshift_asker(client(t)), reg.run_dir, claim(), FakeEvidence(),
                 models=("meterless/deep",))
    assert ans.verdict == "cannot_tell" and ans.receipt["pinned"] is False
    assert not [c for c in t.calls if c["method"] == "GET"]


def test_a_failed_look_is_recorded_and_not_asked_again(reg: Register) -> None:
    cid = _held(reg)
    reg.record_second_family_failure(cid, "answered by some/other-model")
    assert reg.second_family_due() == []
    assert reg.get_claim(cid)["verdict"]["verdict"] == "holds"
    looked = reg.second_family_record()["looked"]
    assert looked[0]["failed"] == "answered by some/other-model"
    assert reg.finish_failures(1) == []


@pytest.mark.parametrize(("claude", "second", "expected"), [
    ("holds", "does_not_support", True), ("holds_with_correction", "does_not_support", True),
    ("not_supported", "supports", True), ("holds", "supports", False),
    ("holds", "cannot_tell", False), ("could_not_check", "does_not_support", False),
    ("could_not_check", "supports", False), ("not_supported", "does_not_support", False),
])
def test_what_counts_as_a_disagreement(claude: str, second: str, expected: bool) -> None:
    assert sf.disagrees(claude, second) is expected


# --- The register: recorded beside Claude's verdict; a disagreement gets a third look --

def _held(reg: Register) -> str:
    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid, "s-claude"), cid, "holds", "the quote is there")
    return cid


def test_an_agreement_is_recorded_and_changes_nothing(reg: Register) -> None:
    cid = _held(reg)
    assert reg.second_family_due() == [cid]
    body = reg.record_second_family(cid, answer("supports"))
    assert body["disagrees"] is False and body["compared_with"] == {
        "verdict": "holds", "session": "s-claude"}
    assert reg.second_family_due() == []
    assert reg.get_claim(cid)["verdict"]["verdict"] == "holds"
    assert reg.finish_failures(1) == []
    assert not [r for r in checks_api.pending_checks(reg.run_dir)
                if r.prompt.startswith("recheck:")]


@pytest.mark.parametrize("third", ["not_supported", "holds"])
def test_a_disagreement_gets_a_third_look_which_decides(reg: Register, third: str) -> None:
    cid = _held(reg)
    body = reg.record_second_family(cid, answer("does_not_support", "the passage is FY2024"))
    assert body["disagrees"] and body["recheck_requested"]
    # Nemotron alone lowers nothing: the Claude verdict stands until the third look.
    assert reg.get_claim(cid)["verdict"]["verdict"] == "holds"
    assert any("the third look has not landed" in f for f in reg.finish_failures(1))
    chk = launched(reg, "claim-checker", f"recheck:{cid}", "s-third")
    mine = reg.check_assignment(chk)
    assert mine["claude_verdict"]["verdict"] == "holds"
    assert mine["second_family"] == {"verdict": "does_not_support",
                                     "reason": "the passage is FY2024",
                                     "model": "nvidia/nemotron-3-ultra-550b-a55b"}
    reg.record_claim_verdict(chk, cid, third, "read both opinions and the passage")
    st = next(s for s in checks_api.states(reg.run_dir).values()
              if s.request.prompt == f"recheck:{cid}")
    checks_api.mark_ended(reg.run_dir, st.request.request_id, 0, "completed")
    assert reg.get_claim(cid)["verdict"]["verdict"] == third
    unmet = reg.finish_failures(1)
    assert (unmet == []) is (third == "holds")
    rec = reg.second_family_record()
    assert rec["disagreements"] == [{
        "claim_id": cid, "claude": "holds", "second_family": "does_not_support",
        "reason": "the passage is FY2024", "third_look": third, "third_look_status": "ended"}]
    assert reg.second_family_due() == []  # never asked twice, so no loop


def test_a_rejection_the_second_family_would_approve_is_not_approved(reg: Register) -> None:
    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid, "s-claude"), cid, "not_supported", "no")
    reg.record_second_family(cid, answer("supports"))
    assert reg.get_claim(cid)["verdict"]["verdict"] == "not_supported"
    assert f"claim {cid[:12]} has verdict not_supported" in reg.finish_failures(1)


def test_only_the_third_look_session_may_judge_it(reg: Register) -> None:
    cid = _held(reg)
    reg.record_second_family(cid, answer("does_not_support"))
    with pytest.raises(Refused, match="not started for"):
        reg.record_claim_verdict(_caller("s-nobody"), cid, "holds", "x")


def _caller(session: str) -> Any:
    from stemma.register.store import Caller

    return Caller("stemma:claim-checker", None, session)


def test_unavailability_is_recorded_and_never_blocks(reg: Register) -> None:
    cid = _held(reg)
    reg.second_family_unavailable("provider_auth_failed: HTTP 401")
    assert reg.finish_failures(1) == []
    out = reg.finish_run(LEAD, 1)
    assert out["finished"]
    rec = json.loads((reg.run_dir / "deliverable/record.json").read_text())
    assert rec["second_family"]["unavailable"][0]["reason"].startswith("provider_auth_failed")
    assert cid


def test_uncited_and_unverified_claims_are_not_asked(reg: Register) -> None:
    cid = ready_run(reg)
    assert reg.second_family_due() == []  # no Claude verdict yet
    other = reg.file_claim(WORKER, claim(statement="Uncited 1.2bn."))["claim_id"]
    req = next(r for r in checks_api.pending_checks(reg.run_dir) if r.prompt == "doc:r1")
    checks_api.mark_launched(reg.run_dir, req.request_id, "s-unv", "claude-opus-5-5", "high")
    reg.record_claim_verdict(_caller("s-unv"), cid, "holds", "ok")  # not verified yet
    assert reg.second_family_due() == []
    checks_api.mark_verified(reg.run_dir, "s-unv", "claude-opus-5-5", "high")
    assert reg.second_family_due() == [cid] and other not in reg.second_family_due()


def test_reasoning_before_the_answer_is_never_the_verdict() -> None:
    text = ('The user wants me to reply with JSON like {"verdict": "supports", "reason": '
            '"..."}. The passage says FY2024.\n'
            '{"verdict": "does_not_support", "reason": "the passage is FY2024"}')
    assert sf.parse(text) == ("does_not_support", "the passage is FY2024")
    think = '<think>{"verdict": "supports"}</think>no final answer'
    with pytest.raises(ValueError):
        sf.parse(think)


def test_the_answer_gets_a_realistic_token_budget(reg: Register) -> None:
    t = FakeTransport(['{"verdict": "supports"}'])
    sf.ask(sf.nightshift_asker(client(t)), reg.run_dir, claim(), FakeEvidence())
    post = next(c for c in t.calls if c["method"] == "POST")
    assert post["body"]["max_tokens"] == sf.MAX_TOKENS >= 2048
