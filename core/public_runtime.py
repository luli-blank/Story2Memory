from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from dotenv import dotenv_values

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_OVERRIDE_VAR = "STORY2MEMORY_ENV_OVERRIDE"
DEFAULT_RUNTIME_OVERRIDE_PATH = ROOT_DIR / "data" / "config" / "runtime.env"

STARTUP_PLACEHOLDER_VALUES: dict[str, tuple[str, ...]] = {
    "LLM_API_KEY": ("your-llm-api-key", ""),
    "LLM_BASE_URL": ("https://your-llm-base-url", ""),
    "LLM_MODEL": ("your-llm-model", ""),
    "EMBED_API_KEY": ("your-embedding-api-key", ""),
    "EMBED_BASE_URL": ("https://your-embedding-base-url", ""),
    "EMBED_MODEL": ("your-embedding-model", ""),
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
    "AGENT_RUNTIME_PREWARM_ENABLED",
)

DEFAULT_STARTUP_SETTINGS: dict[str, Any] = {
    "llm_api_key": "",
    "llm_base_url": "",
    "llm_model": "",
    "vector_retrieval_enabled": False,
    "embed_api_key": "",
    "embed_base_url": "",
    "embed_model": "",
    "qdrant_url": "http://qdrant:6333",
    "rerank_enabled": False,
    "rerank_provider": "local",
    "rerank_base_url": "http://rerank-local:8000/rerank",
    "rerank_api_key": "",
    "rerank_model": "BAAI/bge-reranker-v2-m3",
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


def _is_placeholder_value(env_key: str, value: object) -> bool:
    normalized = _normalize_text(value)
    return normalized in STARTUP_PLACEHOLDER_VALUES.get(env_key, ())


def _requires_rerank_api_key(provider: str, base_url: str) -> bool:
    normalized_provider = _normalize_text(provider).lower()
    normalized_base = _normalize_text(base_url).lower()
    if normalized_provider == "local":
        return False
    if any(marker in normalized_base for marker in ("rerank-local", "127.0.0.1", "localhost")):
        return False
    return True


def build_startup_settings(
    env: Mapping[str, str] | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    merged = _merge_runtime_env(env=env, path=path)
    defaults = dict(DEFAULT_STARTUP_SETTINGS)
    defaults.update(
        {
            "llm_api_key": _normalize_text(merged.get("LLM_API_KEY", defaults["llm_api_key"])),
            "llm_base_url": _normalize_text(merged.get("LLM_BASE_URL", defaults["llm_base_url"])),
            "llm_model": _normalize_text(merged.get("LLM_MODEL", defaults["llm_model"])),
            "vector_retrieval_enabled": _is_truthy(merged.get("HYBRID_DENSE_RETRIEVAL_ENABLED", "0")),
            "embed_api_key": _normalize_text(merged.get("EMBED_API_KEY", defaults["embed_api_key"])),
            "embed_base_url": _normalize_text(merged.get("EMBED_BASE_URL", defaults["embed_base_url"])),
            "embed_model": _normalize_text(merged.get("EMBED_MODEL", defaults["embed_model"])),
            "qdrant_url": _normalize_text(merged.get("QDRANT_URL", defaults["qdrant_url"])),
            "rerank_enabled": not _is_truthy(merged.get("RERANK_DISABLED", "1")),
            "rerank_provider": _normalize_text(merged.get("RERANK_PROVIDER", defaults["rerank_provider"])) or defaults["rerank_provider"],
            "rerank_base_url": _normalize_text(merged.get("RERANK_BASE_URL", defaults["rerank_base_url"])) or defaults["rerank_base_url"],
            "rerank_api_key": _normalize_text(merged.get("RERANK_API_KEY", defaults["rerank_api_key"])),
            "rerank_model": _normalize_text(merged.get("RERANK_MODEL", defaults["rerank_model"])) or defaults["rerank_model"],
            "prewarm_enabled": _is_truthy(merged.get("AGENT_RUNTIME_PREWARM_ENABLED", "0")),
        }
    )
    return defaults


def validate_startup_settings(settings: Mapping[str, object]) -> list[str]:
    errors: list[str] = []

    def _require(settings_key: str, env_key: str) -> None:
        value = settings.get(settings_key, "")
        if not _normalize_text(value) or _is_placeholder_value(env_key, value):
            errors.append(f"{env_key} is required.")

    _require("llm_api_key", "LLM_API_KEY")
    _require("llm_base_url", "LLM_BASE_URL")
    _require("llm_model", "LLM_MODEL")

    if bool(settings.get("vector_retrieval_enabled", False)):
        _require("embed_api_key", "EMBED_API_KEY")
        _require("embed_base_url", "EMBED_BASE_URL")
        _require("embed_model", "EMBED_MODEL")

    if bool(settings.get("rerank_enabled", False)):
        rerank_provider = _normalize_text(settings.get("rerank_provider", ""))
        rerank_base_url = _normalize_text(settings.get("rerank_base_url", ""))
        _require("rerank_base_url", "RERANK_BASE_URL")
        _require("rerank_model", "RERANK_MODEL")
        if _requires_rerank_api_key(rerank_provider, rerank_base_url):
            _require("rerank_api_key", "RERANK_API_KEY")

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
    env_map = {
        "LLM_API_KEY": _normalize_text(settings.get("llm_api_key", "")),
        "LLM_BASE_URL": _normalize_text(settings.get("llm_base_url", "")),
        "LLM_MODEL": _normalize_text(settings.get("llm_model", "")),
        "HYBRID_DENSE_RETRIEVAL_ENABLED": "1" if bool(settings.get("vector_retrieval_enabled", False)) else "0",
        "EMBED_API_KEY": _normalize_text(settings.get("embed_api_key", "")),
        "EMBED_BASE_URL": _normalize_text(settings.get("embed_base_url", "")),
        "EMBED_MODEL": _normalize_text(settings.get("embed_model", "")),
        "QDRANT_URL": _normalize_text(settings.get("qdrant_url", DEFAULT_STARTUP_SETTINGS["qdrant_url"])) or DEFAULT_STARTUP_SETTINGS["qdrant_url"],
        "RERANK_DISABLED": "0" if bool(settings.get("rerank_enabled", False)) else "1",
        "RERANK_PROVIDER": _normalize_text(settings.get("rerank_provider", DEFAULT_STARTUP_SETTINGS["rerank_provider"])) or DEFAULT_STARTUP_SETTINGS["rerank_provider"],
        "RERANK_BASE_URL": _normalize_text(settings.get("rerank_base_url", DEFAULT_STARTUP_SETTINGS["rerank_base_url"])) or DEFAULT_STARTUP_SETTINGS["rerank_base_url"],
        "RERANK_API_KEY": _normalize_text(settings.get("rerank_api_key", "")),
        "RERANK_MODEL": _normalize_text(settings.get("rerank_model", DEFAULT_STARTUP_SETTINGS["rerank_model"])) or DEFAULT_STARTUP_SETTINGS["rerank_model"],
        "AGENT_RUNTIME_PREWARM_ENABLED": "1" if bool(settings.get("prewarm_enabled", False)) else "0",
    }
    lines = ["# Story2Memory runtime startup config"]
    lines.extend(_env_line(key, env_map[key]) for key in STARTUP_MANAGED_ENV_ORDER)
    target_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target_path


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
