from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping

import httpx
import pymysql
from dotenv import dotenv_values
from neo4j import GraphDatabase
from openai import OpenAI

from database.mysql_dsn import parse_mysql_dsn, resolve_mysql_dsn

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_OVERRIDE_VAR = "STORY2MEMORY_ENV_OVERRIDE"
DEFAULT_RUNTIME_OVERRIDE_PATH = ROOT_DIR / "data" / "config" / "runtime.env"
ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
ARK_CODING_BASE_URL = "https://ark.cn-beijing.volces.com/api/coding/v3"
DEFAULT_QDRANT_URL = "http://qdrant:6333"
DEFAULT_RERANK_BASE_URL = "http://rerank-local:8000/rerank"
DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
DEFAULT_QWEN_RERANK_BASE_URL = "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"
DEFAULT_QWEN_RERANK_MODEL = "qwen3-rerank"
DEFAULT_QWEN_RERANK_INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query."
STARTUP_HEALTH_TIMEOUT_SECONDS = 3.0
STARTUP_LLM_TIMEOUT_SECONDS = 20.0

STARTUP_PLACEHOLDER_VALUES: dict[str, tuple[str, ...]] = {
    "ark_api_key": ("", "your-ark-api-key", "your-llm-api-key", "your-embedding-api-key"),
    "llm_model": ("", "your-llm-model"),
    "embed_model": ("", "your-embedding-model"),
    "rerank_api_key": ("", "EMPTY", "your-rerank-api-key"),
}

STARTUP_MANAGED_ENV_ORDER: tuple[str, ...] = (
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "HYBRID_DENSE_RETRIEVAL_ENABLED",
    "EMBED_API_KEY",
    "EMBED_BASE_URL",
    "EMBED_MODEL",
    "QDRANT_URL",
    "RERANK_DISABLED",
    "RERANK_PROVIDER",
    "RERANK_BASE_URL",
    "RERANK_API_KEY",
    "RERANK_MODEL",
    "RERANK_INSTRUCTION",
    "AGENT_RUNTIME_PREWARM_ENABLED",
)

DEFAULT_STARTUP_SETTINGS: dict[str, Any] = {
    "ark_api_key": "",
    "llm_model": "",
    "embed_model": "",
    "vector_retrieval_enabled": True,
    "rerank_enabled": True,
    "rerank_provider": "qwen",
    "rerank_base_url": DEFAULT_QWEN_RERANK_BASE_URL,
    "rerank_api_key": "",
    "rerank_model": DEFAULT_QWEN_RERANK_MODEL,
    "rerank_instruction": DEFAULT_QWEN_RERANK_INSTRUCTION,
    "prewarm_enabled": False,
}

PLACEHOLDER_SECRET_VALUES: dict[str, tuple[str, ...]] = {
    "MYSQL_ROOT_PASSWORD": ("change-me-mysql-root-password", "change-me-root"),
    "MYSQL_PASSWORD": ("change-me-story2memory-db-password", "story2memory"),
    "NEO4J_PASSWORD": ("change-me-neo4j-password", "change-me-neo4j"),
}


def _normalize_text(value: object) -> str:
    return str(value or "").strip()


def _is_truthy(value: object) -> bool:
    return _normalize_text(value).lower() in {"1", "true", "yes", "on"}


def _is_ark_base_url(value: object) -> bool:
    normalized = _normalize_text(value).rstrip("/")
    return not normalized or normalized in {ARK_BASE_URL, ARK_CODING_BASE_URL}


def _is_placeholder_value(settings_key: str, value: object) -> bool:
    normalized = _normalize_text(value)
    return normalized in STARTUP_PLACEHOLDER_VALUES.get(settings_key, ())


def resolve_env_value(*names: str, env: Mapping[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    for name in names:
        if not name:
            continue
        value = _normalize_text(source.get(name, ""))
        if value:
            return value
    return ""


def resolve_runtime_llm_model(*names: str, env: Mapping[str, str] | None = None) -> str:
    return resolve_env_value(*names, "LLM_MODEL", env=env)


def require_runtime_llm_model(*names: str, env: Mapping[str, str] | None = None) -> str:
    value = resolve_runtime_llm_model(*names, env=env)
    if value:
        return value
    label = " / ".join([*names, "LLM_MODEL"]) if names else "LLM_MODEL"
    raise RuntimeError(f"Missing runtime LLM model configuration. Set {label}.")


def resolve_runtime_embed_model(*names: str, env: Mapping[str, str] | None = None) -> str:
    return resolve_env_value(*names, "EMBED_MODEL", env=env)


def require_runtime_embed_model(*names: str, env: Mapping[str, str] | None = None) -> str:
    value = resolve_runtime_embed_model(*names, env=env)
    if value:
        return value
    label = " / ".join([*names, "EMBED_MODEL"]) if names else "EMBED_MODEL"
    raise RuntimeError(f"Missing runtime embedding model configuration. Set {label}.")


def resolve_runtime_llm_base_url(*names: str, env: Mapping[str, str] | None = None) -> str:
    return resolve_env_value(*names, "LLM_BASE_URL", env=env) or ARK_CODING_BASE_URL


def resolve_runtime_embed_base_url(*names: str, env: Mapping[str, str] | None = None) -> str:
    return resolve_env_value(*names, "EMBED_BASE_URL", env=env) or ARK_BASE_URL


def _runtime_override_path(
    path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    if path is not None:
        return Path(path).expanduser()
    source = env if env is not None else os.environ
    override_path = _normalize_text(source.get(ENV_OVERRIDE_VAR, ""))
    if override_path:
        return Path(override_path).expanduser()
    return DEFAULT_RUNTIME_OVERRIDE_PATH


def get_runtime_override_path(
    path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    return _runtime_override_path(path=path, env=env)


def _read_runtime_override_env(
    path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    resolved_path = _runtime_override_path(path=path, env=env)
    if not resolved_path.exists():
        return {}
    return {
        str(key): _normalize_text(value)
        for key, value in dotenv_values(resolved_path).items()
        if key
    }


def _merge_runtime_env(
    env: Mapping[str, str] | None = None,
    path: str | Path | None = None,
) -> dict[str, str]:
    merged = {str(key): _normalize_text(value) for key, value in (env if env is not None else os.environ).items()}
    merged.update(_read_runtime_override_env(path=path, env=env))
    return merged


def build_runtime_env_map(settings: Mapping[str, object]) -> dict[str, str]:
    ark_api_key = _normalize_text(settings.get("ark_api_key", ""))
    llm_model = _normalize_text(settings.get("llm_model", ""))
    embed_model = _normalize_text(settings.get("embed_model", ""))
    vector_retrieval_enabled = True
    rerank_enabled = True
    rerank_provider = _normalize_text(settings.get("rerank_provider", DEFAULT_STARTUP_SETTINGS["rerank_provider"])).lower()
    rerank_provider = rerank_provider or str(DEFAULT_STARTUP_SETTINGS["rerank_provider"])
    rerank_base_url = _normalize_text(settings.get("rerank_base_url", ""))
    rerank_api_key = _normalize_text(settings.get("rerank_api_key", ""))
    rerank_model = _normalize_text(settings.get("rerank_model", ""))
    rerank_instruction = _normalize_text(settings.get("rerank_instruction", DEFAULT_STARTUP_SETTINGS["rerank_instruction"]))
    if not rerank_base_url:
        rerank_base_url = DEFAULT_RERANK_BASE_URL if rerank_provider == "local" else DEFAULT_QWEN_RERANK_BASE_URL
    if not rerank_model:
        rerank_model = DEFAULT_RERANK_MODEL if rerank_provider == "local" else DEFAULT_QWEN_RERANK_MODEL
    prewarm_enabled = bool(settings.get("prewarm_enabled", DEFAULT_STARTUP_SETTINGS["prewarm_enabled"]))
    return {
        "ARK_API_KEY": ark_api_key,
        "LLM_API_KEY": ark_api_key,
        "LLM_BASE_URL": ARK_CODING_BASE_URL,
        "LLM_MODEL": llm_model,
        "HYBRID_DENSE_RETRIEVAL_ENABLED": "1" if vector_retrieval_enabled else "0",
        "EMBED_API_KEY": ark_api_key,
        "EMBED_BASE_URL": ARK_BASE_URL,
        "EMBED_MODEL": embed_model,
        "QDRANT_URL": DEFAULT_QDRANT_URL,
        "RERANK_DISABLED": "0" if rerank_enabled else "1",
        "RERANK_PROVIDER": rerank_provider,
        "RERANK_BASE_URL": rerank_base_url,
        "RERANK_API_KEY": rerank_api_key,
        "RERANK_MODEL": rerank_model,
        "RERANK_INSTRUCTION": rerank_instruction,
        "AGENT_RUNTIME_PREWARM_ENABLED": "1" if prewarm_enabled else "0",
    }


def _candidate_runtime_env(
    settings: Mapping[str, object],
    env: Mapping[str, str] | None = None,
    path: str | Path | None = None,
) -> dict[str, str]:
    merged = _merge_runtime_env(env=env, path=path)
    merged.update(build_runtime_env_map(settings))
    return merged


def build_startup_settings(
    env: Mapping[str, str] | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    merged = _merge_runtime_env(env=env, path=path)
    defaults = dict(DEFAULT_STARTUP_SETTINGS)

    llm_api_key = _normalize_text(merged.get("LLM_API_KEY", ""))
    embed_api_key = _normalize_text(merged.get("EMBED_API_KEY", ""))
    llm_base_url = _normalize_text(merged.get("LLM_BASE_URL", ""))
    embed_base_url = _normalize_text(merged.get("EMBED_BASE_URL", ""))

    ark_api_key = _normalize_text(merged.get("ARK_API_KEY", ""))
    if not ark_api_key and llm_api_key:
        same_provider_keys = (not embed_api_key) or (llm_api_key == embed_api_key)
        if same_provider_keys and _is_ark_base_url(llm_base_url) and _is_ark_base_url(embed_base_url):
            ark_api_key = llm_api_key

    defaults.update(
        {
            "ark_api_key": ark_api_key,
            "llm_model": _normalize_text(merged.get("LLM_MODEL", defaults["llm_model"])),
            "embed_model": _normalize_text(merged.get("EMBED_MODEL", defaults["embed_model"])),
            "vector_retrieval_enabled": True,
            "rerank_enabled": True,
            "rerank_provider": _normalize_text(merged.get("RERANK_PROVIDER", defaults["rerank_provider"])).lower()
            or defaults["rerank_provider"],
            "rerank_base_url": _normalize_text(merged.get("RERANK_BASE_URL", "")),
            "rerank_api_key": _normalize_text(merged.get("RERANK_API_KEY", ""))
            or _normalize_text(merged.get("DASHSCOPE_API_KEY", "")),
            "rerank_model": _normalize_text(merged.get("RERANK_MODEL", defaults["rerank_model"])),
            "rerank_instruction": _normalize_text(merged.get("RERANK_INSTRUCTION", defaults["rerank_instruction"])),
            "prewarm_enabled": _is_truthy(merged.get("AGENT_RUNTIME_PREWARM_ENABLED", "0")),
        }
    )
    if not defaults["rerank_base_url"]:
        defaults["rerank_base_url"] = (
            DEFAULT_RERANK_BASE_URL
            if defaults["rerank_provider"] == "local"
            else DEFAULT_QWEN_RERANK_BASE_URL
        )
    return defaults


def validate_startup_settings(settings: Mapping[str, object]) -> list[str]:
    errors: list[str] = []

    def _require(settings_key: str, label: str) -> None:
        value = settings.get(settings_key, "")
        if not _normalize_text(value) or _is_placeholder_value(settings_key, value):
            errors.append(f"{label} is required.")

    _require("ark_api_key", "ARK_API_KEY")
    _require("llm_model", "LLM_MODEL")
    _require("embed_model", "EMBED_MODEL")
    provider = _normalize_text(settings.get("rerank_provider", "")).lower()
    if provider not in {"local", "qwen", "openai_compatible", "ark"}:
        errors.append("RERANK_PROVIDER must be one of: local, qwen, openai_compatible, ark.")
    if not _normalize_text(settings.get("rerank_base_url", "")):
        errors.append("RERANK_BASE_URL is required.")
    if not _normalize_text(settings.get("rerank_model", "")):
        errors.append("RERANK_MODEL is required.")
    if provider != "local":
        api_key = _normalize_text(settings.get("rerank_api_key", ""))
        if not api_key or _is_placeholder_value("rerank_api_key", api_key):
            errors.append("RERANK_API_KEY is required for remote rerank.")
    return errors


def _env_line(key: str, value: object) -> str:
    normalized = _normalize_text(value)
    if not normalized:
        return f"{key}="
    if any(char in normalized for char in (' ', '#', '"', "'")):
        escaped = normalized.replace("\\", "\\\\").replace('"', '\\"')
        return f'{key}="{escaped}"'
    return f"{key}={normalized}"


def write_startup_settings(
    settings: Mapping[str, object],
    path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    target_path = _runtime_override_path(path=path, env=env)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    env_map = build_runtime_env_map(settings)
    lines = ["# Story2Memory runtime startup config"]
    lines.extend(_env_line(key, env_map.get(key, "")) for key in STARTUP_MANAGED_ENV_ORDER)
    target_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target_path


def _service_status(
    key: str,
    label: str,
    status: str,
    detail: str,
    *,
    blocking: bool,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "status": status,
        "detail": detail,
        "blocking": blocking,
    }


def _service_failure_status(exc: Exception) -> str:
    message = str(exc).lower()
    if any(
        marker in message
        for marker in (
            "connection refused",
            "timed out",
            "timeout",
            "temporarily unavailable",
            "name or service not known",
            "failed to establish a new connection",
            "no route to host",
            "connection reset by peer",
        )
    ):
        return "starting"
    return "failed"


def _probe_mysql_status(env: Mapping[str, str]) -> dict[str, Any]:
    dsn = resolve_mysql_dsn(env)
    cfg = parse_mysql_dsn(dsn)
    if not cfg:
        return _service_status("mysql", "MySQL", "failed", "MYSQL_DSN 无效。", blocking=True)
    try:
        conn = pymysql.connect(
            **cfg,
            connect_timeout=STARTUP_HEALTH_TIMEOUT_SECONDS,
            read_timeout=STARTUP_HEALTH_TIMEOUT_SECONDS,
            write_timeout=STARTUP_HEALTH_TIMEOUT_SECONDS,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
            charset="utf8mb4",
        )
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1 AS ok")
                cursor.fetchone()
        finally:
            conn.close()
    except Exception as exc:
        return _service_status("mysql", "MySQL", _service_failure_status(exc), f"MySQL 未就绪：{exc}", blocking=True)
    return _service_status("mysql", "MySQL", "ready", "数据库连接正常。", blocking=True)


def _probe_neo4j_status(env: Mapping[str, str]) -> dict[str, Any]:
    uri = _normalize_text(env.get("NEO4J_URI", "bolt://neo4j:7687")) or "bolt://neo4j:7687"
    user = _normalize_text(env.get("NEO4J_USER", "neo4j")) or "neo4j"
    password = _normalize_text(env.get("NEO4J_PASSWORD", ""))
    if not password:
        return _service_status("neo4j", "Neo4j", "failed", "NEO4J_PASSWORD 未配置。", blocking=True)
    try:
        driver = GraphDatabase.driver(uri, auth=(user, password), connection_timeout=STARTUP_HEALTH_TIMEOUT_SECONDS)
        try:
            driver.verify_connectivity()
        finally:
            driver.close()
    except Exception as exc:
        return _service_status("neo4j", "Neo4j", _service_failure_status(exc), f"Neo4j 未就绪：{exc}", blocking=True)
    return _service_status("neo4j", "Neo4j", "ready", "图数据库连接正常。", blocking=True)


def _probe_qdrant_status(env: Mapping[str, str], *, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return _service_status("qdrant", "Qdrant", "ready", "未启用向量检索，不阻塞。", blocking=False)
    url = _normalize_text(env.get("QDRANT_URL", DEFAULT_QDRANT_URL)) or DEFAULT_QDRANT_URL
    try:
        response = httpx.get(
            f"{url.rstrip('/')}/collections",
            timeout=STARTUP_HEALTH_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except Exception as exc:
        return _service_status("qdrant", "Qdrant", _service_failure_status(exc), f"Qdrant 未就绪：{exc}", blocking=True)
    return _service_status("qdrant", "Qdrant", "ready", "向量检索服务正常。", blocking=True)


def _rerank_health_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/rerank"):
        return normalized[: -len("/rerank")] + "/healthz"
    return normalized + "/healthz"


def _probe_rerank_status(env: Mapping[str, str], *, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return _service_status("rerank", "Rerank", "ready", "未启用 rerank，不阻塞。", blocking=False)
    provider = _normalize_text(env.get("RERANK_PROVIDER", "local")).lower() or "local"
    if provider != "local":
        return _service_status(
            "rerank",
            "Rerank Remote",
            "ready",
            "远程 rerank 将在“测试配置”阶段验证，不依赖本地 Docker 健康检查。",
            blocking=True,
        )
    base_url = _normalize_text(env.get("RERANK_BASE_URL", DEFAULT_RERANK_BASE_URL)) or DEFAULT_RERANK_BASE_URL
    try:
        response = httpx.get(_rerank_health_url(base_url), timeout=STARTUP_HEALTH_TIMEOUT_SECONDS)
        response.raise_for_status()
    except Exception as exc:
        return _service_status("rerank", "Rerank Local", _service_failure_status(exc), f"Rerank Local 未就绪：{exc}", blocking=True)
    return _service_status("rerank", "Rerank Local", "ready", "本地 rerank 服务正常。", blocking=True)


def _probe_app_backend_status() -> dict[str, Any]:
    return _service_status("backend", "App Backend", "ready", "当前后端会话已连接。", blocking=True)


def probe_startup_services(
    settings: Mapping[str, object],
    env: Mapping[str, str] | None = None,
    path: str | Path | None = None,
) -> list[dict[str, Any]]:
    candidate_env = _candidate_runtime_env(settings, env=env, path=path)
    vector_enabled = True
    rerank_enabled = True
    return [
        _probe_mysql_status(candidate_env),
        _probe_neo4j_status(candidate_env),
        _probe_qdrant_status(candidate_env, enabled=vector_enabled),
        _probe_rerank_status(candidate_env, enabled=rerank_enabled),
        _probe_app_backend_status(),
    ]


def _test_ark_llm(env: Mapping[str, str]) -> None:
    client = OpenAI(
        api_key=_normalize_text(env.get("LLM_API_KEY", "")),
        base_url=_normalize_text(env.get("LLM_BASE_URL", ARK_CODING_BASE_URL)) or ARK_CODING_BASE_URL,
        timeout=STARTUP_LLM_TIMEOUT_SECONDS,
    )
    response = client.chat.completions.create(
        model=_normalize_text(env.get("LLM_MODEL", "")),
        messages=[{"role": "user", "content": "reply with ok"}],
        max_tokens=8,
        temperature=0,
    )
    content = ""
    if getattr(response, "choices", None):
        content = str(response.choices[0].message.content or "").strip().lower()
    if not content:
        raise RuntimeError("LLM returned an empty response.")


def _embedding_endpoint_mode(env: Mapping[str, str]) -> str:
    return _normalize_text(env.get("EMBED_ENDPOINT_MODE", "auto")).lower() or "auto"


def _embedding_prefers_multimodal(env: Mapping[str, str]) -> bool:
    endpoint_mode = _embedding_endpoint_mode(env)
    if endpoint_mode in {"multimodal", "mm"}:
        return True
    if endpoint_mode in {"text", "standard"}:
        return False
    return "vision" in _normalize_text(env.get("EMBED_MODEL", "")).lower()


def _should_retry_embedding_with_multimodal(exc: Exception, env: Mapping[str, str]) -> bool:
    endpoint_mode = _embedding_endpoint_mode(env)
    if endpoint_mode in {"text", "standard"}:
        return False
    message = str(exc).lower()
    return (
        "does not support this api" in message
        or ("invalidparameter" in message and "model" in message)
    )


def _embedding_response_has_dense_vector(body: Any) -> bool:
    if not isinstance(body, dict):
        return False

    def _has_vector(candidate: Any) -> bool:
        if isinstance(candidate, list) and candidate and isinstance(candidate[0], (int, float)):
            return True
        if isinstance(candidate, list) and candidate and isinstance(candidate[0], list):
            first = candidate[0]
            return bool(first) and isinstance(first[0], (int, float))
        if isinstance(candidate, dict):
            nested = candidate.get("dense") or candidate.get("vector") or candidate.get("values")
            return isinstance(nested, list) and bool(nested) and isinstance(nested[0], (int, float))
        return False

    def _scan(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        candidates = [
            item.get("embedding"),
            item.get("dense_embedding"),
            item.get("dense_vector"),
            item.get("vector"),
        ]
        nested = item.get("embeddings")
        if isinstance(nested, dict):
            candidates.extend(
                [
                    nested.get("dense"),
                    nested.get("embedding"),
                    nested.get("vector"),
                ]
            )
        return any(_has_vector(candidate) for candidate in candidates)

    data = body.get("data")
    if isinstance(data, dict):
        return _scan(data)
    if isinstance(data, list):
        return any(_scan(item) for item in data)
    return False


def _test_ark_embedding_via_multimodal(env: Mapping[str, str]) -> None:
    base_url = _normalize_text(env.get("EMBED_BASE_URL", ARK_BASE_URL)) or ARK_BASE_URL
    payload: dict[str, Any] = {
        "model": _normalize_text(env.get("EMBED_MODEL", "")),
        "input": [{"type": "text", "text": "hello from story2memory"}],
        "encoding_format": "float",
    }
    dimensions_raw = _normalize_text(env.get("EMBED_DIMENSIONS", ""))
    if dimensions_raw:
        try:
            dimensions = int(dimensions_raw)
            if dimensions > 0:
                payload["dimensions"] = dimensions
        except ValueError:
            pass
    instructions = _normalize_text(env.get("EMBED_INSTRUCTIONS", ""))
    if instructions:
        payload["instructions"] = instructions

    response = httpx.post(
        f"{base_url.rstrip('/')}/embeddings/multimodal",
        headers={
            "Authorization": f"Bearer {_normalize_text(env.get('EMBED_API_KEY', ''))}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=STARTUP_LLM_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    try:
        body = response.json()
    except Exception as exc:  # pragma: no cover - defensive branch
        raise RuntimeError("Multimodal embedding returned non-JSON response.") from exc
    if not _embedding_response_has_dense_vector(body):
        raise RuntimeError("Multimodal embedding API returned no dense vectors.")


def _test_ark_embedding(env: Mapping[str, str]) -> None:
    if _embedding_prefers_multimodal(env):
        _test_ark_embedding_via_multimodal(env)
        return

    client = OpenAI(
        api_key=_normalize_text(env.get("EMBED_API_KEY", "")),
        base_url=_normalize_text(env.get("EMBED_BASE_URL", ARK_BASE_URL)) or ARK_BASE_URL,
        timeout=STARTUP_LLM_TIMEOUT_SECONDS,
    )
    try:
        response = client.embeddings.create(
            model=_normalize_text(env.get("EMBED_MODEL", "")),
            input=["hello from story2memory"],
        )
    except Exception as exc:
        if not _should_retry_embedding_with_multimodal(exc, env):
            raise
        _test_ark_embedding_via_multimodal(env)
        return
    data = getattr(response, "data", None) or []
    if not data:
        raise RuntimeError("Embedding API returned no vectors.")


def _test_local_rerank(env: Mapping[str, str]) -> None:
    base_url = _normalize_text(env.get("RERANK_BASE_URL", DEFAULT_RERANK_BASE_URL)) or DEFAULT_RERANK_BASE_URL
    response = httpx.post(
        base_url,
        json={
            "model": _normalize_text(env.get("RERANK_MODEL", DEFAULT_RERANK_MODEL)) or DEFAULT_RERANK_MODEL,
            "query": "谁更相关",
            "documents": ["第一条文档", "第二条文档"],
            "top_n": 1,
        },
        timeout=STARTUP_HEALTH_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload.get("results"), list):
        raise RuntimeError("Rerank response is missing results.")


def _test_qwen_rerank(env: Mapping[str, str]) -> None:
    base_url = _normalize_text(env.get("RERANK_BASE_URL", DEFAULT_QWEN_RERANK_BASE_URL)) or DEFAULT_QWEN_RERANK_BASE_URL
    api_key = _normalize_text(env.get("RERANK_API_KEY", "")) or _normalize_text(env.get("DASHSCOPE_API_KEY", ""))
    if not api_key or _is_placeholder_value("rerank_api_key", api_key):
        raise RuntimeError("Missing RERANK_API_KEY for qwen rerank.")
    payload: dict[str, Any] = {
        "model": _normalize_text(env.get("RERANK_MODEL", DEFAULT_QWEN_RERANK_MODEL)) or DEFAULT_QWEN_RERANK_MODEL,
        "query": "什么是文本排序模型",
        "documents": [
            "文本排序模型广泛用于搜索引擎和推荐系统中，它们根据文本相关性对候选文本进行排序",
            "量子计算是计算科学的一个前沿领域",
            "预训练语言模型的发展给文本排序模型带来了新的进展",
        ],
        "top_n": 2,
    }
    instruction = _normalize_text(env.get("RERANK_INSTRUCTION", ""))
    if instruction:
        payload["instruct"] = instruction
    response = httpx.post(
        base_url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=STARTUP_LLM_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()
    results = body.get("results") if isinstance(body, dict) else None
    if not isinstance(results, list):
        output = body.get("output") if isinstance(body, dict) else None
        if isinstance(output, dict):
            results = output.get("results")
    if not isinstance(results, list) or not results:
        raise RuntimeError("Qwen rerank response is missing results.")


def _test_openai_compatible_rerank(env: Mapping[str, str]) -> None:
    base_url = _normalize_text(env.get("RERANK_BASE_URL", "")) or "http://127.0.0.1:8007/v1"
    endpoint = base_url.rstrip("/")
    if endpoint.endswith("/v1"):
        endpoint = f"{endpoint}/rerank"
    api_key = _normalize_text(env.get("RERANK_API_KEY", "")) or "EMPTY"
    response = httpx.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": _normalize_text(env.get("RERANK_MODEL", "")) or "bge-reranker-v2-m3",
            "query": "谁更相关",
            "documents": ["第一条文档", "第二条文档"],
            "top_n": 1,
        },
        timeout=STARTUP_LLM_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise RuntimeError("OpenAI-compatible rerank response is missing results.")


def _test_rerank(env: Mapping[str, str]) -> None:
    provider = _normalize_text(env.get("RERANK_PROVIDER", "local")).lower() or "local"
    base_url = _normalize_text(env.get("RERANK_BASE_URL", ""))
    if provider == "local":
        _test_local_rerank(env)
        return
    if provider == "qwen" or "dashscope.aliyuncs.com" in base_url.lower() or base_url.rstrip("/").endswith("/v1/reranks"):
        _test_qwen_rerank(env)
        return
    _test_openai_compatible_rerank(env)


def _ark_error_group(exc: Exception, *, model_label: str) -> str:
    status_code = getattr(exc, "status_code", None)
    message = str(exc).lower()
    if status_code in {401, 403}:
        return "Ark Key 无效或无权限"
    if any(marker in message for marker in ("api key", "authentication", "unauthorized", "permission denied", "forbidden")):
        return "Ark Key 无效或无权限"
    return f"{model_label} 模型不可用"


def test_startup_settings(
    settings: Mapping[str, object],
    env: Mapping[str, str] | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    validation_errors = validate_startup_settings(settings)
    services = probe_startup_services(settings, env=env, path=path)
    if validation_errors:
        return {
            "ok": False,
            "services": services,
            "group": "配置未完成",
            "message": "\n".join(validation_errors),
        }

    blocking_failures = [item for item in services if bool(item.get("blocking")) and str(item.get("status")) != "ready"]
    if blocking_failures:
        labels = "、".join(str(item.get("label", "")) for item in blocking_failures)
        return {
            "ok": False,
            "services": services,
            "group": "服务未启动",
            "message": f"{labels} 尚未就绪，请先等待 Docker 依赖启动完成。",
        }

    candidate_env = _candidate_runtime_env(settings, env=env, path=path)

    try:
        _test_ark_llm(candidate_env)
    except Exception as exc:
        return {
            "ok": False,
            "services": services,
            "group": _ark_error_group(exc, model_label="LLM"),
            "message": f"LLM 测试失败：{exc}",
        }

    try:
        _test_ark_embedding(candidate_env)
    except Exception as exc:
        return {
            "ok": False,
            "services": services,
            "group": _ark_error_group(exc, model_label="Embedding"),
            "message": f"Embedding 测试失败：{exc}",
        }

    try:
        _test_rerank(candidate_env)
    except Exception as exc:
        return {
            "ok": False,
            "services": services,
            "group": "Rerank 不可用",
            "message": f"Rerank 测试失败：{exc}",
        }

    return {
        "ok": True,
        "services": services,
        "group": "",
        "message": "Ark 配置测试通过，当前环境已经可以开始使用。",
    }


def refresh_runtime_clients(
    settings: Mapping[str, object],
    env: Mapping[str, str] | None = None,
    path: str | Path | None = None,
) -> list[str]:
    candidate_env = _candidate_runtime_env(settings, env=env, path=path)
    for key, value in build_runtime_env_map(settings).items():
        os.environ[key] = value

    cleared: list[str] = []

    def _clear(label: str, target: object) -> None:
        cache_clear = getattr(target, "cache_clear", None)
        if callable(cache_clear):
            cache_clear()
            cleared.append(label)

    try:
        import agent.chat_agent as chat_agent
        import agent.cosplay_agent as cosplay_agent
        import agent.deepSearch as deep_search
        import agent.hybridSearch as hybrid_search
        import agent.searchAgent as search_agent
        import agent.skills.retrieval_route_skill.route_skill as route_skill
        import database.qdrant_client as qdrant_client
        import rag.entity_qdrant_sync as entity_qdrant_sync

        _clear("agent.chat_agent.get_chat_agent", chat_agent.get_chat_agent)
        _clear("agent.cosplay_agent.get_cosplay_agent", cosplay_agent.get_cosplay_agent)
        _clear("database.qdrant_client._get_embedding_client", qdrant_client._get_embedding_client)
        _clear("database.qdrant_client.get_qdrant_embedding_store", qdrant_client.get_qdrant_embedding_store)
        _clear("agent.hybridSearch._get_embedding_query_client", hybrid_search._get_embedding_query_client)
        _clear("agent.hybridSearch._get_entity_embedding_query_client", hybrid_search._get_entity_embedding_query_client)
        _clear("agent.hybridSearch._get_hybrid_filter_llm", hybrid_search._get_hybrid_filter_llm)
        _clear("agent.hybridSearch._get_rerank_client", hybrid_search._get_rerank_client)
        _clear("agent.deepSearch._get_llm", deep_search._get_llm)
        _clear("agent.skills.retrieval_route_skill.route_skill._build_route_llm", route_skill._build_route_llm)
        _clear("agent.searchAgent._get_recovery_planner_llm", search_agent._get_recovery_planner_llm)
        _clear("agent.searchAgent.get_agentic_research_graph", search_agent.get_agentic_research_graph)
        _clear("rag.entity_qdrant_sync._get_entity_embedding_client", entity_qdrant_sync._get_entity_embedding_client)
    except Exception:
        # The configuration was already applied to os.environ; callers can decide how to surface
        # cache-clear failures if they happen at runtime.
        raise

    return cleared


def request_runtime_restart(delay_seconds: float = 0.35) -> None:
    def _restart() -> None:
        time.sleep(max(0.0, float(delay_seconds)))
        os._exit(0)

    threading.Thread(target=_restart, daemon=True).start()


def validate_public_runtime_env(env: Mapping[str, str] | None = None) -> list[str]:
    source = env if env is not None else os.environ
    errors: list[str] = []
    for key, placeholders in PLACEHOLDER_SECRET_VALUES.items():
        value = str(source.get(key, "") or "").strip()
        if not value:
            errors.append(f"{key} is required for the public Docker deployment.")
            continue
        if value in placeholders:
            errors.append(f"{key} still uses the example placeholder value.")
    return errors


def is_agent_runtime_prewarm_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = _merge_runtime_env(env=env)
    raw = str(source.get("AGENT_RUNTIME_PREWARM_ENABLED", "0") or "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def main() -> int:
    errors = validate_public_runtime_env()
    if not errors:
        return 0
    print("Public runtime configuration error:", file=sys.stderr)
    for item in errors:
        print(f"- {item}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
