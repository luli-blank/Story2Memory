from __future__ import annotations

import os
from typing import Mapping, Any
from urllib.parse import quote, unquote, urlparse

DEFAULT_MYSQL_HOST = "mysql"
DEFAULT_MYSQL_PORT = 3306
DEFAULT_MYSQL_DATABASE = "novel_cognition"
DEFAULT_MYSQL_USER = "story2memory"
PLACEHOLDER_MYSQL_PASSWORD = "change-me-story2memory-db-password"

EXAMPLE_MYSQL_DSN = (
    "mysql+pymysql://"
    f"{DEFAULT_MYSQL_USER}:{PLACEHOLDER_MYSQL_PASSWORD}"
    f"@{DEFAULT_MYSQL_HOST}:{DEFAULT_MYSQL_PORT}/{DEFAULT_MYSQL_DATABASE}"
)


def parse_mysql_dsn(dsn: str) -> dict[str, Any] | None:
    normalized = str(dsn or "").strip()
    if normalized.startswith("mysql+pymysql://"):
        normalized = "mysql://" + normalized.split("://", 1)[1]

    parsed = urlparse(normalized)
    if parsed.scheme != "mysql":
        return None

    database = parsed.path.lstrip("/")
    if not (parsed.hostname and parsed.username and database):
        return None

    return {
        "host": parsed.hostname,
        "port": parsed.port or DEFAULT_MYSQL_PORT,
        "user": unquote(parsed.username),
        "password": unquote(parsed.password or ""),
        "database": unquote(database),
    }


def build_mysql_dsn(env: Mapping[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    user = str(source.get("MYSQL_USER", DEFAULT_MYSQL_USER) or DEFAULT_MYSQL_USER).strip() or DEFAULT_MYSQL_USER
    password = str(source.get("MYSQL_PASSWORD", "") or "").strip()
    database = str(source.get("MYSQL_DATABASE", DEFAULT_MYSQL_DATABASE) or DEFAULT_MYSQL_DATABASE).strip() or DEFAULT_MYSQL_DATABASE
    host = str(source.get("MYSQL_HOST", DEFAULT_MYSQL_HOST) or DEFAULT_MYSQL_HOST).strip() or DEFAULT_MYSQL_HOST
    port = str(source.get("MYSQL_PORT", DEFAULT_MYSQL_PORT) or DEFAULT_MYSQL_PORT).strip() or str(DEFAULT_MYSQL_PORT)

    return (
        "mysql+pymysql://"
        f"{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{quote(database, safe='')}"
    )


def has_mysql_env_config(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    for key in ("MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE", "MYSQL_HOST", "MYSQL_PORT"):
        if str(source.get(key, "") or "").strip():
            return True
    return False


def is_placeholder_mysql_dsn(dsn: str) -> bool:
    normalized = str(dsn or "").strip()
    if not normalized:
        return True
    if normalized == EXAMPLE_MYSQL_DSN:
        return True
    return PLACEHOLDER_MYSQL_PASSWORD in normalized


def resolve_mysql_dsn(env: Mapping[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    raw = str(source.get("MYSQL_DSN", "") or "").strip()
    if raw and not is_placeholder_mysql_dsn(raw):
        return raw
    if not raw and not has_mysql_env_config(source):
        return ""
    return build_mysql_dsn(source)
