# Running Stemma 2: sample mode, live runs, logs and Docker

## Sample mode (no credentials)

```bash
SAMPLE_MODE=true python run.py
```

This replays a recorded real run. It checks every recorded file against the SHA-256 in
the pack's manifest; a changed byte stops it. The judge's log it prints marks every line
from the recording `[replay sha:<hash>]`, and says at the top that the run is a replay. A
line read from any file is never marked live: in a folder without a replay marker it is
`[recorded sha:<hash>]`, and bytes added to a replayed file afterwards are
`[appended after replay]`. Only steps this process runs itself are marked `[live]`:
- World Model's own conformance suite, unmodified, against the register's engine;
- the data layer over the replayed evidence: a sync into SQLite (or Zetaris when
  configured) and one read-only `query_data` query.

It needs Python 3.12 and Node (for the conformance runner); no Zetaris, no Nightshift, no
API keys. The pack it replays is `hackathon/sample/pack/` (added once the recorded run
exists); `python run.py replay PACK` replays any other pack.

## Everything else `run.py` does

- `python run.py ask "QUESTION" [--as-of YYYY-MM-DD]`: a live run, through part B's driver
  (needs the Claude Code settings in `.env.example`; refused while `SAMPLE_MODE=true`).
- `python run.py status RUN_DIR`: what is running, what landed, what waits on you; a replay
  folder says it is a replay.
- `python run.py logs RUN_DIR`: the judge's log of any run: the model behind each request
  (the driver's launch and verification events, and Nightshift receipts), each World
  Model transaction and register call, each data-layer query (Zetaris or SQLite, with row
  counts), and the answer.
- `python run.py conformance`: the World Model conformance suite, live.
- `python run.py record RUN_DIR PACK_DIR`: record a finished run as a pack. Only what a
  reader needs is recorded; the driver's own state, the assembled plugin and raw fetched
  bytes are left out and listed. Anything shaped like a credential stops the recording.

## Docker

```bash
docker build -t stemma2 .
```

```bash
docker run --rm stemma2
```

The image runs sample mode by default. Zetaris and Nightshift are optional and stay on
the host; to use them, pass their settings with `--env-file` (never baked in):
`STEMMA_DATALAYER=zetaris`, `ZETARIS_REST_URL=http://host.docker.internal:8888/api/v1.0`,
`ZETARIS_API_KEY`, `STEMMA_PG_HOST=host.docker.internal`, `STEMMA_PG_PASSWORD`, and for
Nightshift `METERLESS_BASE_URL=http://host.docker.internal:8088/v1`,
`METERLESS_RUNTIME_TOKEN`. Live research runs use the Claude Code CLI signed in on the host,
so they run there, not in the image. On Colima, only folders under your home folder can be
mounted into a container.

Zetaris's own install is in `hackathon/zetaris/README.md`.
