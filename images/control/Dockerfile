# Control-plane image: the API, dashboard and console, evidence ingest, notifier, and the
# Temporal workflow worker. One image; the Kubernetes manifests choose the command.
# It holds no agent CLIs, scanners or project toolchains: those belong to the sandbox image.
#
# python:3.14-slim, pinned by digest (resolved 2026-11-11).
ARG PYTHON_IMAGE=python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f

FROM ${PYTHON_IMAGE} AS build
WORKDIR /build
COPY pyproject.toml requirements.lock README.md ./
COPY src ./src
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check \
        --constraint requirements.lock .

FROM ${PYTHON_IMAGE}
ARG REVISION=unknown
LABEL org.opencontainers.image.title="engineering-control" \
      org.opencontainers.image.source="https://github.com/gntik-ai/Engineering-Control-Kubernetes" \
      org.opencontainers.image.revision="${REVISION}"
# git: the controller-side activities read repositories. No compiler, no shell tooling.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 engineering \
    && useradd --uid 10001 --gid 10001 --home-dir /home/engineering --create-home \
        --shell /usr/sbin/nologin engineering
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
# Migrations run from this image as a Job; schemas and default policies are read at runtime.
COPY alembic.ini ./
COPY migrations ./migrations
COPY config ./config
# The script the console hands out for exporting a project's records from the original
# system. It runs there, not here.
COPY scripts/export_original_records.py ./scripts/export_original_records.py
ENV PATH=/opt/venv/bin:${PATH} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# ENGINEERING_BUILD_ID is deliberately not set here: it switches on Temporal worker
# versioning, which is a deployment decision made in the manifests, not in the image.
USER 10001:10001
CMD ["engineering-dashboard"]
