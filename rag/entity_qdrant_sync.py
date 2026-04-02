from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv
import httpx
import pymysql


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from database.qdrant_client import OpenAIEmbeddingClient, _clean_text, _stable_point_id, get_qdrant_embedding_store, models

ENV_OVERRIDE_VAR = "STORY2MEMORY_ENV_OVERRIDE"
ENTITY_QDRANT_IF_EXISTS_ENV_VAR = "BOOK_ENTITY_QDRANT_IF_EXISTS"
ENTITY_COLLECTIONS = (
    "characters",
    "origanizations",
    "special_existences",
    "world_rules",
)
SPLIT_RECORD_COLLECTIONS = {"characters", "origanizations"}
ENTITY_EMBED_MODEL = "ep-20260303004802-tlt8f"
ENTITY_EMBED_MULTIMODAL_MAX_CONCURRENCY = 100
ENTITY_EMBED_MAX_RETRIES = 6
ENTITY_EMBED_BASE_DELAY_SECONDS = 2.0
ENTITY_EMBED_MAX_DELAY_SECONDS = 30.0
ENTITY_COLLECTION_IF_EXISTS_ENV_VARS = {
    "characters": "BOOK_ENTITY_QDRANT_CHARACTERS_IF_EXISTS",
    "origanizations": "BOOK_ENTITY_QDRANT_ORIGANIZATIONS_IF_EXISTS",
    "special_existences": "BOOK_ENTITY_QDRANT_SPECIAL_EXISTENCES_IF_EXISTS",
    "world_rules": "BOOK_ENTITY_QDRANT_WORLD_RULES_IF_EXISTS",
}


def _load_runtime_env() -> None:
    load_dotenv(dotenv_path=ROOT_DIR / ".env")
    override_path = os.getenv(ENV_OVERRIDE_VAR)
    if override_path:
        load_dotenv(dotenv_path=override_path, override=True)


def _parse_mysql_dsn(dsn: str) -> dict[str, Any] | None:
    normalized = dsn.strip()
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
        "port": parsed.port or 3306,
        "user": unquote(parsed.username),
        "password": unquote(parsed.password or ""),
        "database": unquote(database),
        "charset": "utf8mb4",
        "autocommit": True,
        "cursorclass": pymysql.cursors.DictCursor,
    }


def _connect_mysql():
    _load_runtime_env()
    dsn = os.getenv("MYSQL_DSN", "").strip()
    cfg = _parse_mysql_dsn(dsn)
    if not cfg:
        raise RuntimeError("Missing or invalid MYSQL_DSN.")
    return pymysql.connect(**cfg)


def _parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _batch(seq: list[str], size: int) -> list[list[str]]:
    step = max(1, int(size))
    return [seq[index : index + step] for index in range(0, len(seq), step)]


def _batch_rows(seq: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    step = max(1, int(size))
    return [seq[index : index + step] for index in range(0, len(seq), step)]


def _build_book_filter(book_id: int | None):
    if models is None or book_id is None:
        return None
    return models.Filter(
        must=[
            models.FieldCondition(
                key="book_id",
                match=models.MatchValue(value=int(book_id)),
            )
        ]
    )


def _resolve_entity_qdrant_if_exists_mode(collection_name: str) -> str:
    override_env = ENTITY_COLLECTION_IF_EXISTS_ENV_VARS.get(collection_name, "")
    raw_mode = str(os.getenv(override_env, "")).strip().lower()
    if not raw_mode:
        raw_mode = str(os.getenv(ENTITY_QDRANT_IF_EXISTS_ENV_VAR, "overwrite")).strip().lower()
    if raw_mode in {"skip", "keep", "preserve"}:
        return "skip"
    return "overwrite"


def _collection_has_existing_points(collection_name: str, book_id: int | None = None) -> bool:
    if models is None:
        raise RuntimeError("qdrant-client models are unavailable.")
    client = get_qdrant_embedding_store()._client
    if not client.collection_exists(collection_name=collection_name):
        return False
    points, _ = client.scroll(
        collection_name=collection_name,
        scroll_filter=_build_book_filter(book_id),
        limit=1,
        with_payload=False,
        with_vectors=False,
    )
    return bool(points)


def _load_entity_rows(table_name: str, book_id: int | None = None) -> list[dict[str, Any]]:
    with _connect_mysql() as conn:
        with conn.cursor() as cursor:
            select_need_delete = ", NEED_DELETE" if table_name == "characters" else ""
            if book_id is None:
                cursor.execute(
                    f"""
                    SELECT id, book_id, name, aliases, records{select_need_delete}
                    FROM `{table_name}`
                    ORDER BY id ASC
                    """
                )
            else:
                cursor.execute(
                    f"""
                    SELECT id, book_id, name, aliases, records{select_need_delete}
                    FROM `{table_name}`
                    WHERE book_id = %s
                    ORDER BY id ASC
                    """,
                    (int(book_id),),
                )
            return list(cursor.fetchall() or [])


def _build_embedding_name_text(name: str, aliases: list[str]) -> str:
    normalized_name = str(name or "").strip()
    alias_candidates: list[str] = []
    for alias in aliases:
        alias_text = str(alias or "").strip()
        if not alias_text or alias_text == normalized_name or alias_text in alias_candidates:
            continue
        alias_candidates.append(alias_text)
    parts = [normalized_name] if normalized_name else []
    parts.extend(alias_candidates[:3])
    return "、".join(part for part in parts if part)


def _build_embedding_hash(text: str) -> str:
    normalized = _clean_text(text)
    digest = hashlib.sha256()
    digest.update(ENTITY_EMBED_MODEL.encode("utf-8"))
    digest.update(b"\n")
    digest.update(normalized.encode("utf-8"))
    return digest.hexdigest()


def _expand_qdrant_rows(table_name: str, book_id: int | None = None) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for row in _load_entity_rows(table_name, book_id):
        if table_name == "characters" and str(row.get("NEED_DELETE") or "").strip().lower() == "no":
            continue
        source_id = int(row.get("id") or 0)
        book_id = int(row.get("book_id") or 0)
        name = str(row.get("name") or "").strip()
        aliases = [str(item or "").strip() for item in _parse_json_list(row.get("aliases")) if str(item or "").strip()]
        name_text = _build_embedding_name_text(name, aliases)
        records = _parse_json_list(row.get("records"))
        valid_records: list[tuple[int, str]] = []
        for record_index, item in enumerate(records):
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            try:
                chapter_index = int(item[0])
            except (TypeError, ValueError):
                continue
            record_text = str(item[1] or "").strip()
            if not record_text or book_id <= 0 or source_id <= 0:
                continue
            valid_records.append((chapter_index, record_text))
            if table_name not in SPLIT_RECORD_COLLECTIONS:
                continue
            point_id = _stable_point_id(
                table_name,
                [book_id, source_id, chapter_index, record_index],
            )
            expanded.append(
                {
                    "id": point_id,
                    "book_id": book_id,
                    "source_id": source_id,
                    "name": name,
                    "record": record_text,
                    "chapter_index": chapter_index,
                    "embedding_text": f"{name_text}：{record_text}" if name_text else record_text,
                }
            )
        if table_name in SPLIT_RECORD_COLLECTIONS or not valid_records:
            continue
        first_chapter_index = int(valid_records[0][0])
        merged_record_text = "\n".join(record_text for _, record_text in valid_records)
        point_id = _stable_point_id(
            table_name,
            [book_id, source_id],
        )
        expanded.append(
            {
                "id": point_id,
                "book_id": book_id,
                "source_id": source_id,
                "name": name,
                "record": merged_record_text,
                "chapter_index": first_chapter_index,
                "embedding_text": f"{name_text}：{merged_record_text}" if name_text else merged_record_text,
            }
        )
    return expanded


def _resolve_entity_embedding_dim() -> int:
    if models is None:
        raise RuntimeError("qdrant-client models are unavailable.")
    store = get_qdrant_embedding_store()
    client = store._client
    for collection_name in ENTITY_COLLECTIONS:
        if not client.collection_exists(collection_name=collection_name):
            continue
        info = client.get_collection(collection_name=collection_name)
        params = getattr(getattr(info, "config", None), "params", None)
        vectors = getattr(params, "vectors", None)
        size = getattr(vectors, "size", None)
        if isinstance(size, int) and size > 0:
            return size
    dimensions_raw = os.getenv("EMBED_DIMENSIONS", "").strip()
    if dimensions_raw:
        try:
            parsed = int(dimensions_raw)
            if parsed > 0:
                return parsed
        except ValueError:
            pass
    return _get_entity_embedding_client().embedding_dim()


def _ensure_collection(collection_name: str) -> None:
    if models is None:
        raise RuntimeError("qdrant-client models are unavailable.")
    store = get_qdrant_embedding_store()
    client = store._client
    if client.collection_exists(collection_name=collection_name):
        info = client.get_collection(collection_name=collection_name)
        params = getattr(getattr(info, "config", None), "params", None)
        vectors = getattr(params, "vectors", None)
        actual_size = getattr(vectors, "size", None)
        dimensions_raw = os.getenv("EMBED_DIMENSIONS", "").strip()
        if dimensions_raw:
            try:
                parsed = int(dimensions_raw)
                expected_size = parsed if parsed > 0 else _get_entity_embedding_client().embedding_dim()
            except ValueError:
                expected_size = _get_entity_embedding_client().embedding_dim()
        else:
            expected_size = _get_entity_embedding_client().embedding_dim()
        if isinstance(actual_size, int) and actual_size != expected_size:
            raise RuntimeError(
                f"Qdrant entity collection dimension mismatch for {collection_name}: "
                f"actual={actual_size}, expected={expected_size}. "
                "Refusing to recreate existing collection automatically because that would affect other books."
            )
        return
    embedding_dim = _resolve_entity_embedding_dim()
    client.create_collection(
        collection_name=collection_name,
        vectors_config=models.VectorParams(
            size=embedding_dim,
            distance=models.Distance.COSINE,
        ),
    )


def _load_existing_hashes(collection_name: str, book_id: int | None = None) -> dict[int, str]:
    if models is None:
        raise RuntimeError("qdrant-client models are unavailable.")
    client = get_qdrant_embedding_store()._client
    if not client.collection_exists(collection_name=collection_name):
        return {}
    existing: dict[int, str] = {}
    next_offset = None
    page_size = max(1, int(os.getenv("QDRANT_SCROLL_PAGE_SIZE", "256").strip() or 256))
    while True:
        points, next_offset = client.scroll(
            collection_name=collection_name,
            limit=page_size,
            scroll_filter=_build_book_filter(book_id),
            with_payload=True,
            with_vectors=False,
            offset=next_offset,
        )
        for point in points:
            point_id = getattr(point, "id", None)
            if not isinstance(point_id, int):
                continue
            payload = dict(getattr(point, "payload", {}) or {})
            hash_value = str(payload.get("embedding_hash") or "").strip()
            if hash_value:
                existing[point_id] = hash_value
        if next_offset is None:
            break
    return existing


def _delete_stale_points(collection_name: str, desired_ids: set[int], book_id: int | None = None) -> int:
    if models is None:
        raise RuntimeError("qdrant-client models are unavailable.")
    client = get_qdrant_embedding_store()._client
    if not client.collection_exists(collection_name=collection_name):
        return 0
    stale_ids: list[int] = []
    next_offset = None
    page_size = max(1, int(os.getenv("QDRANT_SCROLL_PAGE_SIZE", "256").strip() or 256))
    while True:
        points, next_offset = client.scroll(
            collection_name=collection_name,
            limit=page_size,
            scroll_filter=_build_book_filter(book_id),
            with_payload=False,
            with_vectors=False,
            offset=next_offset,
        )
        for point in points:
            point_id = getattr(point, "id", None)
            if isinstance(point_id, int) and point_id not in desired_ids:
                stale_ids.append(point_id)
        if next_offset is None:
            break
    if not stale_ids:
        return 0
    batch_size = max(1, int(os.getenv("QDRANT_UPSERT_BATCH_SIZE", "128").strip() or 128))
    for index in range(0, len(stale_ids), batch_size):
        client.delete(
            collection_name=collection_name,
            points_selector=models.PointIdsList(points=stale_ids[index : index + batch_size]),
            wait=True,
        )
    return len(stale_ids)


@lru_cache(maxsize=1)
def _get_entity_embedding_client() -> OpenAIEmbeddingClient:
    client = OpenAIEmbeddingClient()
    client.model = ENTITY_EMBED_MODEL
    return client


def _embed_entity_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    embedding_client = _get_entity_embedding_client()
    batch_size = max(1, int(getattr(embedding_client, "batch_size", 32) or 32))
    vectors: list[list[float]] = []
    for chunk in _batch(texts, batch_size):
        normalized = [_clean_text(text) for text in chunk]
        try:
            vectors.extend(embedding_client._embed_via_text_api(normalized))
        except Exception:
            vectors.extend(asyncio.run(_embed_entity_texts_via_multimodal_async(normalized, embedding_client)))
    return vectors


async def _embed_entity_texts_via_multimodal_async(
    texts: list[str],
    embedding_client: Any,
) -> list[list[float]]:
    semaphore = asyncio.Semaphore(ENTITY_EMBED_MULTIMODAL_MAX_CONCURRENCY)
    instructions = os.getenv("EMBED_INSTRUCTIONS", "").strip()
    dimensions_raw = os.getenv("EMBED_DIMENSIONS", "").strip()
    dimensions: int | None = None
    if dimensions_raw:
        try:
            parsed = int(dimensions_raw)
            if parsed > 0:
                dimensions = parsed
        except ValueError:
            dimensions = None

    async def _embed_single(index: int, text: str, client: httpx.AsyncClient) -> tuple[int, list[float]]:
        payload: dict[str, Any] = {
            "model": embedding_client.model,
            "input": [{"type": "text", "text": _clean_text(text)}],
            "encoding_format": "float",
        }
        if instructions:
            payload["instructions"] = instructions
        if dimensions is not None:
            payload["dimensions"] = dimensions
        for attempt in range(ENTITY_EMBED_MAX_RETRIES + 1):
            try:
                async with semaphore:
                    response = await client.post(
                        f"{embedding_client._base_url_normalized}/embeddings/multimodal",
                        headers={
                            "Authorization": f"Bearer {embedding_client.api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < ENTITY_EMBED_MAX_RETRIES:
                    delay = min(
                        ENTITY_EMBED_MAX_DELAY_SECONDS,
                        ENTITY_EMBED_BASE_DELAY_SECONDS * (2 ** attempt),
                    ) + random.uniform(0.0, 1.0)
                    await asyncio.sleep(delay)
                    continue
                raise RuntimeError(f"Multimodal embedding transport error: {exc!r}") from exc
            body: dict[str, Any] | None = None
            try:
                parsed_body = response.json()
                body = parsed_body if isinstance(parsed_body, dict) else None
            except Exception:
                body = None
            if response.status_code == 429 and attempt < ENTITY_EMBED_MAX_RETRIES:
                delay = min(
                    ENTITY_EMBED_MAX_DELAY_SECONDS,
                    ENTITY_EMBED_BASE_DELAY_SECONDS * (2 ** attempt),
                ) + random.uniform(0.0, 1.0)
                await asyncio.sleep(delay)
                continue
            if response.status_code >= 400:
                detail = json.dumps(body, ensure_ascii=False) if body is not None else response.text
                raise RuntimeError(f"Multimodal embedding HTTP {response.status_code}: {detail}")
            if not isinstance(body, dict):
                raise RuntimeError("Multimodal embedding returned non-JSON response.")

            data = body.get("data")
            vector: list[float] | None = None
            if isinstance(data, dict):
                vector = embedding_client._extract_dense_vector(data)
            elif isinstance(data, list) and data:
                vector = embedding_client._extract_dense_vector(data[0])
            if vector is None:
                raise RuntimeError("Multimodal embedding response missing dense vector.")
            return index, vector
        raise RuntimeError("Multimodal embedding retry exhausted.")

    timeout = max(60.0, float(getattr(embedding_client, "timeout", 20.0) or 20.0))
    async with httpx.AsyncClient(timeout=timeout) as client:
        tasks = [
            asyncio.create_task(_embed_single(index, text, client))
            for index, text in enumerate(texts)
        ]
        results = await asyncio.gather(*tasks)
    vectors: list[list[float]] = [[] for _ in texts]
    for index, vector in results:
        vectors[index] = vector
    return vectors


def _sync_single_collection(
    table_name: str,
    *,
    row_limit: int | None = None,
    book_id: int | None = None,
) -> dict[str, int]:
    if models is None:
        raise RuntimeError("qdrant-client models are unavailable.")
    rows = _expand_qdrant_rows(table_name, book_id)
    is_partial_sync = row_limit is not None
    if row_limit is not None:
        rows = rows[: max(0, int(row_limit))]
    if _resolve_entity_qdrant_if_exists_mode(table_name) == "skip" and _collection_has_existing_points(table_name, book_id):
        return {"rows": len(rows), "embedded": 0, "skipped": len(rows), "deleted": 0}
    _ensure_collection(table_name)
    if not rows:
        deleted = 0 if is_partial_sync else _delete_stale_points(table_name, set(), book_id)
        return {"rows": 0, "embedded": 0, "skipped": 0, "deleted": deleted}
    row_batch_size = max(1, int(os.getenv("ENTITY_QDRANT_ROW_BATCH_SIZE", "256").strip() or 256))
    batch_size = max(1, int(os.getenv("QDRANT_UPSERT_BATCH_SIZE", "128").strip() or 128))
    client = get_qdrant_embedding_store()._client
    existing_hashes = _load_existing_hashes(table_name, book_id)
    desired_ids = {int(row["id"]) for row in rows}
    embedded_count = 0
    skipped_count = 0
    pending_rows: list[dict[str, Any]] = []
    for row in rows:
        embedding_text = _clean_text(row["embedding_text"])
        embedding_hash = _build_embedding_hash(embedding_text)
        row["embedding_text"] = embedding_text
        row["embedding_hash"] = embedding_hash
        if existing_hashes.get(int(row["id"])) == embedding_hash:
            skipped_count += 1
            continue
        pending_rows.append(row)
    for row_batch in _batch_rows(pending_rows, row_batch_size):
        texts = [row["embedding_text"] for row in row_batch]
        vectors = _embed_entity_texts(texts)
        if len(vectors) != len(row_batch):
            raise RuntimeError("Vector size mismatch while syncing entity qdrant rows.")
        points = [
            models.PointStruct(
                id=row["id"],
                vector=vector,
                payload={
                    "id": row["id"],
                    "book_id": row["book_id"],
                    "source_id": row["source_id"],
                    "name": row["name"],
                    "record": row["record"],
                    "chapter_index": row["chapter_index"],
                    "embedding_hash": row["embedding_hash"],
                },
            )
            for row, vector in zip(row_batch, vectors)
        ]
        embedded_count += len(points)
        for index in range(0, len(points), batch_size):
            client.upsert(
                collection_name=table_name,
                points=points[index : index + batch_size],
                wait=True,
            )
    deleted_count = 0 if is_partial_sync else _delete_stale_points(table_name, desired_ids, book_id)
    return {
        "rows": len(rows),
        "embedded": embedded_count,
        "skipped": skipped_count,
        "deleted": deleted_count,
    }


def sync_entity_collections(*, row_limit: int | None = None, book_id: int | None = None) -> dict[str, dict[str, int]]:
    return {
        table_name: _sync_single_collection(table_name, row_limit=row_limit, book_id=book_id)
        for table_name in ENTITY_COLLECTIONS
    }


def main() -> None:
    print(json.dumps(sync_entity_collections(), ensure_ascii=False))


if __name__ == "__main__":
    main()
