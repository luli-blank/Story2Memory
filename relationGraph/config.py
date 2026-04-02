from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv


@dataclass(frozen=True)
class Neo4jConfig:
    uri: str
    user: str
    password: str


def _load_env() -> None:
    load_dotenv()
    override_path = str(os.getenv("STORY2MEMORY_ENV_OVERRIDE", "") or "").strip()
    if override_path:
        load_dotenv(override_path, override=True)


@lru_cache(maxsize=1)
def get_neo4j_config() -> Neo4jConfig:
    _load_env()
    uri = str(os.getenv("NEO4J_URI", "") or "").strip()
    user = str(os.getenv("NEO4J_USER", "") or "").strip()
    password = str(os.getenv("NEO4J_PASSWORD", "") or "").strip()
    if not (uri and user and password):
        raise RuntimeError("Neo4j config is incomplete. Expected NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD.")
    return Neo4jConfig(uri=uri, user=user, password=password)
