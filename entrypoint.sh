#!/bin/sh
set -eu

ENV="${ENV:-dev}"
ENV_FILE=".env.${ENV}"
ENV_PATH="/usr/src/app/${ENV_FILE}"

echo "Container starting... ENV=${ENV}, expecting env file: ${ENV_PATH}"

if [ ! -f "${ENV_PATH}" ] && [ -n "${envfile:-}" ]; then
  echo "No ${ENV_FILE} on disk, but 'envfile' variable present. Writing it to ${ENV_PATH}..."
  printf '%s\n' "$envfile" > "${ENV_PATH}"
fi

if [ -f "${ENV_PATH}" ]; then
  echo "Sourcing environment from ${ENV_PATH}"
  set -a
  . "${ENV_PATH}"
  set +a
else
  echo "WARNING: Could not find ${ENV_PATH}; starting with process environment only."
fi

export PATH="/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH}"

export PYTHONPATH="/usr/src/app/src:/usr/src/app:${PYTHONPATH:-}"

PORT="${PORT:-${APP_PORT:-50042}}"
echo "Starting server on port: ${PORT}, using environment: ${ENV_FILE}"

if python3 -c "import gunicorn" >/dev/null 2>&1; then
  exec python3 -m gunicorn \
    --bind "0.0.0.0:${PORT}" \
    src.main:app \
    --worker-class uvicorn.workers.UvicornWorker
else
  echo "gunicorn module not found; falling back to uvicorn."
  exec python3 -m uvicorn src.main:app --host 0.0.0.0 --port "${PORT}" --proxy-headers
fi
