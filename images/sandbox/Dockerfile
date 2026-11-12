# Sandbox image: what one run works in. It holds the two agent clients (Claude Code and
# Codex), git, a compiler, Node.js with pnpm, Python with uv, and the scanners.
#
# It holds no credentials and nothing that reaches the cluster or GitHub: no kubectl, helm,
# argocd, gh or ssh. A run's subscription sign-in is mounted for that run only, and
# publishing is done by the platform through the GitHub App, never from here.
#
# Everything is pinned: the base image by digest, the Node packages by the integrity
# hashes of node-tools/package-lock.json, the Python packages by the hashes of
# semgrep.lock, and the single-binary tools by the checksums of tools.lock.
#
# node:22-bookworm-slim, pinned by digest (resolved 2026-11-12).
ARG NODE_IMAGE=node:22-bookworm-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee

FROM ${NODE_IMAGE} AS tools
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY tools.lock install-tools.sh /build/
RUN /build/install-tools.sh /build/tools.lock /out

FROM ${NODE_IMAGE} AS node-tools
WORKDIR /opt/sandbox/node
COPY node-tools/package.json node-tools/package-lock.json ./
# `npm ci` installs exactly what the lockfile names and checks every integrity hash. The
# Claude Code package copies its own binary into place when installed, after which the
# copy built for another C library is dead weight.
RUN npm ci --omit=dev --no-audit --no-fund \
    && rm -rf node_modules/@anthropic-ai/claude-code-linux-*-musl \
    && node_modules/.bin/claude --version \
    && node_modules/.bin/codex --version \
    && node_modules/.bin/pnpm --version

FROM ${NODE_IMAGE}
# One unprivileged account. No file keeps a set-user or set-group bit, so nothing in the
# image can raise privileges even where the runtime does not already forbid it.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        bubblewrap build-essential ca-certificates curl file git jq less patch procps \
        python3 python3-venv ripgrep tini unzip xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && userdel --remove node \
    && groupadd --gid 10002 agent \
    && useradd --uid 10002 --gid 10002 --home-dir /home/agent --create-home \
        --shell /bin/bash agent \
    && install --directory --owner agent --group agent /workspace \
    && find / -xdev -type f -perm /6000 -exec chmod a-s {} +
COPY --from=tools /out/ /usr/local/bin/
COPY --from=node-tools /opt/sandbox/node /opt/sandbox/node
COPY semgrep.lock /opt/sandbox/semgrep.lock
RUN python3 -m venv /opt/sandbox/semgrep \
    && uv pip install --python /opt/sandbox/semgrep/bin/python --no-cache \
        --require-hashes --only-binary :all: --requirement /opt/sandbox/semgrep.lock \
    && ln --symbolic /opt/sandbox/semgrep/bin/semgrep /usr/local/bin/semgrep \
    && for name in claude codex pnpm; do \
         ln --symbolic "/opt/sandbox/node/node_modules/.bin/${name}" "/usr/local/bin/${name}"; \
       done
COPY sandbox-versions /usr/local/bin/sandbox-versions
# The image is pinned, so the clients must not look for newer versions of themselves, and
# the scanner must not report to its maker.
ENV HOME=/home/agent \
    DISABLE_AUTOUPDATER=1 \
    SEMGREP_ENABLE_VERSION_CHECK=0 \
    SEMGREP_SEND_METRICS=off \
    npm_config_update_notifier=false
USER 10002:10002
WORKDIR /workspace
# tini reaps the processes an agent leaves behind and passes signals on, so a cancelled
# run stops at once.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sandbox-versions"]
# Last, so that a new commit changes this line alone and every layer above is reused.
ARG REVISION=unknown
LABEL org.opencontainers.image.title="engineering-control-sandbox" \
      org.opencontainers.image.source="https://github.com/gntik-ai/Engineering-Control-Kubernetes" \
      org.opencontainers.image.revision="${REVISION}"
