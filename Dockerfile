# Stemma 2 from a fresh clone: SAMPLE_MODE by default (replays a recorded run, every line
# marked [replay] with its hash; World Model's conformance suite and the data layer run
# live). Needs no Zetaris, no Nightshift and no credentials.
#
#   docker build -t stemma2 .
#   docker run --rm stemma2                      # the sample replay
#   docker run --rm stemma2 python run.py conformance
#
# Optional, both on the host: Zetaris (STEMMA_DATALAYER=zetaris, ZETARIS_REST_URL=
# http://host.docker.internal:8888/api/v1.0, ZETARIS_API_KEY, STEMMA_PG_HOST=
# host.docker.internal, STEMMA_PG_PASSWORD) and Nightshift (METERLESS_BASE_URL=
# http://host.docker.internal:8088/v1, METERLESS_RUNTIME_TOKEN). Pass them with --env-file;
# never bake them into the image. Live research runs use the Claude Code CLI signed in on
# the host, so they run there (python run.py ask ...), not in this image.

# Node for the conformance runner (vendor/runner.ts via tsx), pinned.
FROM node:24.14.1-bookworm-slim AS node

FROM python:3.12.13-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && useradd --create-home --uid 10001 stemma

# Only named paths are copied, never the whole context (secrets stay out at any depth;
# .dockerignore also drops every .env file).
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[evidence,server,register,driver,datalayer,providers]"
COPY conformance/package.json conformance/package-lock.json conformance/bridge.ts \
     conformance/run.sh ./conformance/
COPY conformance/vendor ./conformance/vendor
RUN cd conformance && npm ci --no-audit --no-fund
COPY run.py ./
COPY hackathon/__init__.py hackathon/replay.py hackathon/judgelog.py \
     hackathon/conformance.py hackathon/sample.py hackathon/README.md ./hackathon/
COPY hackathon/sample ./hackathon/sample

ENV SAMPLE_MODE=true \
    STEMMA_RUNS=/data/runs \
    STEMMA_PYTHON=/usr/local/bin/python \
    PYTHONDONTWRITEBYTECODE=1
RUN mkdir -p /data/runs && chown -R stemma /data /app/conformance
USER stemma
CMD ["python", "run.py"]
