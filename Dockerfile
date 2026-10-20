# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.12.12@sha256:73d2665b478d8fa2de1cf105c6841f8e9cb6b09e568fc7700440c09f8fcd7ac4 AS uv-bin

FROM rust:1.96.0-slim-bookworm@sha256:4732ca96fd086cb9be682050c3f0176288eebaac2b80aa2bcefccfaf198e1950 AS native-egress-build
ARG TARGETARCH
WORKDIR /src
COPY gateway/Cargo.toml gateway/Cargo.lock ./
COPY gateway/crates ./crates
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,id=codex-lb-cargo-${TARGETARCH},target=/src/target \
    cargo build --release --locked --package codex-lb-egress-worker --bin codex-lb-native-egress \
    && cp target/release/codex-lb-native-egress /tmp/codex-lb-native-egress

FROM oven/bun:1.4.2-alpine@sha256:d888c0ae6c86d7866ff10c5aafdd9077b36aee6455b33dd270fb93c0dd5cef6f AS frontend-build
WORKDIR /app/frontend
COPY gateway/frontend/package.json gateway/frontend/bun.lock ./
RUN --mount=type=cache,target=/root/.bun/install/cache bun install --frozen-lockfile
COPY gateway/frontend ./
RUN bun run build

FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS python-build
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy
WORKDIR /app
COPY --from=uv-bin /uv /uvx /usr/local/bin/
RUN python -m venv --without-pip /opt/venv
ENV PATH="/opt/venv/bin:/usr/local/bin:$PATH"
COPY gateway/pyproject.toml gateway/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra metrics --extra tracing

FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"
ARG VCS_REF=unknown
LABEL org.opencontainers.image.source="https://github.com/jung-geun/codex-lb" \
    org.opencontainers.image.revision="${VCS_REF}" \
    org.opencontainers.image.version="1.25.0-beta.9-subscription-gateway"
WORKDIR /app
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends --only-upgrade \
        bsdutils gzip libblkid1 libc-bin libc6 libcap2 libmount1 libpcre2-8-0 \
        libsmartcols1 libsqlite3-0 libssl3t64 libsystemd0 libudev1 libuuid1 openssl perl-base sed util-linux \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssl-provider-legacy \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip uninstall -y pip setuptools wheel \
    && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.14
RUN adduser --disabled-password --gecos "" app \
    && mkdir -p /var/lib/codex-lb \
    && chown -R app:app /var/lib/codex-lb
COPY --from=python-build /opt/venv /opt/venv
COPY --from=native-egress-build /tmp/codex-lb-native-egress /usr/local/bin/codex-lb-native-egress
COPY --chown=app:app gateway/app app
COPY --chown=app:app gateway/config config
COPY --chown=app:app gateway/scripts scripts
COPY --chown=app:app gateway/UPSTREAM_REVISION UPSTREAM_REVISION
COPY --chown=app:app gateway/LICENSE gateway/THIRD_PARTY_LICENSES.md ./
COPY --chown=app:app --from=frontend-build /app/app/static app/static
RUN chmod +x /app/scripts/docker-entrypoint.sh /usr/local/bin/codex-lb-native-egress \
    && printf '%s\n' '#!/bin/sh' 'exec python -m app.cli "$@"' > /usr/local/bin/codex-lb \
    && chmod +x /usr/local/bin/codex-lb
USER app
RUN test -r /app/app/static/index.html && test -x /usr/local/bin/codex-lb-native-egress
EXPOSE 2455 1455
CMD ["/app/scripts/docker-entrypoint.sh"]
