# Builder W3: the report viewer, "verify it yourself", the tamper demo and onboarding

Branch `claude/v2-product-111b83`. Four commands, each a subcommand of `run.py` and a
module you can run on its own. Everything here reads a run folder and writes nothing
into it, apart from the register's own lock file.

## What each command does, and how it was shown

**`python run.py report RUN OUT [--verify]`** (`src/stemma/report/site.py`) writes one
self-contained `index.html` and an empty `.nojekyll` file. Together they make a static
page that GitHub Pages serves as it is. The page loads no scripts or fonts from
elsewhere, follows the light or dark setting of the reader's device, and fits a phone.

The answer comes first. Each tagged figure carries a numbered chip. The chip opens the
claim behind the figure: its statement, tier, the checker's verdict and reasons, and
the exact stored passage. The passage shows its locator (page or section), its
character offsets, the text with context and the quotation highlighted, the source
link, and the SHA-256 (a fingerprint of the bytes) of the capture. An estimated figure
shows its script and its inputs, each input linked to its own claim. Below the answer
come the criteria, each with the open material points the last review round raised
against it, then the tree with each node's state, set-aside reasons and the branch
reviewer's verdict. Then "how this was checked": counts, claims the checkers refused
and why, cited figures not checked, the reviewers' points round by round, the blind
framer's challenge, and what was not researched. Last come the run facts.

The page is labelled RECORDED with the run id and date. For a run that never released,
it says so and shows the release gate's own list of unmet conditions.

Shown on run 1, in the browser at desktop and phone width, in light and dark: all 116
cited figures open their claims and none is broken. Screenshot:
`side-session-notes/local_5524a78e-…/w3-viewer-run1.png`. Every run text and source is
escaped before any markup is added. A hostile test feeds script tags, image tags and
`javascript:` links through a stored source, a draft and the tree.

**`python run.py verify RUN [--json]`** (`report/verify.py`) runs offline, with no keys.
It checks six things:
- it re-hashes every capture against its receipt;
- it checks that every stored text is still there at its recorded length;
- it re-checks every quotation with part A's own `find_quote`, and that each figure as
  written sits inside one of its quotations;
- it re-runs every estimate in the register's sandbox;
- it folds the register's log and re-derives every claim id from the claim's content,
  so a figure changed in the log shows;
- it re-hashes every file the register fingerprinted (drafts, frames, reviews, branch
  records and the deliverable).

Exit codes: 0 means everything passed; 1 means something failed, and each failure is
named; 3 means a check could not run on this machine (no sandbox), which is never
counted as a pass; 2 means the folder is not a run.

**Run 1 does not verify clean.** Verify takes about 10 seconds on it:
- 419 captures, 0 changed;
- 466 quotations, 0 missing;
- 2,120 log transactions, and all 455 claim ids match their content;
- 52 fingerprinted files, 0 changed;
- 18 of 181 calculations differ. Their scripts, or helper modules beside them, were
  edited after their claims were filed. Some now report another number; others now ask
  for inputs the claim never named. Two of these claims are cited by draft v11. This
  is a real finding about run 1, not a fault in verify.

**`python run.py tamper RUN [--what source|figure]`** (`report/tamper.py`) works like
this:
1. It copies the run to a temporary folder (a copy-on-write clone on macOS).
2. It verifies the untouched copy.
3. It changes one byte. By default this is a digit of the first cited figure, inside
   the stored source that figure rests on. With `--what figure`, it changes that
   figure's number in the register's log instead.
4. It verifies again and prints only the failures the change added, each one named,
   followed by "Release: REFUSED".

It exits 1 if verify missed the change. On run 1 it takes about 23 seconds: the "500,000"
in the stored White House fact sheet becomes "600,000", and verify names both receipts
of that capture. The second pass reuses a re-run only when the script's code, its
inputs and its documents are byte-identical to the first pass's. A test shows that a
changed helper script is still re-run and caught.

**`python run.py new "QUESTION" [--file F] [--answers A] [--draft D] [--save-draft D]`**
(`src/stemma/onboard/`) makes one model call through `clean_launch`. The call runs on
the pinned binary with the clean configuration, no tools, no plugin and no MCP server.
Its init message must pass the canary (the check that the session started clean)
before the reply is used. The call drafts:
- the question type;
- the broader decision the question may inform;
- each ambiguous term, with its readings, whether both are cheap to research, and
  whether they would change the whole research;
- three to five criteria drawn only from the question.

The user then accepts or edits each piece. The narrow scope is the default (D1). A term
whose readings would change the whole research must be chosen, even in answers-file
mode (D3). Options are recorded only when the user names at least two.

The output is W2's v0.4 `task.json`: `scope`, `criteria`, `options`, and `roles` with
every role on Opus 5.5 at high effort (D10). The question type, the broader decision
(as context only) and the readings taken go in `intake`. On the v0.4 driver, the file
is read back with the driver's own `load_task`. Shown: a live clean call on run 1's
question returned a sensible draft (for example, "on track" read as trend only versus
trend plus pipeline, which the user must choose), and W2's `load_task` from their
branch accepted the result.

## Tests

37 in `tests/report/` and 4 in `hackathon/tests/test_product_commands.py`. They use an
invented run built through the real evidence service and register. They include:
- a tamper case for each check;
- hostile HTML in a source, a draft and the tree;
- a symlinked helper script, which is refused;
- no sandbox, which gives INCOMPLETE, never PASS;
- bad answers files and drafts in the wrong shape, each refused by name;
- run 1 itself, skipped where its folder is absent;
- one live test (`STEMMA_LIVE=1`), run once and passed.

## Findings for others (sent to the orchestrator)

- **W2, the register's re-run.** Since commit 486180d, `read_code` refuses any script
  whose `analysis/` folder holds more than 64 `.py` files. Run 1 holds 223. In a long
  live run, every estimate filed after roughly the 64th script would be refused.
  `verify` copies only the modules a script imports (found with `ast`, never run).
- **W2.** Re-runs start Python with `-I`, which leaves the script's own folder off the
  import path, so a helper module imports only if the script adds its folder itself.
  Run 1's scripts did that; the contract's "copies sibling files" does not say it.
- **W2.** Record the SHA-256 of the script bundle on `derivation_reproduces`. Then verify
  can say "script changed after filing" rather than only "differs".
- **W2 or the orchestrator.** Run 1 has no `usage.jsonl`, so its page shows cost by role
  (from the driver's events) but no tokens. When a run has `usage.jsonl`, the page shows
  tokens and cost by role from it.
- **Limits of verify, stated on its own output:**
  - a verdict line in the log carries no fingerprint;
  - a source replaced together with its receipt cannot be told apart offline;
  - judgments are not re-checked.

## Open

- `stemma report|verify|tamper|new` as subcommands of the `stemma` CLI would need a change
  to W2's `driver/__main__.py`; until then they are `run.py` commands.
- The viewer's lead-response lookup follows W2's planned `draft/response.v<n>.md`; it
  is untested against a real v0.4 run.
