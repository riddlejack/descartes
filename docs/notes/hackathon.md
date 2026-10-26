# Part D: the hackathon adapters

Builder D's notes. Stage 1 is the data layer (Zetaris) and the provider client
(Nightshift, built against fakes). Stage 2 is the pre-registered test of Claude through
Nightshift, written below before any result exists. Stage 3 (sample mode, `run.py`,
Dockerfile, logs) waits for the orchestrator.

## Where things stand (26 October)

**Works, shown by tests:**
- The evidence-derived tables, defined once (`src/stemma/datalayer/schema.py`) and
  rendered for SQLite, Postgres and a Zetaris Unified Semantic Layer. Nine tables: the
  capture manifest, claims, their support, derivation inputs, entities, verdicts and
  supersessions, as-filed SEC facts, and table cells from captured documents.
- Export of a run folder into those rows, sync into SQLite (always) and Postgres
  (Zetaris mode), idempotent and atomic per run. A race test runs four processes
  syncing the same run at once, released together by a barrier; the result is one
  consistent copy.
- `EvidenceValues`, part B's `ClaimValues` interface, over either engine with the
  same SQL text. The numbers come back bit for bit (compared as hexadecimal floats).
  Each number is stored with its exact text form, and a read where the two disagree
  fails (`ValueMismatch`); a test tampers with a stored number to show it.
- The Zetaris REST client (`POST /sql-editor/sqls/run`, bearer key, `X-Org-ID`, a fresh
  `X-Request-ID` per call). Every query is logged in `<run>/datalayer/queries.jsonl`
  with its text, row count, time and ids; passwords are redacted before the log.
- The live Postgres container (`hackathon/zetaris/compose.yaml`) is running; a live test
  shows it holds exactly the rows SQLite holds, table by table.
- The Nightshift Runtime client (contract 0.90.0), tested against an in-process fake
  and a loopback fake server: URL normalisation, the real token (the `rt-local`
  placeholder refused), the required headers, status from `GET /v1/models`, the
  contract's Auto Detect, and a receipt per call naming the route asked for and the
  model the answer names. The token and message text never reach the log.
- **Derivation inputs (contract v0.2.7, section 5a):** `input_lookup` wraps the
  register's own lookup. The register resolves the id and owns the number; the re-run
  gets the data layer's copy, read through Zetaris in Zetaris mode. A difference of a
  single bit refuses (`input_mismatch`; compared as hexadecimal, so -0.0 is not 0.0),
  and so does a copy whose number and text form disagree. If the data layer cannot
  answer (Zetaris or Postgres unreachable, a failed sync or query), the register's
  number is used and `datalayer_unavailable` is raised loudly through an alert callback
  (for the run's events), the query log and the logger. Tests cover each case.
- **`query_data(sql)`**, the read-only MCP tool, exposed by `register_tools(server)` in
  `src/stemma/datalayer/tools.py` as part A does. The SQL is prepared in SQLite against
  an empty copy of the schema under an authorizer that allows only reads of the nine
  tables and twenty plain functions, so writes, other tables (`sqlite_master`),
  `load_extension`, `random`, `printf`, Spark's `reflect`, recursion, comments and a
  second statement are all refused (twenty hostile cases tested). Each result must
  carry a receipt or claim id. The query then runs scoped to this run by a leading
  `WITH` that replaces each table with this run's rows, because the shared Postgres
  holds several runs. At most 500 rows, with truncation said; logged with text,
  backend and row count; a loud SQLite fallback when Zetaris is unreachable, said in
  the answer.
- **Host writes follow no symlink** (contract section 6): `datalayer/` is walked one
  folder at a time with `O_NOFOLLOW` descriptors, log lines are appended `O_NOFOLLOW`,
  and SQLite's file and journals are checked not to be links before each connection.
  Hostile tests plant links to the folder, the database, a journal and the log. The
  race test found a real race (two writers creating `datalayer/` at once), now fixed.
- 107 tests pass; `ruff` and `mypy --strict` are clean on `src/stemma/datalayer` and
  `src/stemma/providers`.

**Not yet shown:**
- **Zetaris itself is not installed.** The platform bundle needs the owner's Zetaris
  account (registration, terms, download, registry login); the orchestrator has the
  exact steps. So Zetaris's own checks (`verify.sh`, the Pikachu query, the REST smoke
  test), the semantic-layer setup, and the live test that registers an invented run
  and reads it back through Zetaris have not run. The equality test so far runs the
  Zetaris code path against a fake that answers from SQLite; that proves the client
  and the number handling, not Zetaris. The live version is `tests/datalayer/test_live.py`.
- The Lightning SQL dialect details are taken from Zetaris's reference and recipes, not
  yet from a run: the USL column types (`string`, `bigint`, `double`), whether
  `COMPILE USL` accepts composite keys, how the datasource names Postgres tables, the
  REST answer's row format, and whether an activated USL table sees new Postgres rows
  without a refresh. Each is a short fix once a live run shows it.
- Nothing from part B is integrated yet (B is not on `main`); see "What part B would
  change" below.

## Why Postgres is the route Zetaris reads

Zetaris cannot read a laptop file. The candidates were a database it registers over
JDBC, an S3-compatible store, or its REST-table feature over a small local endpoint.

- **Postgres over JDBC (chosen).** It is Zetaris's core use, federating a database it
  does not copy, and the same slot a firm's own databases would fill. Typed columns,
  primary keys and transactions mean a sync is atomic, and the semantic layer's
  foreign keys check real keys. The writer is pg8000 (BSD-3-Clause, pure Python). The
  image is already on this machine and runs natively on arm64.
- **S3-compatible storage (rejected).** MinIO is AGPL, which the licence rule excludes.
  Files would need a format (CSV loses types; Parquet adds pyarrow), and every sync
  rewrites objects with no transaction.
- **REST tables (rejected).** Zetaris's own notes say a REST table calls its source on
  every query (one count took 49.7 s uncached), JSON arrives map-shaped and needs
  flattening, and Stemma would have to run a server for Zetaris to poll.

## What Zetaris adds, plainly

For the numbers in an answer: nothing. The equality test exists to show that the
Zetaris path and the SQLite path give the same numbers, so the research result does not
depend on which one ran. What Zetaris does add: one SQL access point where the
evidence tables sit beside a firm's own databases and official statistics, joinable in
one query; and the semantic layer's data-quality rules (each foreign key is a rule, for
example that every support row cites a receipt in the manifest). That is the design's
position (DESIGN-v1 section 7), unchanged.

## What part B would change (accepted as contract v0.2.7, section 5a; D makes it at merge)

B's `file_claim` re-runs a script in a sandbox with no network, and an agent's own Bash
has no network either (B's probe notes: `allowedDomains: []`, raw sockets refused). So
no recorded script can query Zetaris itself. The one place Zetaris can carry the inputs
is the host side, in the MCP server process:
1. **Derivation inputs through the data layer.** The register's input lookup
   (`Register._lookup_input`, passed to `filing.run_checks`) would read each input's
   number with `stemma.datalayer.values_backend(run_dir)` when `STEMMA_DATALAYER=zetaris`,
   after a `stemma.datalayer.sync(run_dir)`. The inputs file the sandboxed script reads
   would then hold numbers that came from Zetaris queries, logged with their text.
2. **Sync after each register write**, in the same process (a call where B rebuilds its
   SQLite index), so Zetaris always holds the current register.
3. **Optional:** a read-only MCP tool that runs one `SELECT` over the semantic layer, so
   an agent can show "a chain of queries the user can see". It needs a read-only Zetaris
   user and a single-statement check.

Done in v0.2.7: `datalayer/` is tool-only, part D is in section 1, `query_data` is in
the tool list. Left for the merge: the hook in B's `register/store.py` (D's, by the
contract); passing B's event writer as `input_lookup`'s `alert`, and
`datalayer_unavailable` in B's `driver/events.py` `LOUD` set so the status page shows it
(B's file: needs the orchestrator's word); B's server calling
`stemma.datalayer.tools.register_tools`; the data-layer settings in `.env.example`.

`query_data` and the dialects: only SQL valid in SQLite passes the check, and Zetaris
runs Spark SQL. Most plain queries mean the same in both, but not all (`round` breaks
ties differently, for one). That is acceptable because a figure taken from a row is
filed as a claim with its own support, and the claim checker judges it.

Also for A: `stemma.datalayer.export.sec_facts_from_bytes` repeats about twenty lines of
A's `facts_from_concept_json`, with A's `as_of` filter, because A is not on `main`. Once
A lands, either D imports A's function or a test shows both agree on the same bytes.

## Stage 2: the pre-registered test of Claude through Nightshift

Written 26 October, before Nightshift is installed and before any result. Nothing below
changes after the first run except to record what happened.

**Question.** Does a Claude call routed through Nightshift return everything the same
call returns directly? "Everything" means structure, not wording: wording differs
between any two runs and is not judged.

**Setup.** Nightshift with the owner's Anthropic API key in its Providers tab and Claude
Opus 5.5 on one lane. If Nightshift does not offer Opus 5.5 (its public catalogue lists
Opus 5, Sonnet 5, Fable 5 and Opus 4.8), the test stops there: the model rule allows
only Opus 5.5 and Fable 5.1, so the route is lossy on model identity. The direct path
uses the same key against Anthropic's Messages API. Claude Code speaks Anthropic's
format and Nightshift speaks OpenAI's, so the Claude Code task (T7) goes through a
bridge (LiteLLM's proxy, MIT), after first checking whether Nightshift's own Anthropic
adapter accepts Anthropic-format requests.

**Tasks** (invented material only; each run three times per path):
- T1, tool calls: two tools, a task that needs two parallel calls and a second turn
  with their results. Compared: tool names, argument JSON, call ids paired with results,
  parallel calls kept parallel, stop reasons.
- T2, long reading: an invented document of about 150,000 tokens with three planted
  sentences; the answer must quote each exactly. Compared: all three quotes found, input
  token counts within 1 per cent, no truncation.
- T3, extended thinking: a reasoning task with a thinking budget, then a tool call and a
  second turn. Compared: thinking blocks present (not summarised away) with their
  signatures, and the second turn accepted.
- T4, citations and document blocks: a document block with citations on. Compared:
  citation blocks present with their character spans.
- T5, model identity: the response's model field and Nightshift's receipt both name
  `claude-opus-5-5`.
- T6, prompt caching: the same long prefix twice with `cache_control`. Compared: cached
  input tokens reported on the second call. Recorded as cost, not as loss.
- T7, Claude Code: one headless worker-style session with two tool calls, pointed at the
  bridge. Compared: the session completes, and its transcript has the same block types
  as the direct session's.

**Decision rule, fixed now.** Lossless only if, in every run of T1 to T5 and T7, nothing
the direct path returns is missing, renamed or changed in type, and the model is Opus
5.5 by both the response and the receipt. One failure anywhere means lossy.
- **Lossless:** the driver gets a provider switch that routes every call through
  Nightshift, and every call's receipt names the model.
- **Lossy:** the research path stays direct, written down plainly. Nightshift serves the
  demo's request and, if the owner allows the cross-family check (design decision 1),
  the evidence auditor of switch S6 through a frontier OpenAI model on a Nightshift
  lane.

## Versions recorded

- Zetaris getting-started repository: commit 6a2fbaa (22 Oct 2026). Platform: not
  installed.
- Meterless repository: commit 1742dec (19 Oct 2026); Runtime contract 0.90.0.
- Postgres 16.13, image `postgres@sha256:71e27bf6…3ddf`; pg8000 1.31.5.
- Docker on Colima (4 CPUs, 7.7 GiB visible to Docker, 30 GiB disk, aarch64, amd64 by
  emulation); Docker Compose 5.1.3 (Zetaris tested 5.5.1), exposed as a `docker compose`
  plugin by a link in `~/.docker/cli-plugins/`.
