#!/bin/sh
# HF Space / Docker entrypoint for grok-free-register
set -eu

cd /app

# HF Space injects PORT (default 7860). Dashboard follows it.
export PORT="${PORT:-7860}"
export HOST="${HOST:-0.0.0.0}"
export DASHBOARD_PORT="${PORT}"
export KEY_EXPORT_DIR="${KEY_EXPORT_DIR:-/data/keys}"
export PROJECT_ROOT="/app"
export PYTHONPATH="/app${PYTHONPATH:+:$PYTHONPATH}"
export SOLVER_PYTHON="${SOLVER_PYTHON:-python}"
export TURNSTILE_API_URL="${TURNSTILE_API_URL:-http://127.0.0.1:5080}"
export TURNSTILE_SOLVER="${TURNSTILE_SOLVER:-hybrid}"
export TURNSTILE_SOLVER_ON_DEMAND="${TURNSTILE_SOLVER_ON_DEMAND:-1}"
export TURNSTILE_SOLVER_HEADLESS="${TURNSTILE_SOLVER_HEADLESS:-1}"
export REGISTER_ENGINE="${REGISTER_ENGINE:-protocol}"
export CONTROL_PLANE_ALLOW_ACTIONS="${CONTROL_PLANE_ALLOW_ACTIONS:-1}"
# Panel auth: set DASHBOARD_PASSWORD (and optional DASHBOARD_USER) via Space Secrets
export DASHBOARD_USER="${DASHBOARD_USER:-${CONTROL_PLANE_USER:-admin}}"
export DASHBOARD_PASSWORD="${DASHBOARD_PASSWORD:-${CONTROL_PLANE_PASSWORD:-${PANEL_PASSWORD:-}}}"
export CONTROL_PLANE_TOKEN="${CONTROL_PLANE_TOKEN:-${DASHBOARD_TOKEN:-${PANEL_TOKEN:-}}}"

mkdir -p /data/keys /data/logs /app/logs

# Seed .env from example if missing (secrets should come from Space Secrets / -e)
if [ ! -f /app/.env ]; then
  if [ -f /app/.env.example ]; then
    cp /app/.env.example /app/.env
  else
    touch /app/.env
  fi
fi

# Append runtime defaults without clobbering existing keys
grep -q '^REGISTER_ENGINE=' /app/.env 2>/dev/null || echo "REGISTER_ENGINE=${REGISTER_ENGINE}" >> /app/.env
grep -q '^TURNSTILE_SOLVER=' /app/.env 2>/dev/null || echo "TURNSTILE_SOLVER=${TURNSTILE_SOLVER}" >> /app/.env
grep -q '^TURNSTILE_SOLVER_ON_DEMAND=' /app/.env 2>/dev/null || echo "TURNSTILE_SOLVER_ON_DEMAND=${TURNSTILE_SOLVER_ON_DEMAND}" >> /app/.env
grep -q '^TURNSTILE_API_URL=' /app/.env 2>/dev/null || echo "TURNSTILE_API_URL=${TURNSTILE_API_URL}" >> /app/.env
grep -q '^KEY_EXPORT_DIR=' /app/.env 2>/dev/null || echo "KEY_EXPORT_DIR=${KEY_EXPORT_DIR}" >> /app/.env
grep -q '^CONTROL_PLANE_ALLOW_ACTIONS=' /app/.env 2>/dev/null || echo "CONTROL_PLANE_ALLOW_ACTIONS=${CONTROL_PLANE_ALLOW_ACTIONS}" >> /app/.env

# Optional Space URL hint
if [ -n "${SPACE_ID:-}" ]; then
  SPACE_HOST=$(echo "${SPACE_ID}" | tr '/' '-')
  echo "✅ HF Space: https://${SPACE_HOST}.hf.space  (dashboard :${PORT})"
else
  echo "✅ Dashboard will bind ${HOST}:${PORT}"
fi

# Sanity: native binaries
for b in \
  /app/native/solver-gateway/solver-gateway \
  /app/native/register-worker/register-worker \
  /app/native/inventory-worker/inventory-worker
do
  if [ -x "$b" ]; then
    echo "  native ok: $b"
  else
    echo "  ⚠️ missing or not executable: $b (protocol may still run via Python)"
  fi
done

# Prefer CapSolver if key present (no browser pressure)
if [ -n "${CAPSOLVER_API_KEY:-}${CAPSOLVER_KEY:-}" ]; then
  echo "✅ CapSolver key detected — protocol path can skip heavy browser when solver API used"
fi

if [ -n "${DASHBOARD_PASSWORD}" ] || [ -n "${CONTROL_PLANE_TOKEN}" ]; then
  echo "🔒 Panel auth enabled (DASHBOARD_PASSWORD / CONTROL_PLANE_TOKEN)"
else
  echo "⚠️  Panel auth OFF — set DASHBOARD_PASSWORD secret for public Spaces"
fi

echo "🚀 Starting grok-free-register dashboard..."
# Dashboard is the control plane; registration is started from UI / API
exec python -m grok_register.dashboard --host "${HOST}" --port "${PORT}"
