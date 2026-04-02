from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import unquote, urlparse

import pymysql
from dotenv import load_dotenv

from .client import run_read, run_write
from .merge import build_edge_key, build_event_key, resolve_node_match
from .models import SyncResult

logger = logging.getLogger(__name__)


def _resolve_mysql_params() -> dict[str, Any] | None:
    load_dotenv()
    override_path = str(os.getenv("STORY2MEMORY_ENV_OVERRIDE", "") or "").strip()
    if override_path:
        load_dotenv(override_path, override=True)
    dsn = str(os.getenv("MYSQL_DSN", "") or "").strip()
    if dsn.startswith("mysql+pymysql://"):
        dsn = "mysql://" + dsn.split("://", 1)[1]
    parsed = urlparse(dsn)
    database = parsed.path.lstrip("/")
    if parsed.scheme != "mysql" or not parsed.hostname or not parsed.username or not database:
        return None
    return {
        "host": parsed.hostname,
        "port": parsed.port or 3306,
        "user": unquote(parsed.username),
        "password": unquote(parsed.password or ""),
        "database": unquote(database),
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
        "autocommit": True,
    }


def _load_book_title(book_id: int) -> str:
    params = _resolve_mysql_params()
    if not params:
        return ""
    try:
        conn = pymysql.connect(**params)
        with conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT title FROM books WHERE id = %s LIMIT 1", (int(book_id),))
                row = cursor.fetchone() or {}
        return str(row.get("title") or "").strip()
    except Exception:
        logger.exception("Failed to load book title for graph sync: book_id=%s", book_id)
        return ""


def _read_character_candidates(tx, book_id: int, names: list[str]) -> list[dict[str, Any]]:
    rows = tx.run(
        """
        MATCH (c:Character {book_id: $book_id})
        WHERE c.name IN $names OR any(alias IN coalesce(c.aliases, []) WHERE alias IN $names)
        RETURN c.graph_key AS graph_key,
               c.book_id AS book_id,
               c.character_id AS character_id,
               c.name AS name,
               coalesce(c.aliases, []) AS aliases,
               coalesce(c.profile_status, 'stub') AS profile_status
        """,
        book_id=int(book_id),
        names=list(sorted({str(item or "").strip() for item in names if str(item or "").strip()})),
    )
    return [record.data() for record in rows]


def _upsert_book(tx, book_id: int, book_title: str) -> None:
    tx.run(
        """
        MERGE (b:Book {book_id: $book_id})
        SET b.title = $title
        """,
        book_id=int(book_id),
        title=str(book_title or ""),
    )


def _upsert_character(tx, payload: dict[str, Any]) -> None:
    tx.run(
        """
        MERGE (c:Character {graph_key: $graph_key})
        SET c.book_id = $book_id,
            c.character_id = $character_id,
            c.name = $name,
            c.aliases = $aliases,
            c.profile_status = $profile_status,
            c.current_state_summary = $current_state_summary,
            c.first_chapter_index = $first_chapter_index,
            c.last_chapter_index = $last_chapter_index,
            c.version_hash = $version_hash
        """,
        **payload,
    )


def _replace_relation_edge(tx, payload: dict[str, Any], history: list[dict[str, Any]]) -> None:
    tx.run(
        """
        MATCH (:Character {graph_key: $source_graph_key})-[r:RELATES_TO {edge_key: $edge_key}]->(:Character {graph_key: $target_graph_key})
        DELETE r
        """,
        edge_key=payload["edge_key"],
        source_graph_key=payload["source_graph_key"],
        target_graph_key=payload["target_graph_key"],
    )
    tx.run(
        """
        MATCH (s:Character {graph_key: $source_graph_key})
        MATCH (t:Character {graph_key: $target_graph_key})
        MERGE (s)-[r:RELATES_TO {edge_key: $edge_key}]->(t)
        SET r.book_id = $book_id,
            r.summary = $summary,
            r.structural_relation = $structural_relation,
            r.action_relation = $action_relation,
            r.emotional_relation = $emotional_relation,
            r.directionality = $directionality,
            r.stability = $stability,
            r.current_status = $current_status,
            r.drivers = $drivers,
            r.first_chapter_index = $first_chapter_index,
            r.last_chapter_index = $last_chapter_index,
            r.version_hash = $version_hash
        """,
        **payload,
    )
    tx.run(
        """
        MATCH (e:RelationEvent {edge_key: $edge_key})
        DETACH DELETE e
        """,
        edge_key=payload["edge_key"],
    )
    for event in history:
        tx.run(
            """
            MATCH (s:Character {graph_key: $source_graph_key})
            MATCH (t:Character {graph_key: $target_graph_key})
            MERGE (e:RelationEvent {event_key: $event_key})
            SET e.book_id = $book_id,
                e.edge_key = $edge_key,
                e.source_graph_key = $source_graph_key,
                e.target_graph_key = $target_graph_key,
                e.chapter_start = $chapter_start,
                e.chapter_end = $chapter_end,
                e.relation_type = $relation_type,
                e.polarity = $polarity,
                e.strength = $strength,
                e.directionality = $directionality,
                e.stability = $stability,
                e.current_status = $current_status,
                e.summary = $summary,
                e.evidence_chapters = $evidence_chapters
            MERGE (s)-[:HAS_RELATION_EVENT]->(e)
            MERGE (e)-[:TARGETS]->(t)
            """,
            **event,
        )


def sync_character_relation_subgraph(
    *,
    book_id: int,
    book_title: str = "",
    character_row: dict[str, Any],
    profile_json: dict[str, Any] | None,
    relations: list[dict[str, Any]],
    version_hash: str,
    book_character_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    effective_book_title = str(book_title or "").strip() or _load_book_title(int(book_id))
    profile_payload = profile_json if isinstance(profile_json, dict) else {}
    identity_payload = profile_payload.get("identity", {}) if isinstance(profile_payload.get("identity"), dict) else {}
    source_aliases = [str(item or "").strip() for item in identity_payload.get("aliases") or character_row.get("aliases") or [] if str(item or "").strip()]
    candidate_names = [str(character_row.get("name") or "").strip(), *source_aliases]
    for row in relations:
        candidate_names.append(str(row.get("target_character_name") or "").strip())

    candidates = run_read(_read_character_candidates, int(book_id), candidate_names)
    source_graph_key, source_reused = resolve_node_match(
        book_id=int(book_id),
        character_id=int(character_row.get("id") or 0) or None,
        name=str(character_row.get("name") or "").strip(),
        aliases=source_aliases,
        candidates=candidates,
    )

    upgraded_stub_keys: list[str] = []
    if source_reused and source_graph_key.startswith("stub:"):
        upgraded_stub_keys.append(source_graph_key)

    source_payload = {
        "graph_key": source_graph_key,
        "book_id": int(book_id),
        "character_id": int(character_row.get("id") or 0) or None,
        "name": str(character_row.get("name") or "").strip(),
        "aliases": source_aliases,
        "profile_status": "ready",
        "current_state_summary": str(identity_payload.get("summary") or "").strip(),
        "first_chapter_index": int(identity_payload.get("first_chapter_index") or 0),
        "last_chapter_index": int(identity_payload.get("last_chapter_index") or 0),
        "version_hash": str(version_hash or ""),
    }

    relation_payloads: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = []
    alias_lookup: dict[int, list[str]] = {}
    for row in book_character_rows or []:
        alias_lookup[int(row.get("id") or 0)] = [
            str(item or "").strip()
            for item in row.get("aliases") or []
            if str(item or "").strip()
        ]

    for row in relations:
        target_character_id = int(row.get("target_character_id") or 0) or None
        target_aliases = alias_lookup.get(int(target_character_id or 0), [])
        target_graph_key, target_reused = resolve_node_match(
            book_id=int(book_id),
            character_id=target_character_id,
            name=str(row.get("target_character_name") or "").strip(),
            aliases=target_aliases,
            candidates=candidates,
        )
        if target_reused and target_graph_key.startswith("stub:"):
            upgraded_stub_keys.append(target_graph_key)

        target_payload = {
            "graph_key": target_graph_key,
            "book_id": int(book_id),
            "character_id": target_character_id,
            "name": str(row.get("target_character_name") or "").strip(),
            "aliases": target_aliases,
            "profile_status": "ready" if target_character_id else "stub",
            "current_state_summary": "",
            "first_chapter_index": int(row.get("first_chapter_index") or 0),
            "last_chapter_index": int(row.get("last_chapter_index") or 0),
            "version_hash": str(version_hash or ""),
        }
        edge_key = build_edge_key(int(book_id), source_graph_key, target_graph_key)
        edge_payload = {
            "edge_key": edge_key,
            "book_id": int(book_id),
            "source_graph_key": source_graph_key,
            "target_graph_key": target_graph_key,
            "summary": str(row.get("summary") or "").strip(),
            "structural_relation": [str(item or "").strip() for item in row.get("structural_relation") or [] if str(item or "").strip()],
            "action_relation": [str(item or "").strip() for item in row.get("action_relation") or [] if str(item or "").strip()],
            "emotional_relation": [str(item or "").strip() for item in row.get("emotional_relation") or [] if str(item or "").strip()],
            "directionality": str(row.get("directionality") or "").strip(),
            "stability": str(row.get("stability") or "").strip(),
            "current_status": str(row.get("current_status") or "").strip(),
            "drivers": [str(item or "").strip() for item in row.get("drivers") or [] if str(item or "").strip()],
            "first_chapter_index": int(row.get("first_chapter_index") or 0),
            "last_chapter_index": int(row.get("last_chapter_index") or 0),
            "version_hash": str(version_hash or ""),
        }
        history_payload = []
        for item in row.get("history_json") or []:
            history_payload.append(
                {
                    "event_key": build_event_key(edge_key, item),
                    "book_id": int(book_id),
                    "edge_key": edge_key,
                    "source_graph_key": source_graph_key,
                    "target_graph_key": target_graph_key,
                    "chapter_start": int(item.get("chapter_start") or 0),
                    "chapter_end": int(item.get("chapter_end") or 0),
                    "relation_type": str(item.get("relation_type") or "").strip(),
                    "polarity": str(item.get("polarity") or "neutral").strip() or "neutral",
                    "strength": str(item.get("strength") or "medium").strip() or "medium",
                    "directionality": str(item.get("directionality") or "").strip(),
                    "stability": str(item.get("stability") or "").strip(),
                    "current_status": str(item.get("current_status") or "").strip(),
                    "summary": str(item.get("summary") or "").strip(),
                    "evidence_chapters": [int(value) for value in item.get("evidence_chapters") or [] if int(value) > 0],
                }
            )
        relation_payloads.append((target_payload, edge_payload, history_payload))

    def _write(tx):
        _upsert_book(tx, int(book_id), effective_book_title)
        _upsert_character(tx, source_payload)
        synced_nodes = 1
        synced_edges = 0
        synced_events = 0
        for target_payload, edge_payload, history_payload in relation_payloads:
            _upsert_character(tx, target_payload)
            synced_nodes += 1
            _replace_relation_edge(tx, edge_payload, history_payload)
            synced_edges += 1
            synced_events += len(history_payload)
        return SyncResult(
            status="success",
            synced_nodes=synced_nodes,
            synced_edges=synced_edges,
            synced_events=synced_events,
            upgraded_stub_keys=sorted(set(upgraded_stub_keys)),
        ).to_dict()

    try:
        return run_write(_write)
    except Exception as exc:
        logger.exception("Failed to sync relation graph subgraph: book_id=%s source_character_id=%s", book_id, character_row.get("id"))
        return SyncResult(status="error", error=str(exc)).to_dict()


def delete_book_relation_graph(book_id: int) -> dict[str, Any]:
    def _delete(tx):
        tx.run(
            """
            MATCH (e:RelationEvent {book_id: $book_id})
            DETACH DELETE e
            """,
            book_id=int(book_id),
        )
        tx.run(
            """
            MATCH (c:Character {book_id: $book_id})
            DETACH DELETE c
            """,
            book_id=int(book_id),
        )
        tx.run(
            """
            MATCH (b:Book {book_id: $book_id})
            DETACH DELETE b
            """,
            book_id=int(book_id),
        )
        return {"book_id": int(book_id), "deleted": 1}

    try:
        return run_write(_delete)
    except Exception as exc:
        logger.exception("Failed to delete relation graph: book_id=%s", book_id)
        return {"book_id": int(book_id), "deleted": 0, "error": str(exc)}
