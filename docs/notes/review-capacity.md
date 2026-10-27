# Reviewed release under bounded capacity

Submission previously consumed a review round even when the frozen text could not
pass mechanical release checks. Once the allowance ran out, corrected text could
neither obtain review nor release, while the driver could keep restarting its author.

The owner chose to preserve independent review of the released text. Submission now
shares content checks with release and rejects all detected content defects before
scheduling work. Verdicts stay at release because submission causes claim checking.
At exhausted capacity, checking and retries settle before a typed, unreleased end.
The existing treatment of final-round reviewer criticisms remains unchanged.

A separate operator action may grant one further round with a reason, expected
current limit and stable id. It does not edit the frozen configuration or reset any
other budget. A sealed register event owns the grant; the ordinary event log mirrors
it. Using only events.jsonl would make release authority depend on a log excluded
from the sealed register snapshot. Retrying after a mirror-write failure repairs the
mirror without granting twice. The MCP surface has no grant operation.

Implementation reuses the repository's existing register lock, append-only facts,
safe filesystem operations, supervisor lock and session lifecycle. The approved
open-source survey was reviewed; no new dependency or parallel persistence system
was needed. This is an admission/exhaustion correction over those existing seams.

Tests cover the legacy deadlock, rejection without spend, unchecked claims accepted
for checking, exact-version review required for release, concurrent conditional
grants, idempotent retry, unchanged frozen configuration, and the terminal driver and
Stop-hook behavior. Provider calls are fakes. Run-specific replay, timing, deviation
and launch records belong outside the repository.
