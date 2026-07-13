# syntax=docker/dockerfile:1
# Hugging Face Space / Docker — grok-free-register
# Hardware: 2 vCPU + 16 GB RAM recommended (Chromium Turnstile)
#
# IMPORTANT for HF Spaces:
#   If your Space git tree is incomplete (only Dockerfile), this image
#   clones the full app from GitHub during build. Override with build args:
#     REPO_URL / REPO_REF
#
# Space settings: SDK=Docker, PORT is injected (default 7860)

ARG PYTHON_VERSION=3.11-bookworm
ARG REPO_URL=https://github.com/xiaocongyu66/grok-free-register.git
ARG REPO_REF=main

# ========== Stage 0: Full application source ==========
# Prefer cloning full repo so HF Spaces with a thin Dockerfile still build.
FROM debian:bookworm-slim AS source
ARG REPO_URL
ARG REPO_REF
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# 1) Clone canonical GitHub tree (always present on HF builders with network)
RUN set -eu; \
    echo "Cloning ${REPO_URL} @ ${REPO_REF}"; \
    git clone --depth 1 --branch "${REPO_REF}" "${REPO_URL}" /src/app \
    || git clone --depth 1 "${REPO_URL}" /src/app; \
    cd /src/app && git checkout "${REPO_REF}" 2>/dev/null || true; \
    test -d /src/app/grok_register; \
    test -f /src/app/docker/entrypoint.sh; \
    echo "Source OK: $(cd /src/app && git rev-parse --short HEAD 2>/dev/null || echo unknown)"

# ========== Stage 1: Python deps ==========
FROM python:${PYTHON_VERSION} AS pydeps
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=source /src/app/vendor /build/vendor
# Core deps inline (no requirements.txt dependency)
RUN pip install --upgrade pip wheel \
    && pip install \
        'cloakbrowser>=0.3.0' \
        'requests>=2.31.0' \
        'PySocks>=1.7.1' \
        'python-dotenv>=1.0.0' \
        'httpx>=0.28' \
        'playwright>=1.55' \
        'curl_cffi>=0.6' \
    && if [ -f /build/vendor/CF-Ares/pyproject.toml ] || [ -f /build/vendor/CF-Ares/setup.py ]; then \
         pip install /build/vendor/CF-Ares || true; \
       fi \
    && if [ -f /build/vendor/turnstile-solver/requirements.txt ]; then \
         pip install -r /build/vendor/turnstile-solver/requirements.txt || true; \
       fi

# ========== Stage 2a: Go natives ==========
FROM golang:1.22-bookworm AS gobuild
WORKDIR /src
COPY --from=source /src/app/native/proxy-worker /src/proxy-worker
COPY --from=source /src/app/native/register-worker /src/register-worker
COPY --from=source /src/app/native/solver-gateway /src/solver-gateway
RUN mkdir -p /out \
    && (cd /src/proxy-worker && go build -o /out/proxy-worker .) \
    && (cd /src/register-worker && go build -o /out/register-worker .) \
    && (cd /src/solver-gateway && go build -o /out/solver-gateway .)

# ========== Stage 2b: Rust natives ==========
FROM rust:1-bookworm AS rustbuild
WORKDIR /src
COPY --from=source /src/app/native/inventory-worker /src/inventory-worker
COPY --from=source /src/app/native/solver-watchdog /src/solver-watchdog
RUN mkdir -p /out \
    && (cd /src/inventory-worker && cargo build --release && cp target/release/inventory-worker /out/) \
    && (cd /src/solver-watchdog && cargo build --release && cp target/release/solver-watchdog /out/)

# ========== Stage 2c: C++ util ==========
FROM debian:bookworm-slim AS cppbuild
RUN apt-get update && apt-get install -y --no-install-recommends g++ \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /out
WORKDIR /src
COPY --from=source /src/app/native/solver-util /src/solver-util
RUN g++ -O2 -std=c++17 -o /out/solver-util /src/solver-util/solver_util.cpp

# ========== Stage 3: Runtime ==========
FROM python:${PYTHON_VERSION}
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7860 \
    HOST=0.0.0.0 \
    DASHBOARD_PORT=7860 \
    REGISTER_ENGINE=protocol \
    TURNSTILE_SOLVER=hybrid \
    TURNSTILE_SOLVER_ON_DEMAND=1 \
    TURNSTILE_API_URL=http://127.0.0.1:5080 \
    TURNSTILE_SOLVER_HEADLESS=1 \
    TURNSTILE_SOLVER_THREADS=2 \
    GO_REGISTER_WORKERS=4 \
    CONTROL_PLANE_ALLOW_ACTIONS=1 \
    KEY_EXPORT_DIR=/data/keys \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PROJECT_ROOT=/app \
    SOLVER_PYTHON=python

RUN apt-get update && apt-get install -y --no-install-recommends \
    tini ca-certificates curl \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 \
    libx11-6 libx11-xcb1 libxcb1 libxext6 libxshmfence1 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY --from=pydeps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=pydeps /usr/local/bin /usr/local/bin

WORKDIR /app
# Application code from cloned GitHub tree (not Space-local partial tree)
COPY --from=source /src/app/grok_register /app/grok_register
COPY --from=source /src/app/xai_enroller /app/xai_enroller
COPY --from=source /src/app/native/solver-hybrid /app/native/solver-hybrid
COPY --from=source /src/app/scripts /app/scripts
COPY --from=source /src/app/vendor /app/vendor
COPY --from=source /src/app/docker/entrypoint.sh /entrypoint.sh

RUN printf '%s\n' \
      'cloakbrowser>=0.3.0' \
      'requests>=2.31.0' \
      'PySocks>=1.7.1' \
      'python-dotenv>=1.0.0' \
      'httpx>=0.28' \
      'playwright>=1.55' \
      'curl_cffi>=0.6' \
      > /app/requirements.txt \
    && mkdir -p \
      /app/native/proxy-worker \
      /app/native/register-worker \
      /app/native/solver-gateway \
      /app/native/inventory-worker \
      /app/native/solver-watchdog \
      /app/native/solver-util \
      /data/keys /data/logs /app/logs \
    && ln -sfn /data/keys /app/keys \
    && ln -sfn /data/logs /app/logs

COPY --from=gobuild /out/proxy-worker /app/native/proxy-worker/proxy-worker
COPY --from=gobuild /out/register-worker /app/native/register-worker/register-worker
COPY --from=gobuild /out/solver-gateway /app/native/solver-gateway/solver-gateway
COPY --from=rustbuild /out/inventory-worker /app/native/inventory-worker/inventory-worker
COPY --from=rustbuild /out/solver-watchdog /app/native/solver-watchdog/solver-watchdog
COPY --from=cppbuild /out/solver-util /app/native/solver-util/solver-util

RUN chmod +x /entrypoint.sh \
      /app/native/proxy-worker/proxy-worker \
      /app/native/register-worker/register-worker \
      /app/native/solver-gateway/solver-gateway \
      /app/native/inventory-worker/inventory-worker \
      /app/native/solver-watchdog/solver-watchdog \
      /app/native/solver-util/solver-util \
    && python -m playwright install chromium \
    && python -m playwright install-deps chromium || true

EXPOSE 7860
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/entrypoint.sh"]
