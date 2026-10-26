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
- **Zetaris itself is not installed yet.** The owner downloaded the bundle on 26 Oct;
  it is unzipped, configured and passes `preflight.sh` except the registry sign-in,
  which only the owner may run. After it, one command finishes the install
  (`bash ~/.private/zetaris/finish-install.sh`; see `hackathon/zetaris/README.md`),
  then the owner creates the REST API key in Zetaris's interface. So Zetaris's own checks (`verify.sh`, the Pikachu query, the REST smoke
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

## The hook in part B's register (contract v0.2.7, section 5a; made at merge, 26 Oct)

Merged `main` (A, B, C) at 0f65706. `Register._inputs()` in `register/store.py` returns
B's own lookup unless `STEMMA_DATALAYER=zetaris`; then it wraps it in
`stemma.datalayer.input_lookup` (claims from the register itself), turns
`InputMismatch` into B's `Refused("Refused: input_mismatch: ...")`, and writes
`datalayer_unavailable` to the run's `events.jsonl` (source `register`). Any failure of
the data layer, a bad setting included, files the claim with the register's numbers.
`tests/datalayer/test_register_hook.py` runs the real `Register`: unchanged without
Zetaris mode; unreachable (a closed loopback port) files with the register's numbers and
writes both events; a one-input difference refuses without naming the numbers; matching
numbers reach the re-run from the data layer. A test also shows D's SEC re-read agrees
with A's parser on the same bytes. Full suite under every extras group: 978 passed,
6 skipped (live tests); ruff and mypy --strict clean.

Not done, outside the hook's scope (B's files): `datalayer_unavailable` is not yet in
`driver/events.py`'s `LOUD` set, so the status page does not list it as an alert; and
`mcp_server.py` does not yet call `stemma.datalayer.tools.register_tools`, so
`query_data` is not served. One line each.

### The original proposal

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

- Zetaris getting-started repository: commit 6a2fbaa (22 Oct 2026). Platform bundle:
  zip sha256 `14c6a811a140378bec1749ceffd7fa123bafb2f178545b26354e8f9433b413d3`, images
  `2.4.3-v2.4.3.1-hackathon`, JDBC driver 2.4.3.1; not yet started (registry sign-in).
- The bundle's `verify.sh` queries through the interface's proxy
  (`/api/proxy/query/sql/start`, body `select` and `pageLimit`) and gets rows back as
  objects whose values are strings (`"seven":"7"`). The client here uses the direct REST
  route the getting-started helpers use (`/sql-editor/sqls/run` on port 8888) and reads
  rows as lists or objects, parsing numbers from text; which route and shape the live
  instance gives is the first thing the live test settles.
- Meterless repository: commit 1742dec (19 Oct 2026); Runtime contract 0.90.0.
- Postgres 16.13, image `postgres@sha256:71e27bf6…3ddf`; pg8000 1.31.5.
- Docker on Colima (4 CPUs, 7.7 GiB visible to Docker, 30 GiB disk, aarch64, amd64 by
  emulation); Docker Compose 5.1.3 (Zetaris tested 5.5.1), exposed as a `docker compose`
  plugin by a link in `~/.docker/cli-plugins/`.

## Cross-family review of 5892002 (26 Oct), fixed by principle

1. **query_data isolation is structural now, not by parsing SQL.** The agent's SQL never
   reaches Zetaris, Postgres or the run's SQLite file. Code-written SELECTs (the only text
   Zetaris receives) fetch this run's rows of the tables the query reads into a fresh
   in-memory SQLite database holding nothing else, `query_only` on; the agent's SQL runs
   there. `SELECT ... FROM main.claim` and every other spelling can see only this run's
   rows. The SQL checks stay as a second line. Zetaris's datasource connects as a
   Postgres role that can only read (`ensure_reader`; shown live: insert, update,
   delete, create all refused, even after `SET default_transaction_read_only = off`).
2. **The fallback never raises.** A failed log write is recorded on the event
   (`log_error`); a failed event write goes to stderr; the register's number is used.
3. **No response body in any exception or log.** Errors keep the status, length and a
   hash. Every string in a log entry is scrubbed of registered keys, tokens and
   passwords (Zetaris key, Postgres passwords, Nightshift token), `password "..."`
   options and bearer tokens.
4. **Exact comparison.** A value read back must have the canonical text it was filed with
   and the same bits; `+0.0` against `"-0.0"`, `8.0` against `"8"` all refuse.

Each has a test that fails on 5892002 and passes now. Suite: 994 passed, 5 skipped.
Per-run Zetaris identities (a Zetaris user per run) were not needed for isolation once
no agent text reaches Zetaris; noted as an option, not built.

## Cross-family recheck of 1e2df45 (26 Oct), fixed by principle

1. **One snapshot.** The export reads every claim through the register's new
   `claims_snapshot()` (one log position, under the writers' lock); every table of a
   sync, with a fresh generation in `sync_state`, is written in one transaction. SQLite
   loads for `query_data` happen in one read transaction; Zetaris loads are accepted only
   if the generation read before and after agree (five attempts, then a typed error).
   Tests race a sync between table reads on both paths.
2. **Nothing from a response body, keys included,** goes into an exception or log:
   errors carry what was expected, the status, length and hash. (Reproduced on 1e2df45:
   the key leaked; now it does not.)
3. **Provisioning decides by structured state:** the run's own record of completed steps
   (`datalayer/provisioned.json`) and the rows of `SHOW DATASOURCES`, never error text;
   `QueryError` carries the HTTP status. Tests: twice is a no-op, a half-done provision
   resumes without repeating, an existing datasource is found by listing, an "already
   exists" message is not swallowed.
4. **NaN and infinities refuse** outright; finite values compare by text and bits.
   (Reproduced on 1e2df45: `(nan, "nan")` passed.)
5. **A fresh role per run** (`stemma_ro_<key>`), SELECT on that run's schema only, never
   reused (a pre-existing role of that name refuses), and checked before Zetaris gets it:
   no superuser, createrole, createdb, replication, bypassrls or inherit; no membership;
   no ownership; no write, truncate, reference or trigger privilege on any table; no
   SELECT outside its schema; no CREATE in any schema; no CREATE or TEMPORARY in, or
   CONNECT to, any other database; no sequence update. Live against the container: the
   role reads its schema and is refused reading another run's schema, every write,
   create, alter and temp table, also after `SET default_transaction_read_only = off`;
   six planted grants are each found. The live check found two real gaps, both closed:
   the container's `postgres` database let PUBLIC connect and make temp tables (now
   revoked on every database), and a sequence check that errored on non-sequences.
   `pg_catalog.pg_settings` is excluded from the write check by name: it is SET's
   interface, not stored data.

Each run now has its own datasource and semantic layer in Zetaris instead of one shared
model; the old shared reader role and public tables from earlier tests were dropped.
Suite: 1017 passed, 5 skipped (Zetaris and web live tests); ruff and mypy clean.

## Stage 3, without the recorded run yet (26 Oct)

Branch `claude/part-d-stage3`, from main at 2afea16.
- **`run.py`**, one entry point: `ask`, `status`, `logs`, `replay`, `conformance`, `record`;
  with no arguments and `SAMPLE_MODE=true`, it replays the bundled sample. Runs from a
  fresh clone without installing (it puts `src/` on the path).
- **Replay** (`hackathon/replay.py`): a pack is copies of a finished run's readable files
  plus a manifest of SHA-256 and sizes. Replay verifies every file (changed, missing,
  unlisted and path-escaping files are typed errors), copies them into a fresh folder and
  writes `REPLAY.json` labelling each item `replay` with its hash. Lines appended later (a
  live query in a replayed log) are labelled live; a replayed file whose recorded bytes
  changed is an error, never relabelled.
- **The judge's log** (`hackathon/judgelog.py`): models behind each request, World Model
  transactions and register calls, every data-layer query, the answer, each line
  `[replay sha:...]` or `[live]`, with a header that says REPLAY when it is one.
- **Live in sample mode** (`hackathon/sample.py`): the conformance suite (8/8, shown) and
  the data layer over the replayed evidence.
- **Dockerfile** (`python:3.12.13-slim-bookworm` with Node 24.14.1 copied from its image):
  built here; in the container the conformance suite passes 8/8 and a mounted pack replays
  with the same labels. `.dockerignore` keeps `.env`, `.venv`, `.git` and runs out.
- Not done: the bundled sample pack (waits for B's walking-skeleton run folder);
  Nightshift receipts appear in the log only once something calls Nightshift (stage 2).

### Review of fd048fd, fixed by principle
1. **Replay is the default label; live is never inferred.** A line read from a file is
   `replay sha:` (replayed bytes), `recorded sha:` (a folder without a marker, such as a
   pack's own files) or `appended after replay`; only steps this process ran and recorded
   as it ran are `[live]`. Paths are canonicalised before every lookup; a manifest path
   that is not already canonical (`./x`, `a//b`, `a/./b`) is refused.
2. **Manifest fields are hostile identifiers:** the run id must be a plain identifier,
   and every destination is checked to stay inside its folder after resolution
   (`x/../../outside` and four other forms tested).
3. **No env file enters the image:** `.dockerignore` drops `.env` files at any depth
   (`**/.env`, `**/.env.*`, `**/*.env`, keeping `.env.example`), and the Dockerfile copies
   only named paths. Shown: canaries planted at four depths are absent from the built image.
4. **Recording refuses every credential form**, with a test per form: `KEY=value`,
   `export ...TOKEN=`, YAML `PASSWORD:`, JSON `"api_key": "..."`, a SQL `password "..."`
   option, bearer headers, and known key formats (Anthropic, OpenAI, NVIDIA, GitHub, Slack,
   AWS, private keys). Masked values (`***`, `<set>`), numeric token counts and ordinary
   words record. Verification refuses a pack that carries one.
5. **Kept:** `datalayer/providers.jsonl` (the Nightshift receipts) is recorded and shown;
   each conformance run is saved (in the replay folder and under `runs/conformance/`), and
   a log written later shows the saved result labelled `recorded <time> sha:`.

## Zetaris live (26 Oct)

Installed from the owner's bundle (images 2.4.3-v2.4.3.1-hackathon). The interface is on
port 3300: the owner's own app holds 3000. Zetaris's own checks all pass:
- `verify.sh`: 9 of 9, ending with a query through ui, api, driver and Spark;
- the REST smoke test: `SELECT 1` through `POST /sql-editor/sqls/run` (rows come back as
  lists of text);
- the getting-started Pikachu recipe: `static` not hidden, `lightning-rod` hidden, as
  recorded upstream.

**Found live, and changed:** Zetaris's JDBC table registration finds tables only in a
database's `public` schema (`REGISTER DATASOURCE TABLES` reported "0 injected" for a run's
own schema while `PREVIEW DATASOURCE SQL` read it). So each run now has its own Postgres
**database** (tables in its `public`), not a schema: stronger isolation (Postgres cannot
read across databases) and what Zetaris registers. The role checks moved with it: the run's
role may connect only to its own database (checked across the cluster), and the table
checks run inside that database. Shown live: it can read its tables and is refused
connecting to another run's database or `postgres`, and every write, create, alter and temp
table; seven planted grants are each found.

**The contract-5a round trip, live** (`tests/datalayer/test_live.py::
test_invented_run_round_trip_through_zetaris`, repeatable; it removes everything it made):
sync of the invented run (receipts, five claims with verdicts and a correction) into its
own database; provisioning (fresh role, datasource, a Unified Semantic Layer of ten tables
with primary and foreign keys, all activated; `LIST DQ USL` lists the key rules); values
through the semantic layer equal to SQLite's bit for bit; `query_data` through Zetaris
returning the verdict rows with their claim ids; each derivation input read back through
Zetaris with the register's exact bits.

**The fallback, live:** with Zetaris's API stopped, each derivation input took the
register's own number (bits equal) and raised `datalayer_unavailable`; `query_data`
answered from SQLite and said "Zetaris was unavailable; answered from SQLite".

What a judge sees in `datalayer/queries.jsonl` (one line per query; times vary):

    {"engine": "zetaris", "purpose": "statement", "ok": true, "sql": "CREATE DATASOURCE stemma_r... OPTIONS (... password \"***\" ...)"}
    {"engine": "zetaris", "purpose": "statement", "ok": true, "sql": "COMPILE USL IF NOT EXISTS evidence_r... DEPLOY NAMESPACE lightning.metastore.stemma DDL ..."}
    {"engine": "zetaris", "purpose": "query", "ok": true, "rows": 2, "ms": 479, "sql": "SELECT claim_id, number, number_repr FROM lightning.metastore.stemma.evidence_r....claim WHERE run_id = '...' AND (claim_id LIKE '391d...%' OR ...) ORDER BY claim_id"}
    {"engine": "zetaris", "purpose": "query_data_load", "ok": true, "rows": 2, "sql": "SELECT run_id, claim_id, ... FROM lightning.metastore.stemma.evidence_r....claim WHERE run_id = '...' ORDER BY run_id, claim_id"}
    {"engine": "memory", "purpose": "query_data", "backend": "zetaris", "rows": 2, "sql": "SELECT c.claim_id, ... FROM claim c JOIN claim_support s ..."}
    {"engine": "zetaris", "purpose": "fallback", "level": "loud", "fallback_to": "sqlite", "reason": "zetaris_unavailable: cannot reach http://localhost:8888/...: Connection refused"}
    {"engine": "datalayer", "purpose": "datalayer_unavailable", "level": "loud", "claim_id": "391d...", "mode": "zetaris", "error": "ZetarisUnavailable: ..."}
