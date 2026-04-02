from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import unquote, urlparse
from uuid import NAMESPACE_URL, uuid5

import pymysql
import httpx
from dotenv import load_dotenv
from openai import OpenAI

try:
    from qdrant_client import QdrantClient
    from qdrant_client import models
except ModuleNotFoundError:  # pragma: no cover - optional runtime dependency
    QdrantClient = None  # type: ignore[assignment]
    models = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_OVERRIDE_VAR = "STORY2MEMORY_ENV_OVERRIDE"

CHAPTER_COLLECTION = "chapterSummaryEmbedding"
PLOT_COLLECTION = "plotSummaryEmbedding"
VOLUME_COLLECTION = "volumeSummaryEmbedding"


@dataclass(frozen=True)
class EmbeddingCollectionSpec:
    name: str
    source_sql: str
    text_field: str
    payload_fields: tuple[str, ...]
    id_fields: tuple[str, ...]


CHAPTER_SPEC = EmbeddingCollectionSpec(
    name=CHAPTER_COLLECTION,
    source_sql="""
        SELECT
            book_id,
            chapter_index,
            plot_id,
            chapter_summary
        FROM book_chapters
        WHERE book_id = %s
          AND COALESCE(chapter_summary, '') <> ''
        ORDER BY chapter_index ASC
    """,
    text_field="chapter_summary",
    payload_fields=("book_id", "chapter_index", "plot_id", "chapter_summary"),
    id_fields=("book_id", "chapter_index"),
)

PLOT_SPEC = EmbeddingCollectionSpec(
    name=PLOT_COLLECTION,
    source_sql="""
        SELECT
            book_id,
            plot_id,
            volume_id,
            start_chapter_index,
            end_chapter_index,
            plot_summary,
            CONCAT_WS(
                '\n',
                CONCAT('summary: ', COALESCE(plot_summary, '')),
                CONCAT('special_existence: ', COALESCE(CAST(special_existence AS CHAR), '')),
                CONCAT('origanizations: ', COALESCE(CAST(origanizations AS CHAR), '')),
                CONCAT('world_rules: ', COALESCE(CAST(world_rules AS CHAR), ''))
            ) AS search_content
        FROM book_plots
        WHERE book_id = %s
          AND COALESCE(plot_summary, '') <> ''
        ORDER BY plot_id ASC
    """,
    text_field="search_content",
    payload_fields=(
        "book_id",
        "plot_id",
        "volume_id",
        "start_chapter_index",
        "end_chapter_index",
        "plot_summary",
        "search_content",
    ),
    id_fields=("book_id", "plot_id"),
)

VOLUME_SPEC = EmbeddingCollectionSpec(
    name=VOLUME_COLLECTION,
    source_sql="""
        SELECT
            book_id,
            volume_index,
            start_plot_index,
            end_plot_index,
            volume_summary
        FROM book_volumes
        WHERE book_id = %s
          AND COALESCE(volume_summary, '') <> ''
        ORDER BY start_plot_index ASC
    """,
    text_field="volume_summary",
    payload_fields=(
        "book_id",
        "volume_index",
        "start_plot_index",
        "end_plot_index",
        "volume_summary",
    ),
    id_fields=("book_id", "start_plot_index", "end_plot_index"),
)

SPEC_BY_COLLECTION: dict[str, EmbeddingCollectionSpec] = {
    CHAPTER_COLLECTION: CHAPTER_SPEC,
    PLOT_COLLECTION: PLOT_SPEC,
    VOLUME_COLLECTION: VOLUME_SPEC,
}


def _load_runtime_env() -> None:
    load_dotenv(dotenv_path=ROOT_DIR / ".env")
    override_path = os.getenv(ENV_OVERRIDE_VAR)
    if override_path:
        loaded = load_dotenv(dotenv_path=override_path, override=True)
        if not loaded:
            logger.warning("Env override file not found: %s=%s", ENV_OVERRIDE_VAR, override_path)


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
        raise RuntimeError("Missing or invalid MYSQL_DSN environment variable.")
    return pymysql.connect(**cfg)


def _stable_point_id(collection_name: str, values: Sequence[Any]) -> int:
    token = f"{collection_name}::" + "::".join(str(item) for item in values)
    return uuid5(NAMESPACE_URL, token).int % ((1 << 63) - 1)


def _clean_text(text: Any) -> str:
    rendered = str(text or "").strip()
    return rendered if rendered else "[EMPTY]"


def _build_embedding_hash(model: str, text: Any) -> str:
    normalized = _clean_text(text)
    digest = hashlib.sha256()
    digest.update(str(model or "").encode("utf-8"))
    digest.update(b"\n")
    digest.update(normalized.encode("utf-8"))
    return digest.hexdigest()


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _batch(seq: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    step = max(1, int(size))
    for idx in range(0, len(seq), step):
        yield seq[idx : idx + step]


class OpenAIEmbeddingClient:
    def __init__(self) -> None:
        _load_runtime_env()
        api_key = (
            os.getenv("EMBED_API_KEY", "").strip()
            or os.getenv("ARK_API_KEY", "").strip()
        )
        if not api_key:
            raise RuntimeError("Missing EMBED_API_KEY or ARK_API_KEY for embedding API.")

        self.model = os.getenv("EMBED_MODEL", "ep-20251224183557-n8vdn").strip() or "ep-20251224183557-n8vdn"
        self.base_url = os.getenv("EMBED_BASE_URL", "").strip() or "https://ark.cn-beijing.volces.com/api/v3"
        timeout = float(os.getenv("EMBED_TIMEOUT_SECONDS", "20").strip() or 20.0)
        self.batch_size = max(1, int(os.getenv("EMBED_BATCH_SIZE", "32").strip() or 32))
        self.api_key = api_key
        self.timeout = timeout
        self.endpoint_mode = (os.getenv("EMBED_ENDPOINT_MODE", "auto").strip().lower() or "auto")
        self._base_url_normalized = self.base_url.rstrip("/")
        self._multimodal_runtime_forced = False
        self._client = OpenAI(api_key=api_key, base_url=self.base_url, timeout=timeout)
        self._dim_lock = threading.Lock()
        self._dimension: int | None = None

    def _prefer_multimodal(self) -> bool:
        if self.endpoint_mode in {"multimodal", "mm"}:
            return True
        if self.endpoint_mode in {"text", "standard"}:
            return False
        return "vision" in self.model.lower()

    def _should_retry_with_multimodal(self, exc: Exception) -> bool:
        if self.endpoint_mode in {"text", "standard"}:
            return False
        message = str(exc).lower()
        return (
            "does not support this api" in message
            or ("invalidparameter" in message and "model" in message)
        )

    @staticmethod
    def _extract_dense_vector(item: Any) -> list[float] | None:
        if hasattr(item, "model_dump"):
            item = item.model_dump()
        if not isinstance(item, dict):
            return None

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

        for candidate in candidates:
            if isinstance(candidate, list) and candidate and isinstance(candidate[0], (int, float)):
                return [float(v) for v in candidate]
            if (
                isinstance(candidate, list)
                and candidate
                and isinstance(candidate[0], list)
                and candidate[0]
                and isinstance(candidate[0][0], (int, float))
            ):
                return [float(v) for v in candidate[0]]
            if isinstance(candidate, dict):
                dense = candidate.get("dense") or candidate.get("vector") or candidate.get("values")
                if isinstance(dense, list) and dense and isinstance(dense[0], (int, float)):
                    return [float(v) for v in dense]
        return None

    def _embed_via_text_api(self, texts: Sequence[str]) -> list[list[float]]:
        response = self._client.embeddings.create(
            model=self.model,
            input=[_clean_text(text) for text in texts],
        )
        partial: list[list[float] | None] = [None] * len(texts)
        for item in response.data:
            partial[int(item.index)] = list(item.embedding)
        vectors = [vector for vector in partial if vector is not None]
        if len(vectors) != len(texts):
            raise RuntimeError("Embedding API returned incomplete vectors.")
        return vectors

    def _embed_via_multimodal_api(self, texts: Sequence[str]) -> list[list[float]]:
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

        vectors: list[list[float]] = []
        for text in texts:
            payload: dict[str, Any] = {
                "model": self.model,
                "input": [{"type": "text", "text": _clean_text(text)}],
                "encoding_format": "float",
            }
            if instructions:
                payload["instructions"] = instructions
            if dimensions is not None:
                payload["dimensions"] = dimensions

            response = httpx.post(
                f"{self._base_url_normalized}/embeddings/multimodal",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
            body: dict[str, Any] | None = None
            try:
                parsed = response.json()
                body = parsed if isinstance(parsed, dict) else None
            except Exception:
                body = None
            if response.status_code >= 400:
                detail = json.dumps(body, ensure_ascii=False) if body is not None else response.text
                raise RuntimeError(f"Multimodal embedding HTTP {response.status_code}: {detail}")
            if not isinstance(body, dict):
                raise RuntimeError("Multimodal embedding returned non-JSON response.")

            data = body.get("data")
            vector: list[float] | None = None
            if isinstance(data, dict):
                vector = self._extract_dense_vector(data)
            elif isinstance(data, list) and data:
                vector = self._extract_dense_vector(data[0])
            if vector is None:
                raise RuntimeError("Multimodal embedding response missing dense vector.")
            vectors.append(vector)
        return vectors

    def embedding_dim(self) -> int:
        if self._dimension is not None:
            return self._dimension
        with self._dim_lock:
            if self._dimension is not None:
                return self._dimension
            vector = self.embed_texts(["dimension_probe"])[0]
            self._dimension = len(vector)
            return self._dimension

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        normalized = [_clean_text(text) for text in texts]
        use_multimodal = self._prefer_multimodal() or self._multimodal_runtime_forced
        vectors: list[list[float]] = []
        for chunk in _batch(normalized, self.batch_size):
            if use_multimodal:
                vectors.extend(self._embed_via_multimodal_api(chunk))
                continue
            try:
                vectors.extend(self._embed_via_text_api(chunk))
            except Exception as exc:
                if not self._should_retry_with_multimodal(exc):
                    raise
                logger.warning(
                    "[Qdrant] embeddings.create rejected model=%s, retry with /embeddings/multimodal",
                    self.model,
                )
                self._multimodal_runtime_forced = True
                use_multimodal = True
                vectors.extend(self._embed_via_multimodal_api(chunk))
        return vectors


@lru_cache(maxsize=1)
def _get_embedding_client() -> OpenAIEmbeddingClient:
    return OpenAIEmbeddingClient()


class QdrantEmbeddingStore:
    """Manage embedding collections in Qdrant and provide dense retrieval APIs."""

    def __init__(self) -> None:
        self._client = self._build_qdrant_client()
        self._lock = threading.Lock()
        self._synced_keys: set[tuple[str, int]] = set()

    def _build_qdrant_client(self):
        _load_runtime_env()
        if QdrantClient is None:
            raise RuntimeError("qdrant-client is not installed. Please install qdrant-client.")

        url = os.getenv("QDRANT_URL", "").strip()
        api_key = os.getenv("QDRANT_API_KEY", "").strip() or None
        timeout = float(os.getenv("QDRANT_TIMEOUT_SECONDS", "20").strip() or 20.0)
        if url:
            return QdrantClient(url=url, api_key=api_key, timeout=timeout)

        auto_url = os.getenv("QDRANT_AUTO_URL", "http://127.0.0.1:56333").strip()
        if auto_url:
            try:
                remote_client = QdrantClient(url=auto_url, api_key=api_key, timeout=timeout)
                remote_client.get_collections()
                logger.info("[Qdrant] auto-detected remote server: %s", auto_url)
                return remote_client
            except Exception as exc:
                logger.info("[Qdrant] remote server unavailable at %s, fallback to local mode: %s", auto_url, exc)

        local_path = os.getenv("QDRANT_LOCAL_PATH", "").strip()
        if not local_path:
            # Keep local qdrant files outside project tree to avoid dev hot-reload loops.
            local_path = str(Path.home() / ".story2memory" / "qdrant")
        path_obj = Path(local_path).expanduser().resolve()
        try:
            root_dir = ROOT_DIR.resolve()
            if root_dir in path_obj.parents or path_obj == root_dir:
                logger.warning(
                    "[Qdrant] local path is inside project tree: %s. This may trigger dev hot-reload. "
                    "Set QDRANT_LOCAL_PATH outside repo or use QDRANT_URL.",
                    path_obj,
                )
        except Exception:
            pass
        path_obj.mkdir(parents=True, exist_ok=True)
        return QdrantClient(path=str(path_obj))

    def ensure_collections(self) -> None:
        for collection_name in SPEC_BY_COLLECTION:
            self._ensure_single_collection(collection_name)

    def _ensure_single_collection(self, collection_name: str) -> None:
        if models is None:
            raise RuntimeError("qdrant-client models are unavailable.")
        embedding_dim = _get_embedding_client().embedding_dim()
        exists = self._client.collection_exists(collection_name=collection_name)
        if not exists:
            self._client.create_collection(
                collection_name=collection_name,
                vectors_config=models.VectorParams(
                    size=embedding_dim,
                    distance=models.Distance.COSINE,
                ),
            )
            logger.info(
                "[Qdrant] created collection=%s dim=%d",
                collection_name,
                embedding_dim,
            )
            return

        info = self._client.get_collection(collection_name=collection_name)
        actual_size: int | None = None
        vectors_cfg = getattr(getattr(info, "config", None), "params", None)
        vectors_data = getattr(vectors_cfg, "vectors", None) if vectors_cfg else None
        if hasattr(vectors_data, "size"):
            actual_size = _coerce_int(getattr(vectors_data, "size", None))
        if actual_size and actual_size != embedding_dim:
            raise RuntimeError(
                f"Qdrant collection dimension mismatch for {collection_name}: "
                f"actual={actual_size}, expected={embedding_dim}. "
                "Refusing to recreate existing collection automatically because that would affect other books."
            )

    def collection_exists(self, collection_name: str) -> bool:
        return bool(self._client.collection_exists(collection_name=collection_name))

    def sync_book(
        self,
        collection_name: str,
        book_id: int,
        *,
        force: bool = False,
        reset: bool = False,
    ) -> dict[str, int]:
        spec = SPEC_BY_COLLECTION.get(collection_name)
        if spec is None:
            raise ValueError(f"Unknown collection name: {collection_name}")
        normalized_book_id = _coerce_int(book_id)
        if normalized_book_id is None or normalized_book_id <= 0:
            raise ValueError("book_id must be positive integer.")

        sync_key = (collection_name, normalized_book_id)
        if not force and sync_key in self._synced_keys:
            return {"book_id": normalized_book_id, "collection": collection_name, "rows": 0, "skipped": 1}

        with self._lock:
            if not force and sync_key in self._synced_keys:
                return {"book_id": normalized_book_id, "collection": collection_name, "rows": 0, "skipped": 1}
            self._ensure_single_collection(collection_name)
            rows = self._load_source_rows(spec, normalized_book_id)
            rewrite_stats = self._rewrite_points(spec, normalized_book_id, rows, reset=reset)
            self._synced_keys.add(sync_key)
            return {
                "book_id": normalized_book_id,
                "collection": collection_name,
                "rows": len(rows),
                "skipped": int(rewrite_stats.get("skipped", 0)),
                "embedded": int(rewrite_stats.get("embedded", 0)),
                "deleted": int(rewrite_stats.get("deleted", 0)),
            }

    def _load_source_rows(self, spec: EmbeddingCollectionSpec, book_id: int) -> list[dict[str, Any]]:
        with _connect_mysql() as conn:
            with conn.cursor() as cursor:
                cursor.execute(spec.source_sql, (book_id,))
                return list(cursor.fetchall() or [])

    def _rewrite_points(
        self,
        spec: EmbeddingCollectionSpec,
        book_id: int,
        rows: Sequence[dict[str, Any]],
        *,
        reset: bool = False,
    ) -> dict[str, int]:
        if models is None:
            raise RuntimeError("qdrant-client models are unavailable.")
        embedding_client = _get_embedding_client()
        existing_hashes = self._load_existing_hashes(spec.name, book_id)
        desired_ids: set[int] = set()
        pending_rows: list[dict[str, Any]] = []
        skipped_count = 0

        for row in rows:
            point_id = _stable_point_id(
                spec.name,
                [row.get(field) for field in spec.id_fields],
            )
            text = _clean_text(row.get(spec.text_field, ""))
            embedding_hash = _build_embedding_hash(embedding_client.model, text)
            desired_ids.add(point_id)
            if existing_hashes.get(point_id) == embedding_hash:
                skipped_count += 1
                continue
            pending_rows.append(
                {
                    "row": row,
                    "point_id": point_id,
                    "text": text,
                    "embedding_hash": embedding_hash,
                }
            )

        embedded_count = 0
        if pending_rows:
            texts = [item["text"] for item in pending_rows]
            vectors = embedding_client.embed_texts(texts)
            if len(vectors) != len(pending_rows):
                raise RuntimeError("Vector size mismatch while syncing Qdrant embeddings.")

            points: list[Any] = []
            for item, vector in zip(pending_rows, vectors):
                row = item["row"]
                payload = {field: row.get(field) for field in spec.payload_fields}
                payload["embedding_hash"] = item["embedding_hash"]
                points.append(
                    models.PointStruct(
                        id=item["point_id"],
                        vector=vector,
                        payload=payload,
                    )
                )

            batch_size = max(1, int(os.getenv("QDRANT_UPSERT_BATCH_SIZE", "128").strip() or 128))
            for idx in range(0, len(points), batch_size):
                batch = points[idx : idx + batch_size]
                self._client.upsert(
                    collection_name=spec.name,
                    points=batch,
                    wait=True,
                )
            embedded_count = len(points)

        deleted_count = 0
        if reset or not rows:
            deleted_count = self._delete_stale_book_points(spec.name, book_id, desired_ids)
        return {
            "embedded": embedded_count,
            "skipped": skipped_count,
            "deleted": deleted_count,
        }

    def _delete_book_points(self, collection_name: str, book_id: int) -> None:
        if models is None:
            raise RuntimeError("qdrant-client models are unavailable.")
        self._client.delete(
            collection_name=collection_name,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="book_id",
                            match=models.MatchValue(value=book_id),
                        )
                    ]
                )
            ),
            wait=True,
        )

    def _load_existing_hashes(self, collection_name: str, book_id: int) -> dict[int, str]:
        if models is None:
            raise RuntimeError("qdrant-client models are unavailable.")
        existing: dict[int, str] = {}
        next_offset = None
        page_size = max(1, int(os.getenv("QDRANT_SCROLL_PAGE_SIZE", "256").strip() or 256))
        while True:
            points, next_offset = self._client.scroll(
                collection_name=collection_name,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="book_id",
                            match=models.MatchValue(value=int(book_id)),
                        )
                    ]
                ),
                limit=page_size,
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

    def _delete_stale_book_points(self, collection_name: str, book_id: int, desired_ids: set[int]) -> int:
        if models is None:
            raise RuntimeError("qdrant-client models are unavailable.")
        stale_ids: list[int] = []
        next_offset = None
        page_size = max(1, int(os.getenv("QDRANT_SCROLL_PAGE_SIZE", "256").strip() or 256))
        while True:
            points, next_offset = self._client.scroll(
                collection_name=collection_name,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="book_id",
                            match=models.MatchValue(value=int(book_id)),
                        )
                    ]
                ),
                limit=page_size,
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
        for idx in range(0, len(stale_ids), batch_size):
            self._client.delete(
                collection_name=collection_name,
                points_selector=models.PointIdsList(points=stale_ids[idx : idx + batch_size]),
                wait=True,
            )
        return len(stale_ids)

    def dense_search(
        self,
        collection_name: str,
        book_id: int,
        query_vector: Sequence[float],
        limit: int,
        source_ids: Sequence[int] | None = None,
    ) -> list[dict[str, Any]]:
        if models is None:
            raise RuntimeError("qdrant-client models are unavailable.")
        must_conditions: list[Any] = [
            models.FieldCondition(
                key="book_id",
                match=models.MatchValue(value=int(book_id)),
            )
        ]
        normalized_source_ids = sorted({int(item) for item in (source_ids or []) if int(item) > 0})
        if normalized_source_ids:
            must_conditions.append(
                models.FieldCondition(
                    key="source_id",
                    match=models.MatchAny(any=normalized_source_ids),
                )
            )
        query_filter = models.Filter(must=must_conditions)
        safe_limit = max(1, int(limit))
        if hasattr(self._client, "search"):
            response_points = self._client.search(
                collection_name=collection_name,
                query_vector=list(query_vector),
                limit=safe_limit,
                query_filter=query_filter,
                with_payload=True,
                with_vectors=False,
            )
        else:
            query_response = self._client.query_points(
                collection_name=collection_name,
                query=list(query_vector),
                limit=safe_limit,
                query_filter=query_filter,
                with_payload=True,
                with_vectors=False,
            )
            response_points = list(getattr(query_response, "points", []) or [])
        items: list[dict[str, Any]] = []
        for point in response_points:
            payload = dict(getattr(point, "payload", {}) or {})
            items.append(
                {
                    "id": getattr(point, "id", None),
                    "score": float(getattr(point, "score", 0.0) or 0.0),
                    "payload": payload,
                }
            )
        return items

    def scroll_payloads(
        self,
        collection_name: str,
        book_id: int,
    ) -> list[dict[str, Any]]:
        if models is None:
            raise RuntimeError("qdrant-client models are unavailable.")
        all_points: list[dict[str, Any]] = []
        next_offset = None
        page_size = max(1, int(os.getenv("QDRANT_SCROLL_PAGE_SIZE", "256").strip() or 256))
        while True:
            points, next_offset = self._client.scroll(
                collection_name=collection_name,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="book_id",
                            match=models.MatchValue(value=int(book_id)),
                        )
                    ]
                ),
                limit=page_size,
                with_payload=True,
                with_vectors=False,
                offset=next_offset,
            )
            for point in points:
                all_points.append(
                    {
                        "id": getattr(point, "id", None),
                        "payload": dict(getattr(point, "payload", {}) or {}),
                    }
                )
            if next_offset is None:
                break
        return all_points


@lru_cache(maxsize=1)
def get_qdrant_embedding_store() -> QdrantEmbeddingStore:
    return QdrantEmbeddingStore()


def ensure_embedding_collections() -> None:
    store = get_qdrant_embedding_store()
    store.ensure_collections()


def ensure_book_embeddings(
    book_id: int,
    *,
    force: bool = False,
    reset: bool = False,
) -> dict[str, dict[str, int]]:
    store = get_qdrant_embedding_store()
    store.ensure_collections()
    return {
        CHAPTER_COLLECTION: store.sync_book(CHAPTER_COLLECTION, book_id, force=force, reset=reset),
        PLOT_COLLECTION: store.sync_book(PLOT_COLLECTION, book_id, force=force, reset=reset),
        VOLUME_COLLECTION: store.sync_book(VOLUME_COLLECTION, book_id, force=force, reset=reset),
    }


def sync_book_embedding_collections(
    book_id: int,
    collection_names: Sequence[str],
    *,
    force: bool = False,
    reset: bool = False,
) -> dict[str, dict[str, int]]:
    store = get_qdrant_embedding_store()
    store.ensure_collections()
    stats: dict[str, dict[str, int]] = {}
    for collection_name in collection_names:
        if collection_name not in SPEC_BY_COLLECTION:
            raise ValueError(f"Unknown collection name: {collection_name}")
        stats[collection_name] = store.sync_book(collection_name, book_id, force=force, reset=reset)
    return stats


def delete_book_embedding_collections(
    book_id: int,
    collection_names: Sequence[str] | None = None,
) -> dict[str, dict[str, int]]:
    store = get_qdrant_embedding_store()
    targets = tuple(collection_names or SPEC_BY_COLLECTION.keys())
    stats: dict[str, dict[str, int]] = {}
    for collection_name in targets:
        if collection_name not in SPEC_BY_COLLECTION:
            raise ValueError(f"Unknown collection name: {collection_name}")
        if not store.collection_exists(collection_name):
            stats[collection_name] = {"deleted": 0, "skipped": 1}
            continue
        store._delete_book_points(collection_name, int(book_id))
        stats[collection_name] = {"deleted": 1, "skipped": 0}
    return stats
