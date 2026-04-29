#!/usr/bin/env bash
set -euo pipefail

cd /app

export MYSQL_DSN="$(python - <<'PY'
from database.mysql_dsn import resolve_mysql_dsn

print(resolve_mysql_dsn())
PY
)"

python -m core.public_runtime

APP_PORT="${APP_FRONTEND_PORT:-3000}"

exec reflex run \
  --env prod \
  --single-port \
  --backend-host 0.0.0.0 \
  --frontend-port "${APP_PORT}" \
  --backend-port "${APP_PORT}"
