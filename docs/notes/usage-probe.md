# Token accounting: live probe result

26 October 2026, Claude Code 2.1.308, the clean config (a signed-in Claude Code configuration),
`tests/e2e/usage_probe.py`.

A placeholder lead (the real plugin, hooks and tools) started one worker subagent with
a one-word job. `usage.jsonl` held two records: the lead's session and the worker
subagent (branch `b-probe`, taken from its brief).
- The lead's tokens counted from its transcript (4 input, 455 output, 8,449 written to
  cache, 30,393 read from cache) equal the `usage` of its own stream-json result exactly.
- The result's `modelUsage` (6 input, 459 output, 28,439 written to cache) equals the
  lead plus the worker (2, 4, 19,990) exactly. So `usage` covers the main thread only,
  and `modelUsage` and `total_cost_usd` ($0.18, at list prices) cover the subagents too.
- `usage.md` and the summary in `status.json` showed both roles and the framing phase.
