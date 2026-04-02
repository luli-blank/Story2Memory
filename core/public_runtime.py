from __future__ import annotations

import os
import sys
from typing import Mapping

PLACEHOLDER_SECRET_VALUES: dict[str, tuple[str, ...]] = {
    "MYSQL_ROOT_PASSWORD": ("change-me-mysql-root-password", "change-me-root"),
    "MYSQL_PASSWORD": ("change-me-story2memory-db-password", "story2memory"),
    "NEO4J_PASSWORD": ("change-me-neo4j-password", "change-me-neo4j"),
}


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
    source = env if env is not None else os.environ
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
