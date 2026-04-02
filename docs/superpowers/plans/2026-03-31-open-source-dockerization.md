# Open-Source Docker Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package Story2Memory into a single Docker Compose startup path so new users can copy `.env.example`, run one command, and use the app with all first-party services containerized.

**Architecture:** Run the Reflex application as one `app` container and move all first-party dependencies into Compose-managed services: MySQL, Qdrant, Neo4j, Redis, `rerank-local`, and `web-search`. Use one committed `.env.example` as the runtime contract, switch internal service addresses to Docker DNS names, and add one-shot init services for MySQL schema bootstrap and Neo4j initialization.

**Tech Stack:** Docker Compose, Dockerfile, Reflex, Python 3.13, pytest, MySQL 8, Qdrant, Neo4j 5, Redis 7, Bash

---

## File Map

**Create:**

- `.env.example`
- `.dockerignore`
- `Dockerfile`
- `README.md`
- `scripts/docker_app_entrypoint.sh`
- `tests/test_open_source_runtime_contract.py`

**Modify:**

- `.gitignore`
- `docker-compose.yml`
- `Makefile`

**Reference only:**

- `rxconfig.py`
- `database/mysql/create_tables.sql`
- `scripts/init_neo4j.cypher`
- `database/qdrant_client.py`
- `reflex_app/reflex_app.py`

**Responsibilities:**

- `.env.example`: single public runtime contract for open-source users
- `.dockerignore`: keep image builds fast and avoid copying local state
- `Dockerfile`: build the `app` image that runs the Reflex app
- `scripts/docker_app_entrypoint.sh`: stable application startup command inside the container
- `docker-compose.yml`: full service topology, health checks, init services, named volumes, published ports
- `Makefile`: remove stale startup targets and align helper commands with Docker-first workflow
- `README.md`: canonical onboarding path for open-source users
- `tests/test_open_source_runtime_contract.py`: repository contract tests for Docker/runtime artifacts

## Task 1: Define the Public Runtime Contract

**Files:**

- Create: `.env.example`
- Create: `tests/test_open_source_runtime_contract.py`
- Modify: `.gitignore`

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path


def test_env_example_declares_required_open_source_keys():
    env_example = Path(".env.example")
    assert env_example.exists(), ".env.example should exist for public setup"

    content = env_example.read_text(encoding="utf-8")
    required_lines = [
        "APP_ENV=prod",
        "APP_FRONTEND_PORT=3000",
        "APP_BACKEND_PORT=8000",
        "MYSQL_DSN=mysql+pymysql://story2memory:story2memory@mysql:3306/novel_cognition",
        "QDRANT_URL=http://qdrant:6333",
        "NEO4J_URI=bolt://neo4j:7687",
        "REDIS_URL=redis://redis:6379/0",
        "RERANK_BASE_URL=http://rerank-local:8000",
        "LLM_API_KEY=your-llm-api-key",
        "LLM_BASE_URL=https://your-llm-base-url",
        "LLM_MODEL=your-llm-model",
    ]
    for line in required_lines:
        assert line in content, f"missing line: {line}"


def test_gitignore_blocks_local_env_files_but_keeps_env_example_trackable():
    gitignore = Path(".gitignore").read_text(encoding="utf-8")
    assert ".env" in gitignore
    assert "!.env.example" in gitignore
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_open_source_runtime_contract.py::test_env_example_declares_required_open_source_keys tests/test_open_source_runtime_contract.py::test_gitignore_blocks_local_env_files_but_keeps_env_example_trackable -v`

Expected: FAIL because `.env.example` does not exist and `.gitignore` does not ignore `.env`

- [ ] **Step 3: Write minimal implementation**

Create `.env.example` with grouped sections and placeholder values:

```dotenv
APP_ENV=prod
APP_FRONTEND_PORT=3000
APP_BACKEND_PORT=8000

MYSQL_ROOT_PASSWORD=change-me-root
MYSQL_DATABASE=novel_cognition
MYSQL_USER=story2memory
MYSQL_PASSWORD=story2memory
MYSQL_DSN=mysql+pymysql://story2memory:story2memory@mysql:3306/novel_cognition

QDRANT_URL=http://qdrant:6333
QDRANT_TIMEOUT_SECONDS=20

NEO4J_URI=bolt://neo4j:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=change-me-neo4j

REDIS_URL=redis://redis:6379/0

LLM_API_KEY=your-llm-api-key
LLM_BASE_URL=https://your-llm-base-url
LLM_MODEL=your-llm-model

EMBED_API_KEY=your-embedding-api-key
EMBED_BASE_URL=https://your-embedding-base-url
EMBED_MODEL=your-embedding-model

RERANK_PROVIDER=openai_compatible
RERANK_DISABLED=0
RERANK_API_KEY=EMPTY
RERANK_BASE_URL=http://rerank-local:8000
RERANK_MODEL=bge-reranker-v2-m3
```

Update `.gitignore` to include:

```gitignore
.env
.env.*
!.env.example
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_open_source_runtime_contract.py::test_env_example_declares_required_open_source_keys tests/test_open_source_runtime_contract.py::test_gitignore_blocks_local_env_files_but_keeps_env_example_trackable -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add .env.example .gitignore tests/test_open_source_runtime_contract.py
git commit -m "chore: add open-source env contract"
```

## Task 2: Package the Reflex App into an Image

**Files:**

- Create: `.dockerignore`
- Create: `Dockerfile`
- Create: `scripts/docker_app_entrypoint.sh`
- Modify: `tests/test_open_source_runtime_contract.py`

- [ ] **Step 1: Extend the failing test**

Append these tests:

```python
def test_app_container_files_define_reflex_runtime():
    dockerfile = Path("Dockerfile")
    entrypoint = Path("scripts/docker_app_entrypoint.sh")

    assert dockerfile.exists(), "Dockerfile should exist"
    assert entrypoint.exists(), "docker entrypoint should exist"

    docker_content = dockerfile.read_text(encoding="utf-8")
    entrypoint_content = entrypoint.read_text(encoding="utf-8")

    assert "FROM python:3.13-slim" in docker_content
    assert "COPY requirements.txt" in docker_content
    assert 'CMD ["/app/scripts/docker_app_entrypoint.sh"]' in docker_content
    assert "reflex run --env prod" in entrypoint_content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_open_source_runtime_contract.py::test_app_container_files_define_reflex_runtime -v`

Expected: FAIL because the files do not exist

- [ ] **Step 3: Write minimal implementation**

Create `.dockerignore`:

```dockerignore
.git
__pycache__/
.pytest_cache/
.web/
.states/
.worktrees/
data/logs/
*.pyc
*.pyo
*.db
.env
```

Create `Dockerfile`:

```dockerfile
FROM python:3.13-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl default-mysql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt && pip install reflex

COPY . /app
RUN chmod +x /app/scripts/docker_app_entrypoint.sh

EXPOSE 3000 8000

CMD ["/app/scripts/docker_app_entrypoint.sh"]
```

Create `scripts/docker_app_entrypoint.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

cd /app

exec reflex run --env prod --backend-host 0.0.0.0 --frontend-port "${APP_FRONTEND_PORT:-3000}" --backend-port "${APP_BACKEND_PORT:-8000}"
```

- [ ] **Step 4: Run test and image build verification**

Run:

```bash
pytest tests/test_open_source_runtime_contract.py::test_app_container_files_define_reflex_runtime -v
docker build -t story2memory-app:test .
```

Expected:

- pytest: PASS
- docker build: exits `0`

- [ ] **Step 5: Commit**

```bash
git add .dockerignore Dockerfile scripts/docker_app_entrypoint.sh tests/test_open_source_runtime_contract.py
git commit -m "feat: add app container image"
```

## Task 3: Expand Compose into the Canonical Full Stack

**Files:**

- Modify: `docker-compose.yml`
- Modify: `tests/test_open_source_runtime_contract.py`

- [ ] **Step 1: Extend the failing test**

Append:

```python
def test_docker_compose_declares_app_qdrant_and_init_services():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    required_tokens = [
        "app:",
        "qdrant:",
        "mysql-init:",
        "neo4j-init:",
        "depends_on:",
        "condition: service_healthy",
        "condition: service_completed_successfully",
        "qdrant_data:",
    ]
    for token in required_tokens:
        assert token in compose, f"missing compose token: {token}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_open_source_runtime_contract.py::test_docker_compose_declares_app_qdrant_and_init_services -v`

Expected: FAIL because `app`, `qdrant`, `mysql-init`, and `neo4j-init` are not yet present

- [ ] **Step 3: Write minimal implementation**

Modify `docker-compose.yml` to:

- add `env_file: .env` to all relevant services
- add `qdrant` service using `qdrant/qdrant:latest`
- add `app` service built from the repository root
- add `mysql-init` one-shot service using `mysql:8.0`
- add `neo4j-init` one-shot service using `neo4j:5`
- wire `app` to depend on:
  - `mysql`: `service_healthy`
  - `qdrant`: `service_healthy`
  - `neo4j`: `service_healthy`
  - `redis`: `service_started`
  - `rerank-local`: `service_healthy`
  - `mysql-init`: `service_completed_successfully`
  - `neo4j-init`: `service_completed_successfully`

Use this shape for the new services:

```yaml
  qdrant:
    image: qdrant/qdrant:latest
    restart: unless-stopped
    ports:
      - "127.0.0.1:56333:6333"
    volumes:
      - qdrant_data:/qdrant/storage
    healthcheck:
      test: ["CMD", "curl", "-f", "http://127.0.0.1:6333/"]
      interval: 10s
      timeout: 5s
      retries: 20

  mysql-init:
    image: mysql:8.0
    env_file: .env
    depends_on:
      mysql:
        condition: service_healthy
    volumes:
      - .:/workspace
    working_dir: /workspace
    entrypoint:
      - sh
      - -lc
      - >
        mysql -hmysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE"
        < /workspace/database/mysql/create_tables.sql
    restart: "no"

  neo4j-init:
    image: neo4j:5
    env_file: .env
    depends_on:
      neo4j:
        condition: service_healthy
    volumes:
      - .:/workspace
    working_dir: /workspace
    entrypoint:
      - sh
      - -lc
      - >
        cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USER" -p "$NEO4J_PASSWORD"
        < /workspace/scripts/init_neo4j.cypher
    restart: "no"

  app:
    build:
      context: .
    env_file: .env
    depends_on:
      mysql:
        condition: service_healthy
      qdrant:
        condition: service_healthy
      neo4j:
        condition: service_healthy
      mysql-init:
        condition: service_completed_successfully
      neo4j-init:
        condition: service_completed_successfully
```

- [ ] **Step 4: Run test and compose validation**

Run:

```bash
pytest tests/test_open_source_runtime_contract.py::test_docker_compose_declares_app_qdrant_and_init_services -v
docker compose config
```

Expected:

- pytest: PASS
- `docker compose config`: exits `0`

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml tests/test_open_source_runtime_contract.py
git commit -m "feat: dockerize full open-source stack"
```

## Task 4: Clean Up the Developer Surface and Docker Helper Commands

**Files:**

- Modify: `Makefile`
- Modify: `tests/test_open_source_runtime_contract.py`

- [ ] **Step 1: Extend the failing test**

Append:

```python
def test_makefile_uses_reflex_and_docker_first_targets():
    content = Path("Makefile").read_text(encoding="utf-8")
    assert "uvicorn app.main:create_app" not in content
    assert "docker compose up --build app" in content or "docker compose up --build" in content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_open_source_runtime_contract.py::test_makefile_uses_reflex_and_docker_first_targets -v`

Expected: FAIL because the stale `uvicorn app.main:create_app` target still exists

- [ ] **Step 3: Write minimal implementation**

Update `Makefile`:

- replace the stale `run` target with `reflex run --env dev`
- add `docker-up` to start the full stack
- add `docker-build` to rebuild `app`
- add `docker-logs` to tail the `app` container
- keep advanced remote-maintenance targets only if they still work after the Docker-first flow is established

Use this baseline:

```makefile
run:
	reflex run --env dev

docker-build:
	docker compose build app

docker-up:
	docker compose up --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f app
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_open_source_runtime_contract.py::test_makefile_uses_reflex_and_docker_first_targets -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add Makefile tests/test_open_source_runtime_contract.py
git commit -m "chore: align make targets with docker runtime"
```

## Task 5: Publish the Canonical Onboarding Documentation

**Files:**

- Create: `README.md`
- Modify: `tests/test_open_source_runtime_contract.py`

- [ ] **Step 1: Extend the failing test**

Append:

```python
def test_readme_documents_the_public_quickstart():
    readme = Path("README.md")
    assert readme.exists(), "README.md should exist"

    content = readme.read_text(encoding="utf-8")
    required_phrases = [
        "cp .env.example .env",
        "docker compose up --build",
        "LLM_API_KEY",
        "Qdrant",
        "Neo4j",
        "MySQL",
    ]
    for phrase in required_phrases:
        assert phrase in content, f"missing README phrase: {phrase}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_open_source_runtime_contract.py::test_readme_documents_the_public_quickstart -v`

Expected: FAIL because the root `README.md` does not exist

- [ ] **Step 3: Write minimal implementation**

Create `README.md` with:

- project summary
- required prerequisites: Docker and Docker Compose
- quickstart:

```bash
cp .env.example .env
docker compose up --build
```

- note that users must fill `LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL`
- service summary for MySQL, Qdrant, Neo4j, Redis, and rerank-local
- troubleshooting note for first-run model download delays

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_open_source_runtime_contract.py::test_readme_documents_the_public_quickstart -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add README.md tests/test_open_source_runtime_contract.py
git commit -m "docs: add docker-first open-source quickstart"
```

## Task 6: Run the End-to-End Smoke Checks

**Files:**

- Verify only: `.env.example`, `Dockerfile`, `docker-compose.yml`, `Makefile`, `README.md`

- [ ] **Step 1: Run the repository contract tests**

Run: `pytest tests/test_open_source_runtime_contract.py -v`

Expected: all tests PASS

- [ ] **Step 2: Validate compose rendering**

Run: `docker compose config > /tmp/story2memory.compose.rendered.yml`

Expected: exits `0`

- [ ] **Step 3: Build and start the full stack**

Run:

```bash
cp .env.example .env
docker compose up --build -d
```

Expected:

- images build successfully
- `app`, `mysql`, `qdrant`, `neo4j`, `redis`, `rerank-local`, `web-search` become healthy or started
- `mysql-init` and `neo4j-init` exit successfully

- [ ] **Step 4: Check logs and UI reachability**

Run:

```bash
docker compose ps
docker compose logs --tail=200 app
curl -I http://127.0.0.1:3000
```

Expected:

- `docker compose ps` shows the app stack up
- app logs show Reflex boot completed
- `curl` returns `200`, `302`, or another reachable HTTP response instead of connection refusal

- [ ] **Step 5: Commit**

```bash
git add .
git commit -m "feat: ship docker-first open-source runtime"
```

## Notes for the Implementer

- Do not commit a real `.env`.
- Rotate any credentials that were ever stored in the current local `.env` before publishing.
- Keep `.env.example` authoritative. If a new required variable appears during implementation, add it there immediately.
- Prefer Docker DNS names inside container-facing environment variables. Do not use `127.0.0.1` for container-to-container traffic.
- Keep the first release opinionated. Do not add compose profiles or extra launch modes until the default path is stable.
