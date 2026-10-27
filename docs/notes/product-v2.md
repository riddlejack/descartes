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

## Codex review, round 1: ten findings, all fixed

Each fix below has a regression test built from the review's own probe.

1. **Markdown could run a script.** A code span inside a link URL closed the `href`
   attribute. The renderer now splits the raw text into tokens and escapes each token
   at the moment it is inserted. It uses no placeholders, and a link is made only from
   a URL that cannot leave a quoted attribute. The page carries a strict Content
   Security Policy: only its own script and style run, allowed by their SHA-256.
2. **Reads now stay inside the run.** All reads go through `report/safefs.py`:
   - paths are checked before anything is opened;
   - no symlink is followed in any part of the path;
   - only regular files with a single name are read, so a hard link is refused too;
   - every read has a byte limit.

   This covers scripts, stored text, receipts and fingerprint paths.
3. to 5. **Changes verify could not see now fail, once a run is sealed.**
   - Verify reads `release/manifest.json`, which W2's `finish_run` will write (format
     `stemma-release-manifest/1`, proposed to W2).
   - Every file must match it. A file added, removed or changed fails, and so does any
     change to the sealed part of the log.
   - `--expect <sha256>` binds the manifest to a digest kept outside the run folder.
   - A log that does not end with a newline fails.
   - Without a manifest the result is UNSEALED (exit 4), never PASS. Run 1 is unsealed.
6. **"Released" now requires a valid release.** The register must have recorded a
   finish, the deliverable's bytes must match the SHA-256 it recorded, and no release
   block or withdrawal may stand. The page no longer claims that nothing was edited.
7. **Malformed records no longer crash.** Every check runs under a guard, and a record
   in the wrong shape becomes a named failure.
8. **Every traversal has limits.** The tree and branch folders are walked iteratively,
   with depth and cycle limits. Each file has a size limit, and so does the walk of
   the whole run folder.
9. **Verify writes nothing.** `report/ledger.py` folds the register's log in memory
   from its bytes. No file is opened for writing and no lock is taken; a test runs
   verify on a read-only copy. Stop-gap: it reaches two private attributes of the
   register until W2 provides a public read-only constructor.
10. **A missing review no longer counts as a pass.** A criterion shows green only when
    all three final reviews of the shown draft exist. If any is missing, the criterion
    is shown as not checked.

The bundle builder `verify.code_bundle` is marked as a stop-gap. It is to be replaced by
W2's public builder, so that verify and `file_claim` re-run the same code.

## Switching to W2's register code (W2 commit 2e4f4f7)

`report/compat.py` looks up W2's public functions at run time. Once the register branch
is merged, verify runs exactly the code that `finish_run` and `file_claim` run:
- `manifest.check` for the seal;
- `manifest.parse_log` for the log;
- `derivation.build_bundle` for bundles;
- `filing.rerun_filed` for a claim filed with a stored bundle. Such a claim re-runs from
  its filed copy, and verify notes when the working copy in `analysis/` has changed
  since.

Until the merge, the report's stop-gaps run instead. Two stop-gaps remain after the
merge: `ledger._memory_world` and `ledger._read_only_register`, which reach two private
register attributes. Each has a STOP-GAP note and should be replaced if W2 adds a
public read-only register constructor.

Shown on a throwaway merge of both branches (never committed): the full suite gave
1532 passed and 19 skipped, mypy and ruff were clean, run 1's verify output was the same
as on this branch, and `stemma verify|report|new` reach these entry points.

## Codex recheck: five more fixes (H1 to H3, M1, M2)

- **H1, only sealed bytes count.** With a release manifest, the viewer and verify fold
  only the log's sealed prefix, and read only the sealed prefix of
  `register/checks.jsonl`. Anything appended after the seal fails the seal check and is
  never read, so a forged verdict, finish record or revocation decides nothing. A
  manifest that does not seal `checks.jsonl` gives INCOMPLETE, never PASS; W2's
  manifest now seals it.

  W2's new `Register.read_only` folds the whole log file, not the sealed prefix, so
  `report/ledger.py` keeps its own in-memory snapshot.
- **H2, the tamper demo writes only inside its copy.**
  - It copies the whole run except what is only operational, and refuses any link or
    special file before it touches anything.
  - It refuses to run when the untouched copy already fails verify.
  - It takes the path it changes from a checked SHA-256, and writes through the
    register's confined writer.

  **Run 1 is now refused, for two reasons:** a worker left a symbolic link at
  `branches/b3-public-policy/testdocs/`, and its calculations already fail. The demo
  needs a clean run.
- **H3, `stemma new` follows no link when it writes.** It creates the file exclusively,
  and a dangling link counts as an existing file. `--force` never replaces a link, and a
  folder reached through a link is refused.
- **M1, side files are read safely.** `checks.jsonl` and `refused_corrections.jsonl` are
  read through `safefs` and a strict parser. A calculation input too large for a float
  is a named failure.
- **M2, the driver checks the task before it is written.** `stemma new` validates with
  the driver's own `load_task` first. It refuses when the driver predates task.json
  v0.4: on this branch alone it now refuses, and after the merge it writes.
- Folder and file names may now start with a dot (`.claude`), but `.` and `..` are
  still refused.

## On v2-integration: W2's code imported directly

The branch is rebased onto `v2-integration`. `report/compat.py` and every fallback are
gone: verify imports `manifest.check`, `manifest.after_seal`, `manifest.parse_log`,
`derivation.build_bundle` and `filing.rerun_filed` directly. A function that is missing
now fails loudly at import, so verify can never quietly run other code. The test fixture
seals with `manifest.seal` (format `stemma-release-manifest/2`).

One stop-gap is left, and it is marked: `report/ledger.py` folds the sealed bytes in
memory. W2's `Register.read_only` reads the whole log file, and a release must be read
from its sealed bytes only.

## The last stop-gap removed: Register.from_sealed

`report/ledger.py` now opens the register with W2's `Register.from_sealed`. It folds
exactly the sealed bytes of the log and takes the sealed bytes of `checks.jsonl`. It
also takes the bytes of `refused_corrections.jsonl`, all read through `safefs`, and it
refuses every write. No private register attribute is touched any more.

A manifest that does not seal `checks.jsonl` is now a failure: format
`stemma-release-manifest/2` always seals it.

## The head-to-head harness for run 3 (hypothesis H5)

Two modules, in `hackathon/headtohead/`:

- **`bare.py`, the bare arm's launcher.** Not launched yet; the orchestrator launches it
  after Stemma's run ends, so the two never share a usage window.
  - It takes run 3's question, named options, onboarding answers and as-of date from
    `tests/e2e/run3_demo.py` itself, and `--question-file` replaces the question the
    same way that launcher does.
  - It launches one session through `clean_launch`, with the canary checked: Opus 5.5 at
    high effort, no plugin, no MCP server, no slash commands.
  - Its tools are WebSearch and WebFetch, and Bash, Read and Write confined to its own
    folder, so that it can calculate. Bash runs in the workers' sandbox: no network,
    reads denied from the filesystem root and re-opened only for its folder and what
    the shell and the system Python need (nothing of this repository), and writes only
    in its folder. The Read and Write tools are allowed only inside the folder. Run 3's
    Stemma folder, the Claude and Codex configurations and SSH keys are denied by name
    besides.
  - A scope canary runs before the real session, in the same folder with the same
    settings. It is told to read a marker file outside the folder with the Read tool
    and with `cat`, and to reach the network with `curl`. Code requires that it tried
    all three, and that neither the marker's random token nor the reached page's text
    appears in its stream. Otherwise the arm is refused and the real session never
    starts.
  - The scope canary is the only proof that headless Read is denied outside the folder;
    it is checked at launch, never in a unit test.
  - **Stated deviation from a plain user:** the prompt asks the bare arm to cite a
    source for every figure, which a plain user may not ask. Without it, the citation
    counts would measure nothing. The exact prompt is saved with its answer.
  - It writes prompt.txt, launch.json, stream.jsonl, answer.md, usage.json and
    outcome.json into a new folder under the `--out` folder (required since the
    pre-public pass; it was a private runs folder before).
  - `--dry-run` creates nothing.

- **`score.py`, the scorer.** It counts both answers by the same rules, which are also
  written into `score.json`:
  - A figure is a number token, found by the register's own tokenizer.
  - A figure's sources are the ones cited in its own sentence.
  - Every cited page is fetched now through the evidence service, with the cache off,
    and kept as a capture.
  - Code decides whether a source resolves and whether it contains the figure.
  - A cross-family judge (Codex `gpt-6-astra`, read-only) decides two things: which
    figures a source contradicts, and which gaps and limits an answer admits. Each of
    its answers counts only when code finds its quotation. Every prompt and reply is
    recorded.
  - It writes `summary.txt`, which gives the counts in sentences, because the owner
    reads no tables.

**Live trial: run 1's v11 draft against an invented bare answer.**
- It took 22 minutes: 81 captures and 72 judge calls.
- Stemma: 386 figures, 150 contained in a cited source, 80 of 80 cited sources resolve,
  67 gaps and limits admitted.
- The invented bare answer: 6 figures, 1 contained, 2 of 3 sources resolve.

**Two rules came out of the trial.**
- *Drift is not a contradiction.* Both "contradictions" the judge found on Stemma's side
  were the AFDC station count, which has moved since the run (268,116 then, 268,137
  now). The judge is now told that a quantity that changes over time and differs only
  because the page is newer is not a contradiction.
- *Calculated figures are counted apart.* No page can contain an estimate, so figures
  whose cited claim is a recorded calculation are counted separately. That count is
  Stemma's only, outside the shared comparison.

## Codex review of the head-to-head harness: twelve fixes

Each fix below has a test named after it, built from the review's probe.

**The five high-severity findings.**
1. *An unrelated number counted as support.* Containment is now only a diagnostic.
   Every figure taken from a source is judged against every resolved source it cites,
   whether code found its number there or not.
2. *A missing judgment counted as no contradiction.* Each figure the judge is asked
   about needs exactly one valid verdict. A missing, duplicated or invalid verdict, an
   empty reply, or an unfindable quotation leaves the figure unjudged. Unjudged figures
   are counted and reported, never taken as zero.
3. *Text could steer the judge.* The answer's and the source's text now sit between
   random markers, and the prompt says to ignore any instruction inside them. Arm names
   never reach the judge, and each call has its own folder holding only its own
   document. Quotations are still checked by code.
4. *A failed bare run was accepted as an answer.* Now an answer counts only when the
   canary passed, the session exited 0 without being stopped, its result is a success
   and not an error, and the answer has text.
5. *Computed figures counted against sources.* The judge now asks one question of both
   answers: was this figure taken from a source or computed? Computed figures are left
   out of the source counts. Stemma's computed figures are re-run as verify does; the
   bare answer has no recorded calculations to re-run.

**The seven medium-severity findings.**

6. *Formatting changed the figures.* Numbers are parsed into value, sign, unit, scale
   and stated precision. Years are recognised by their context, and reference lists are
   left out.
7. *Citations were lost in sentences and tables.* Citations are now resolved per block,
   before sentences are split, and each figure takes the markers that follow it.
8. *Some of Stemma's citation routes were dropped.* A claim's provenance now also
   follows its derivation's documents, and unresolved routes are reported.
9. *An empty page counted as resolved.* A source resolves only with an eligible status
   and some text, and the extractor's flagged ranges are used in every quotation check.
10. *One gap could count several times.* Each admitted gap is an answer item, counted
    once.
11. *A silent session could run on forever.* A reader thread enforces the start-up and
    session deadlines, then terminates and kills the process with bounded waits.
12. *Tools were limited but the filesystem was not.* The bare arm's tools are confined
    and proven at launch by the scope canary. That the harness process itself runs as
    the owner is a stated limit.

## Codex re-check of the head-to-head harness: the eight remaining findings

Review: a private review record (W3-headtohead-recheck), not in this repository. One
test per finding (`test_recheck_<n>_...` in `hackathon/tests/test_headtohead.py`); all
twelve new tests failed on 9628aa3 before the fixes (the canary and stuck-child tests
partly because their fake now speaks the new six-step canary prompt).

1. **Calculation credit.** A computed Stemma figure counts as reproduced only when a
   claim it cites re-runs and its recorded value is the number shown (sign, unit, scale,
   at the shown precision). "We compute 999" over a claim recorded as 30 gets none, and
   says why.
2. **Partial source failures.** A figure unjudged against any resolved cited source is
   counted (`taken_figures_with_a_cited_source_unjudged`), even when another source
   supported it, beside `taken_figures_judged_against_every_resolved_cited_source`; the
   summary says how many.
3. **The judge.** It sees no citation markup (claim tags, links, URLs, reference
   markers removed: `blind`). It runs inside a macOS sandbox profile (`judge_profile`):
   no read or write of the home folder, /Volumes or the temporary folders except its own
   empty call folder and Codex's folder, in which the owner's `AGENTS.md`, sessions and
   history stay shut. Tested by the operating system with a planted file, and live: one
   scoring with a source telling the judge to `cat` an outside file; the judge judged
   the figure and the file's token appeared in no reply.
   - macOS refuses a sandbox inside a sandbox with any deny rule, so Codex's own command
     sandbox cannot start in this one. The judge therefore gets the source text in its
     prompt (on stdin) and needs no command. A source over 400,000 characters is given as
     excerpts around the figures' numbers and long words, and such figures are counted.
   - Running Codex with its own sandbox switched off was refused by this session's
     permission checks; not pursued.
   - Found on the way: every earlier judge call (the 87 in the live re-trial) ran with
     the owner's `~/.codex/AGENTS.md` loaded. Now it is shut. Codex's skills and plugins
     from `~/.codex` still load unless `STEMMA_CODEX_HOME` names a clean, signed-in Codex
     folder (made like the clean Claude configuration); the summary says which ran.
4. **Parser.** "of 2000 benches", "(2000) benches" and ".5%" are figures; "[1] reports 42
   benches." is a sentence, not a reference definition (that needs a colon, a URL or a
   sources heading). A year after "of", "to", "from" or in brackets is a date unless a
   counted word follows it; "Smith (2021)" and "the 2021 American Jobs Plan" are dates.
5. **Citations.** A figure takes markers in its own sentence, or right after it with
   only spaces and punctuation between, or earlier in its sentence; never across another
   sentence's words. Only a table row gives every marker in the row to its figures.
6. **Scope canary.** Six calls, each needing its result: the marker read by Read and by
   Bash and curl must be refused; a control file inside the folder read by both tools
   and the page fetched by WebFetch must succeed (so a refusal is the confinement's).
   The canary session itself must exit 0 with a successful result. Not watched live:
   whether this harness marks a refused Bash command `is_error`; if not, the canary
   refuses the arm and names the step.
7. **Forced stop.** A child that closes its output but does not exit is stopped and
   recorded as stopped, so it is never eligible.
8. **Gaps.** Any invalid entry in the judge's gap reply marks the count not fully
   judged, and the summary says so.

Checks: 46 head-to-head tests pass; full suite 1685 passed, 19 skipped; ruff and
mypy --strict clean on `src` and `hackathon/headtohead`.
