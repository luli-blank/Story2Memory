from __future__ import annotations

from functools import lru_cache
from typing import Any, Callable

from neo4j import GraphDatabase

from .config import get_neo4j_config


@lru_cache(maxsize=1)
def get_driver():
    config = get_neo4j_config()
    return GraphDatabase.driver(config.uri, auth=(config.user, config.password))


def run_read(fn: Callable[..., Any], *args, **kwargs):
    driver = get_driver()
    with driver.session() as session:
        return session.execute_read(fn, *args, **kwargs)


def run_write(fn: Callable[..., Any], *args, **kwargs):
    driver = get_driver()
    with driver.session() as session:
        return session.execute_write(fn, *args, **kwargs)
