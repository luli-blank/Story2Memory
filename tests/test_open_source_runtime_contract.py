from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def test_public_repo_includes_minimum_governance_files():
    for name in ["LICENSE", "CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md"]:
        assert (REPO_ROOT / name).exists(), f"missing governance file: {name}"


def test_public_repo_tracks_no_copyrighted_sample_assets():
    forbidden = [
        path
        for path in _tracked_files()
        if path == "data/.DS_Store"
        or path.startswith("data/book/")
        or path.startswith("data/picture/")
        or path.endswith(".epub")
    ]
    assert forbidden == []


def test_gitignore_blocks_local_private_state_for_public_repo():
    content = _read(".gitignore")
    for token in [
        ".env",
        ".env.*",
        "!.env.example",
        ".worktrees/",
        ".states",
        ".web",
        "uploaded_files/",
        "data/book/",
        "data/picture/",
        "data/logs/",
    ]:
        assert token in content, f"missing ignore rule: {token}"


def test_dockerignore_blocks_local_state_and_private_inputs():
    content = _read(".dockerignore")
    for token in [
        ".env",
        ".env.*",
        ".web/",
        ".states/",
        ".worktrees/",
        "uploaded_files/",
        "data/logs/",
        "*.db",
    ]:
        assert token in content, f"missing docker ignore rule: {token}"


def test_env_example_declares_public_safe_defaults():
    content = _read(".env.example")
    lines = content.splitlines()
    required_lines = [
        "APP_ENV=prod",
        "APP_FRONTEND_PORT=3000",
        "APP_BACKEND_PORT=8000",
        "MYSQL_ROOT_PASSWORD=change-me-mysql-root-password",
        "MYSQL_PASSWORD=change-me-story2memory-db-password",
        "NEO4J_PASSWORD=change-me-neo4j-password",
        "LLM_API_KEY=your-llm-api-key",
        "LLM_BASE_URL=https://your-llm-base-url",
        "LLM_MODEL=your-llm-model",
        "AGENT_RUNTIME_PREWARM_ENABLED=0",
        "MYSQL_DSN=mysql+pymysql://story2memory:change-me-story2memory-db-password@mysql:3306/novel_cognition",
    ]
    for line in required_lines:
        assert line in content, f"missing env line: {line}"
    assert "MYSQL_PASSWORD=story2memory" not in lines
    assert "MYSQL_ROOT_PASSWORD=change-me-root" not in lines
    assert "NEO4J_PASSWORD=change-me-neo4j" not in lines


def test_app_container_files_define_public_reflex_runtime():
    docker_content = _read("Dockerfile")
    entrypoint_content = _read("scripts/docker_app_entrypoint.sh")
    compose_content = _read("docker-compose.yml")

    assert "FROM python:3.13-slim" in docker_content
    assert "FROM oven/bun:1.1.29" in docker_content
    assert "COPY --from=bun /usr/local/bin/bun /usr/local/bin/bun" in docker_content
    assert 'CMD ["/app/scripts/docker_app_entrypoint.sh"]' in docker_content
    assert "python -m core.public_runtime" in entrypoint_content
    assert "reflex run --env prod" in entrypoint_content
    assert "${STORY2MEMORY_ENV_FILE:-.env}" in compose_content


def test_app_container_persists_uploaded_books_and_covers():
    compose_content = _read("docker-compose.yml")
    assert "./data/book:/app/data/book" in compose_content
    assert "./data/picture:/app/data/picture" in compose_content


def test_character_table_schema_defaults_need_delete_to_no():
    create_tables = _read("database/mysql/create_tables.sql")
    assert "`NEED_DELETE` ENUM('yes', 'no') NOT NULL DEFAULT 'no'" in create_tables


def test_docker_compose_binds_only_public_app_ports_to_loopback():
    compose = _read("docker-compose.yml")
    assert '"127.0.0.1:13306:3306"' in compose
    assert '"127.0.0.1:${APP_FRONTEND_PORT:-3000}:3000"' in compose
    assert '"127.0.0.1:${APP_BACKEND_PORT:-8000}:8000"' in compose
    for token in [
        "${REDIS_HOST_PORT",
        "${WEB_SEARCH_HOST_PORT",
        "${NEO4J_HTTP_HOST_PORT",
        "${NEO4J_BOLT_HOST_PORT",
        "${QDRANT_HOST_PORT",
        "${RERANK_HOST_PORT",
    ]:
        assert token not in compose, f"unexpected host port exposure in compose: {token}"


def test_makefile_keeps_only_public_workflow_targets():
    content = _read("Makefile")
    for expected in ["docker compose up --build", "docker compose down", "pytest -q"]:
        assert expected in content
    for forbidden in [
        "open_remote_db_tunnel",
        "prepare_remote_db_env",
        "remote_reset_init",
        "warm_fastembed_sparse_model_docker",
        "ui-remote",
        "tunnel-db",
        "prepare-remote-db",
        "remote-reset-init",
    ]:
        assert forbidden not in content


def test_readme_documents_public_open_source_contract():
    content = _read("README.md")
    for phrase in [
        "cp .env.example .env",
        "docker compose up --build",
        "用户自行上传",
        "不附带任何小说正文",
        "MIT",
        "127.0.0.1:3000",
        "127.0.0.1:8000",
        "默认仅本机访问",
        "AGENT_RUNTIME_PREWARM_ENABLED=0",
    ]:
        assert phrase in content, f"missing README phrase: {phrase}"


def test_public_ci_workflow_runs_pytest_and_compose_smoke():
    workflow = _read(".github/workflows/ci.yml")
    assert "pull_request:" in workflow
    assert "push:" in workflow
    assert "pytest -q" in workflow
    assert "scripts/ci_public_smoke.sh" in workflow
