#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env.ci-smoke"
PROJECT_NAME="story2memoryci"
export STORY2MEMORY_ENV_FILE="$ENV_FILE"

cleanup() {
  cd "$ROOT_DIR"
  docker compose --env-file "$ENV_FILE" -p "$PROJECT_NAME" down -v >/dev/null 2>&1 || true
  rm -f "$ENV_FILE"
}

log_failure() {
  cd "$ROOT_DIR"
  docker compose --env-file "$ENV_FILE" -p "$PROJECT_NAME" logs --no-color || true
}

trap log_failure ERR
trap cleanup EXIT

python - <<'PY' "$ROOT_DIR/.env.example" "$ENV_FILE"
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text(encoding="utf-8")
target = source
replacements = {
    "MYSQL_ROOT_PASSWORD=change-me-mysql-root-password": "MYSQL_ROOT_PASSWORD=ci-mysql-root-secret",
    "MYSQL_PASSWORD=change-me-story2memory-db-password": "MYSQL_PASSWORD=ci-story2memory-db-secret",
    "MYSQL_DSN=mysql+pymysql://story2memory:change-me-story2memory-db-password@mysql:3306/novel_cognition": "MYSQL_DSN=mysql+pymysql://story2memory:ci-story2memory-db-secret@mysql:3306/novel_cognition",
    "NEO4J_PASSWORD=change-me-neo4j-password": "NEO4J_PASSWORD=ci-neo4j-secret",
    "LLM_API_KEY=your-llm-api-key": "LLM_API_KEY=fake-key",
    "LLM_BASE_URL=https://your-llm-base-url": "LLM_BASE_URL=https://example.invalid/v1",
    "LLM_MODEL=your-llm-model": "LLM_MODEL=fake-model",
    "EMBED_API_KEY=your-embedding-api-key": "EMBED_API_KEY=fake-embed-key",
    "EMBED_BASE_URL=https://your-embedding-base-url": "EMBED_BASE_URL=https://example.invalid/embed",
    "EMBED_MODEL=your-embedding-model": "EMBED_MODEL=fake-embed-model",
}
for old, new in replacements.items():
    target = target.replace(old, new)
Path(sys.argv[2]).write_text(target, encoding="utf-8")
PY

cd "$ROOT_DIR"
docker compose --env-file "$ENV_FILE" -p "$PROJECT_NAME" up --build -d

for url in "http://127.0.0.1:8000/_health" "http://127.0.0.1:8000/ping" "http://127.0.0.1:3000"; do
  for _ in $(seq 1 60); do
    if curl --fail --silent --show-error "$url" >/dev/null; then
      break
    fi
    sleep 2
  done
  curl --fail --silent --show-error "$url" >/dev/null
done

docker compose --env-file "$ENV_FILE" -p "$PROJECT_NAME" ps
