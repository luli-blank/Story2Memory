#!/usr/bin/env bash
set -euo pipefail

cd /app

python -m core.public_runtime

exec reflex run --env prod --backend-host 0.0.0.0 --frontend-port "${APP_FRONTEND_PORT:-3000}" --backend-port "${APP_BACKEND_PORT:-8000}"
