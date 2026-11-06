# Kairo on Cloud Run Instances: Hermes gateway + router + mail watcher + Tailscale (userspace).
# Build context: the kairo/ repository root (see deploy/build.sh).
# trixie: SQLite 3.46 (bookworm's 3.40 has the WAL-reset bug Hermes warns about)
FROM python:3.13-slim-trixie

ARG HERMES_TAG=v2026.9.24
ARG HERMES_COMMIT=f97608f178d1ffeca59860195ab7da295f7c8e5f
ARG TAILSCALE_VERSION=1.104.5
ARG UV_VERSION=0.13.1

# Bytecode is compiled at build time: /app and /opt are read-only at runtime, so Python could
# not cache it and would recompile every module on each start (slow on 1 vCPU).
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 \
    UV_PYTHON=/usr/local/bin/python3.13

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git tini \
 && rm -rf /var/lib/apt/lists/*

# Pinned tools: uv (installer) and Tailscale static binaries (userspace networking, no TUN).
RUN pip install --no-cache-dir "uv==${UV_VERSION}" \
 && curl -fsSL "https://pkgs.tailscale.com/stable/tailscale_${TAILSCALE_VERSION}_amd64.tgz" -o /tmp/ts.tgz \
 && tar -xzf /tmp/ts.tgz -C /tmp \
 && install -m 0755 /tmp/tailscale_${TAILSCALE_VERSION}_amd64/tailscale /tmp/tailscale_${TAILSCALE_VERSION}_amd64/tailscaled /usr/local/bin/ \
 && rm -rf /tmp/ts.tgz /tmp/tailscale_*

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin agent

# Hermes at the pinned tag; refuse to build if the tag moved.
RUN git clone --depth 1 --branch "${HERMES_TAG}" https://github.com/NousResearch/hermes-agent.git /opt/hermes-agent \
 && test "$(git -C /opt/hermes-agent rev-parse HEAD)" = "${HERMES_COMMIT}" \
 && cd /opt/hermes-agent && uv sync --frozen --extra messaging --extra mcp --no-dev \
 && rm -rf /opt/hermes-agent/.git

# Kairo: router and core with their lock files, then the rest of the repo.
COPY router/pyproject.toml router/uv.lock /app/router/
COPY core/pyproject.toml core/uv.lock /app/core/
RUN cd /app/router && uv sync --frozen --no-dev --no-install-project \
 && cd /app/core && uv sync --frozen --no-dev --extra mcp --no-install-project
COPY router /app/router
COPY core /app/core
COPY hermes /app/hermes
COPY deploy /app/deploy
RUN cd /app/router && uv sync --frozen --no-dev \
 && cd /app/core && uv sync --frozen --no-dev --extra mcp \
 && python -m compileall -q /app/router /app/core /opt/hermes-agent -x '/\.venv/|/tests?/' \
 && chmod 0755 /app/deploy/entrypoint.sh /app/hermes/install.sh /app/hermes/cron-jobs.sh \
 && chown -R root:root /app /opt/hermes-agent && chmod -R go-w /app /opt/hermes-agent

USER agent
WORKDIR /home/agent
ENV HOME=/home/agent \
    HERMES_HOME=/home/agent/.hermes \
    KAIRO_STATE_DIR=/home/agent/state \
    KAIRO_ENV_FILE=/secrets/env/kairo.env \
    KAIRO_HERMES_BIN=/opt/hermes-agent/.venv/bin/hermes \
    KAIRO_SNAPSHOT_DIR=/mnt/agent-state/snapshots \
    PORT=8080

EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "/app/deploy/entrypoint.sh"]
