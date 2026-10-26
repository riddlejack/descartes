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

1. Register at https://cloud.enterprise.zetaris.com/register and accept Zetaris's terms.
2. On https://cloud.enterprise.zetaris.com/dashboard/download, download
   `zetaris-platform.zip` into `~/.private/zetaris/`, and run the registry login it
   shows (`docker login zetarishackathon.azurecr.io`) in Terminal.
3. After the platform is up: sign in at http://localhost:3000, create an API key under
   Management → API Tokens, and add `ZETARIS_API_KEY=<key>` to
   `~/.private/zetaris/stemma.env`. Zetaris's guide says an agent must never create or
   fetch this key.

## Steps the builder runs

```bash
docker compose --env-file ~/.private/zetaris/stemma.env -f hackathon/zetaris/compose.yaml up -d
```

```bash
cd ~/.private/zetaris && unzip zetaris-platform.zip && cd zetaris-platform && ./preflight.sh
```

Then set the `.env` values Zetaris's install guide lists (admin email and password,
`ZSPARK_DRIVER_MEM=2g`, `ZSPARK_DRIVER_CORES=2`, `ZSPARK_MASTER_URL=local[2]`,
`ZETARIS_API_MAX_HEAP=1g`, `ZETARIS_SEED_TPCH=true`), `chmod 600 .env`, and:

```bash
docker compose pull && docker compose up -d && ./verify.sh
```

Connect the evidence database to Zetaris's network, so its query engine reaches it by
name (`stemma-evidence-pg:5432`):

```bash
docker network connect zetaris-platform_default stemma-evidence-pg
```

Zetaris's own checks, in the order its quick start asks: `verify.sh` (nine checks),
the Pikachu recipe (`docs/guides/first-dataset.md` in zetaris/getting-started), and the
REST smoke test:

```bash
python -m stemma.datalayer check-zetaris
```

Then register the datasource and the semantic layer, and run the live tests:

```bash
python -m stemma.datalayer setup-zetaris
```

```bash
python -m pytest tests/datalayer/test_live.py -v
```

## The semantic layer

One USL model, `lightning.metastore.stemma.evidence`, with nine tables: `capture`,
`claim`, `claim_support`, `claim_input`, `claim_entity`, `claim_verdict`,
`claim_supersession`, `sec_fact` and `doc_table_cell`. The definitions come from
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
