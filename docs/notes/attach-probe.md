# Arrival notices reach a headless lead: probe result

26 October 2026, Claude Code 2.1.308, the clean config, `tests/e2e/attach_probe.py`.

**Shown.** A lead started by the driver (real plugin, hooks and tools; placeholder text)
received the notice of a file handed to it by `stemma attach` while it was waiting.
- The lead's first Bash call waited until `task/arrivals` existed.
- 20 seconds in, `stemma attach <run> client-questions.csv` copied the file to
  `task/arrivals/1/client-questions.csv` and queued the notice.
- The lead's next tool call (`ls -R task`) carried it. The PreToolUse hook returned it as
  `additionalContext`, and `.stemma/arrivals.jsonl` and an `arrival_notice_delivered`
  event recorded the delivery with that call's tool_use_id.
- The lead quoted the notice word for word, labelled by the harness as "PreToolUse:Bash
  hook additional context".

**Negative first attempt, recorded.** The first probe asked the lead to run `sleep 45`.
The harness blocks a standalone `sleep` in Bash, so the lead never waited, both its
calls ran before the attach, and nothing was delivered. It reported NONE, which is
correct. The second probe waited with an until-loop instead.

**Earlier probe, same day.** A bare plugin with PreToolUse and PostToolUse hooks that
each return `additionalContext`: the headless model saw both texts.
