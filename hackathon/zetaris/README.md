# Zetaris for Stemma: install and setup

This is how Zetaris becomes the data layer on one Mac. Every secret lives in
`~/.private/zetaris/` (folder mode 700, files 600), never in this repository.

## What runs

- **Zetaris**, from the platform bundle Zetaris issues to a registered account
  (`zetaris-platform.zip`, its `.env`, and a registry login). Proprietary; the
  evaluation terms allow internal use for 30 days without an order.
- **`stemma-evidence-pg`**, a Postgres 16.13 container (`compose.yaml` here) that
  holds the evidence-derived tables. Zetaris cannot read a file on the laptop, so it
  registers this database as a JDBC datasource and serves its tables as one Unified
  Semantic Layer.

## Steps only the owner can take

1. Done 26 Oct: registered, downloaded `zetaris-platform.zip` into `~/.private/zetaris/`.
2. Sign in to Zetaris's image registry: paste the `docker login zetarishackathon.azurecr.io`
   command from the download page into Terminal. No agent runs a command carrying the
   owner's password.
3. Then, in the same Terminal, one command that finishes the install (preflight, a 4 GB
   pull, the first start, Zetaris's `verify.sh`, and the network link to the evidence
   database; 20 to 45 minutes, logged to `~/.private/zetaris/install.log`):

```bash
bash ~/.private/zetaris/finish-install.sh
```

4. When it ends it says what is left: sign in at http://localhost:3300 with the email and
   password in `~/.private/zetaris/admin.txt`, create an API key under Management → API
   Tokens, and add `ZETARIS_API_KEY=<key>` to `~/.private/zetaris/stemma.env`. Zetaris's
   guide says an agent must never create or fetch this key.

The interface is on port 3300, not Zetaris's default 3000: on this Mac another app
already listens on 127.0.0.1:3000. `.env` sets `UI_PORT=127.0.0.1:3300` (loopback only)
and `ALLOWED_ORIGINS` and `GUI_URL` to match.

## What the builder prepared

- The bundle unzipped into `~/.private/zetaris/zetaris-platform/` (zip sha256
  `14c6a811…13d3`, images 2.4.3-v2.4.3.1-hackathon, JDBC driver 2.4.3.1). Its `.env` ships
  filled in; two changes, the original kept as `.env.as-shipped` (both mode 600): our own
  admin password in place of the published default, and `TELEMETRY_ENABLED=false`
  (declining the optional usage report, so the first sign-in does not ask).
- `preflight.sh` passes except the registry sign-in: Docker 29.2.1, Compose 5.1.3,
  Rosetta on (Colima `vz`), 4 CPUs, 7 GB (8 recommended, 6 required), 80 GB free.
- `postgres:16-alpine`, the stack's one public image, pulled in advance.
- `stemma-evidence-pg` running (`compose.yaml` here, restarts with Docker).
- `finish-install.sh` (a copy of the one in this folder) in `~/.private/zetaris/`.

The evidence database joins Zetaris's network (`zetaris_default`) so the query engine
reaches it by name (`stemma-evidence-pg:5432`):

```bash
docker network connect zetaris_default stemma-evidence-pg
```

Zetaris's own checks, in the order its quick start asks: `verify.sh` (nine checks),
the Pikachu recipe (`docs/guides/first-dataset.md` in zetaris/getting-started), and the
REST smoke test:

```bash
python -m stemma.datalayer check-zetaris
```

Each run is provisioned in Zetaris on its first Zetaris-mode sync (its own read-only
Postgres role, datasource and semantic layer); to do it by hand for one run folder:

```bash
python -m stemma.datalayer provision <run_dir>
```

Then run the live tests:

```bash
python -m pytest tests/datalayer/test_live.py -v
```

## The semantic layer

One USL model per run, `lightning.metastore.stemma.evidence_<run key>`, with ten tables:
`sync_state`, `capture`, `claim`, `claim_support`, `claim_input`, `claim_entity`,
`claim_verdict`, `claim_supersession`, `sec_fact` and `doc_table_cell`. Each run's rows
live in their own Postgres schema, read by a fresh role that can read that schema and
nothing else (checked before Zetaris is given it, and on every sync). The definitions come from
`src/stemma/datalayer/schema.py`, the one place they are written. Each foreign key
becomes a Zetaris data-quality rule; for example, every support row must cite a
receipt in the capture manifest.

## Tear down

```bash
cd ~/.private/zetaris/zetaris-platform && docker compose down
```

```bash
docker compose -f hackathon/zetaris/compose.yaml down
```

`down -v` would also delete the data volumes; use it only to discard everything.
