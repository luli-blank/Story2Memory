from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urlparse

import httpx
import pymysql
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from openai import OpenAI
from volcenginesdkarkruntime import Ark

from agent.prompt import HYBRID_RESULT_FILTER_PROMPT
from agent.graph import apply_llm_network_settings
from database.qdrant_client import (
    CHAPTER_COLLECTION,
    PLOT_COLLECTION,
    VOLUME_COLLECTION,
    get_qdrant_embedding_store,
)

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_OVERRIDE_VAR = "STORY2MEMORY_ENV_OVERRIDE"
FASTEMBED_CACHE_PATH_ENV_VAR = "FASTEMBED_CACHE_PATH"
FASTEMBED_CACHE_DIR_ENV_VAR = "FASTEMBED_CACHE_DIR"
ENTITY_EMBED_MODEL_ENV_VAR = "ENTITY_EMBED_MODEL"
DEFAULT_ENTITY_EMBED_MODEL = "ep-20260303004802-tlt8f"
CHARACTER_COLLECTION = "characters"
ORIGANIZATION_COLLECTION = "origanizations"
SPECIAL_EXISTENCE_COLLECTION = "special_existences"
WORLD_RULE_COLLECTION = "world_rules"
HYBRID_SPARSE_RETRIEVAL_ENABLED_ENV_VAR = "HYBRID_SPARSE_RETRIEVAL_ENABLED"

RRF_K = 60.0
HYBRID_FILTER_COMPLEX_MARKERS = (
    "为什么",
    "原因",
    "动机",
    "时间线",
    "演变",
    "完整",
    "全过程",
    "结局",
    "影响",
    "意义",
)
HYBRID_FILTER_STOPWORDS = {
    "的",
    "了",
    "吗",
    "呢",
    "和",
    "与",
    "中",
    "里",
    "是",
    "谁",
    "什么",
    "多少",
    "哪",
    "哪些",
    "请问",
    "小说",
}


@dataclass(frozen=True)
class HybridSpec:
    collection_name: str
    text_field: str
    default_return_k: int
    default_dense_k: int
    default_sparse_k: int


CHAPTER_HYBRID_SPEC = HybridSpec(
    collection_name=CHAPTER_COLLECTION,
    text_field="chapter_summary",
    default_return_k=25,
    default_dense_k=75,
    default_sparse_k=100,
)

PLOT_HYBRID_SPEC = HybridSpec(
    collection_name=PLOT_COLLECTION,
    text_field="search_content",
    default_return_k=10,
    default_dense_k=25,
    default_sparse_k=60,
)

VOLUME_HYBRID_SPEC = HybridSpec(
    collection_name=VOLUME_COLLECTION,
    text_field="volume_summary",
    default_return_k=3,
    default_dense_k=5,
    default_sparse_k=20,
)

CHARACTER_HYBRID_SPEC = HybridSpec(
    collection_name=CHARACTER_COLLECTION,
    text_field="record",
    default_return_k=8,
    default_dense_k=24,
    default_sparse_k=48,
)

ORIGANIZATION_HYBRID_SPEC = HybridSpec(
    collection_name=ORIGANIZATION_COLLECTION,
    text_field="record",
    default_return_k=8,
    default_dense_k=24,
    default_sparse_k=48,
)

SPECIAL_EXISTENCE_HYBRID_SPEC = HybridSpec(
    collection_name=SPECIAL_EXISTENCE_COLLECTION,
    text_field="record",
    default_return_k=6,
    default_dense_k=20,
    default_sparse_k=40,
)

WORLD_RULE_HYBRID_SPEC = HybridSpec(
    collection_name=WORLD_RULE_COLLECTION,
    text_field="record",
    default_return_k=6,
    default_dense_k=20,
    default_sparse_k=40,
)


def _load_runtime_env() -> None:
    load_dotenv(dotenv_path=ROOT_DIR / ".env")
    override_path = os.getenv(ENV_OVERRIDE_VAR)
    if override_path:
        loaded = load_dotenv(dotenv_path=override_path, override=True)
        if not loaded:
            logger.warning("Env override file not found: %s=%s", ENV_OVERRIDE_VAR, override_path)


def _resolve_fastembed_cache_dir() -> Path:
    raw_cache_dir = (
        os.getenv(FASTEMBED_CACHE_PATH_ENV_VAR, "").strip()
        or os.getenv(FASTEMBED_CACHE_DIR_ENV_VAR, "").strip()
    )
    cache_dir = Path(raw_cache_dir).expanduser() if raw_cache_dir else (Path.home() / ".cache" / "fastembed")
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


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


def _coerce_positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _resolve_book_id(novel_title: str) -> int | None:
    title = (novel_title or "").strip()
    if not title:
        return None
    try:
        with _connect_mysql() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id
                    FROM books
                    WHERE title = %s
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (title,),
                )
                row = cursor.fetchone()
        if not row:
            return None
        return int(row.get("id"))
    except Exception as exc:
        logger.error("[HybridSearch] resolve_book_id failed: %s", exc)
        return None


def _normalize_text(text: Any) -> str:
    rendered = str(text or "").strip()
    return rendered if rendered else "[EMPTY]"


def _stringify_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    parts.append(text)
                continue
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
                    continue
            rendered = str(item).strip()
            if rendered:
                parts.append(rendered)
        return "\n".join(parts)
    return str(content).strip()


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    payload = str(raw or "").strip()
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    left = payload.find("{")
    right = payload.rfind("}")
    if left >= 0 and right > left:
        candidate = payload[left : right + 1]
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


class EmbeddingQueryClient:
    def __init__(self, *, model_override: str | None = None) -> None:
        _load_runtime_env()
        api_key = (
            os.getenv("EMBED_API_KEY", "").strip()
            or os.getenv("ARK_API_KEY", "").strip()
        )
        if not api_key:
            raise RuntimeError("Missing EMBED_API_KEY or ARK_API_KEY for hybrid embedding query.")

        base_url = os.getenv("EMBED_BASE_URL", "").strip() or "https://ark.cn-beijing.volces.com/api/v3"
        timeout = float(os.getenv("EMBED_TIMEOUT_SECONDS", "20").strip() or 20.0)
        model = (
            str(model_override or "").strip()
            or os.getenv("EMBED_MODEL", "ep-20251224183557-n8vdn").strip()
            or "ep-20251224183557-n8vdn"
        )

        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.endpoint_mode = (os.getenv("EMBED_ENDPOINT_MODE", "auto").strip().lower() or "auto")
        self._client = OpenAI(api_key=api_key, base_url=self.base_url, timeout=timeout)
        self.model = model

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
            input=[_normalize_text(text) for text in texts],
        )
        if not response.data:
            raise RuntimeError("Embedding query returned empty vectors.")
        partial: list[list[float] | None] = [None] * len(texts)
        for item in response.data:
            partial[int(item.index)] = list(item.embedding)
        vectors = [vector for vector in partial if vector is not None]
        if len(vectors) != len(texts):
            raise RuntimeError("Embedding query returned incomplete vectors.")
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
                "input": [{"type": "text", "text": _normalize_text(text)}],
                "encoding_format": "float",
            }
            if instructions:
                payload["instructions"] = instructions
            if dimensions is not None:
                payload["dimensions"] = dimensions

            response = httpx.post(
                f"{self.base_url}/embeddings/multimodal",
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
                detail = body if body is not None else response.text
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

    def embed_query(self, query: str) -> list[float]:
        text = _normalize_text(query)
        if self._prefer_multimodal():
            return self._embed_via_multimodal_api([text])[0]
        try:
            return self._embed_via_text_api([text])[0]
        except Exception as exc:
            if not self._should_retry_with_multimodal(exc):
                raise
            logger.warning(
                "[HybridSearch] embeddings.create rejected model=%s, retry with /embeddings/multimodal",
                self.model,
            )
            return self._embed_via_multimodal_api([text])[0]


@lru_cache(maxsize=1)
def _get_embedding_query_client() -> EmbeddingQueryClient:
    return EmbeddingQueryClient()


@lru_cache(maxsize=1)
def _get_entity_embedding_query_client() -> EmbeddingQueryClient:
    model = (
        os.getenv(ENTITY_EMBED_MODEL_ENV_VAR, "").strip()
        or DEFAULT_ENTITY_EMBED_MODEL
    )
    return EmbeddingQueryClient(model_override=model)


def _build_hybrid_filter_llm() -> ChatOpenAI:
    _load_runtime_env()
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing LLM_API_KEY for hybrid result filtering.")

    model_name = os.getenv("HYBRID_FILTER_LLM_MODEL", "deepseek-v3.2").strip() or "deepseek-v3.2"
    base_url = os.getenv("LLM_BASE_URL", "").strip()
    timeout = float(os.getenv("HYBRID_FILTER_TIMEOUT_SECONDS", "20").strip() or 20.0)
    kwargs: dict[str, Any] = {
        "model": model_name,
        "api_key": api_key,
        "temperature": 0,
        "timeout": timeout,
    }
    if base_url:
        kwargs["base_url"] = base_url
    apply_llm_network_settings(kwargs)
    return ChatOpenAI(**kwargs)


@lru_cache(maxsize=1)
def _get_hybrid_filter_llm() -> ChatOpenAI:
    return _build_hybrid_filter_llm()


def _get_hybrid_filter_mode() -> str:
    mode = (os.getenv("HYBRID_FILTER_MODE", "auto").strip().lower() or "auto")
    if mode in {"always", "never", "auto"}:
        return mode
    return "auto"


def _extract_query_terms(text: str) -> list[str]:
    tokens = re.findall(r"[\u4e00-\u9fffA-Za-z0-9_]+", str(text or ""))
    terms: list[str] = []
    seen: set[str] = set()
    split_pattern = "|".join(
        sorted((re.escape(word) for word in HYBRID_FILTER_STOPWORDS if word), key=len, reverse=True)
    )
    for token in tokens:
        normalized = token.strip().lower()
        if not normalized:
            continue
        parts = [normalized]
        if split_pattern and re.fullmatch(r"[\u4e00-\u9fff]+", normalized):
            fragments = [part.strip() for part in re.split(split_pattern, normalized) if part.strip()]
            if fragments:
                parts = fragments
        for part in parts:
            if not part or part in HYBRID_FILTER_STOPWORDS or part in seen:
                continue
            if len(part) == 1 and re.fullmatch(r"[\u4e00-\u9fff]", part):
                continue
            seen.add(part)
            terms.append(part)
    return terms


def _extract_candidate_text(candidate: dict[str, Any]) -> str:
    for key in ("chapter_summary", "plot_summary", "volume_summary"):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return _stringify_content(candidate).lower()


def _extract_candidate_score(candidate: dict[str, Any]) -> float | None:
    for key in ("rerank_score", "fused_score", "dense_score", "sparse_score"):
        value = candidate.get(key)
        try:
            score = float(value)
        except (TypeError, ValueError):
            continue
        if score >= 0:
            return score
    return None


def _keyword_coverage(terms: Sequence[str], text: str) -> float:
    if not terms:
        return 0.0
    hits = sum(1 for term in terms if term and term in text)
    return hits / max(1, len(terms))


def _should_skip_rerank(
    *,
    query: str,
    candidates: Sequence[dict[str, Any]],
    top_n: int,
) -> tuple[bool, str]:
    if not candidates:
        return True, "no_candidates"
    if len(candidates) == 1:
        return True, "skip_single_candidate"

    terms = _extract_query_terms(query)
    top1 = candidates[0]
    top2 = candidates[1] if len(candidates) > 1 else None
    top1_text = _extract_candidate_text(top1)
    coverage = _keyword_coverage(terms, top1_text)
    top1_score = _extract_candidate_score(top1)
    top2_score = _extract_candidate_score(top2) if top2 is not None else None
    relative_gap = None
    if top1_score is not None and top2_score is not None and abs(top1_score) > 1e-6:
        relative_gap = max(0.0, (top1_score - top2_score) / abs(top1_score))

    if len(candidates) <= 3 and top_n <= 3 and coverage >= 0.6:
        return True, "skip_rerank_small_precise_set"
    if len(candidates) <= 5 and coverage >= 0.7 and (relative_gap is None or relative_gap >= 0.18):
        return True, "skip_rerank_high_coverage_gap"
    return False, "rerank_needed"


def _is_complex_filter_query(text: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    return any(marker in normalized for marker in HYBRID_FILTER_COMPLEX_MARKERS)


def _should_run_llm_filter(
    *,
    agent_query: str,
    user_query: str,
    candidates: Sequence[dict[str, Any]],
) -> tuple[bool, str]:
    mode = _get_hybrid_filter_mode()
    if mode == "always":
        return True, "llm_filtered_forced"
    if mode == "never":
        return False, "filter_skipped_never"
    if not candidates:
        return False, "empty_candidates"
    if len(candidates) == 1:
        return False, "heuristic_skip_single_candidate"

    query_text = str(user_query or "").strip() or str(agent_query or "").strip()
    if _is_complex_filter_query(query_text):
        return True, "auto_complex_query"

    terms = _extract_query_terms(agent_query) or _extract_query_terms(query_text)
    top1 = candidates[0]
    top2 = candidates[1]
    top3 = candidates[2] if len(candidates) > 2 else None
    top1_text = _extract_candidate_text(top1)
    coverage = _keyword_coverage(terms, top1_text)
    top1_score = _extract_candidate_score(top1)
    top2_score = _extract_candidate_score(top2)
    top3_score = _extract_candidate_score(top3) if top3 is not None else None

    relative_gap = None
    if top1_score is not None and top2_score is not None and abs(top1_score) > 1e-6:
        relative_gap = max(0.0, (top1_score - top2_score) / abs(top1_score))

    concentration_gap = None
    if top1_score is not None and top3_score is not None and abs(top1_score) > 1e-6:
        concentration_gap = max(0.0, (top1_score - top3_score) / abs(top1_score))

    if len(candidates) <= 3 and coverage >= 0.6 and (relative_gap is None or relative_gap >= 0.2):
        return False, "heuristic_skip_precise_small_set"
    if (
        coverage >= 0.6
        and relative_gap is not None
        and relative_gap >= 0.15
        and (concentration_gap is None or concentration_gap >= 0.2)
    ):
        return False, "heuristic_skip_high_confidence_gap"
    if len(candidates) >= 8:
        return True, "auto_large_candidate_set"
    return True, "auto_ambiguous_candidates"


def warm_hybrid_runtime() -> None:
    try:
        get_qdrant_embedding_store()
    except Exception as exc:
        logger.warning("[Prewarm] qdrant store warm failed: %s", exc)
    try:
        _get_embedding_query_client()
    except Exception as exc:
        logger.warning("[Prewarm] embedding query client warm failed: %s", exc)
    if _hybrid_sparse_retrieval_enabled():
        try:
            _get_sparse_encoder()
        except Exception as exc:
            logger.warning("[Prewarm] sparse encoder warm failed: %s", exc)
    if _get_hybrid_filter_mode() != "never":
        try:
            _get_hybrid_filter_llm()
        except Exception as exc:
            logger.warning("[Prewarm] hybrid filter llm warm failed: %s", exc)


class RerankClient:
    def __init__(self) -> None:
        _load_runtime_env()
        self.provider = (os.getenv("RERANK_PROVIDER", "").strip().lower() or "ark")
        self.api_key = (
            os.getenv("RERANK_API_KEY", "").strip()
            or os.getenv("ERANK_API_KEY", "").strip()
            or os.getenv("DASHSCOPE_API_KEY", "").strip()
            or os.getenv("ARK_API_KEY", "").strip()
        )
        if not self.api_key and self.provider not in {"local", "openai_compatible"}:
            raise RuntimeError("Missing RERANK_API_KEY/ERANK_API_KEY/DASHSCOPE_API_KEY/ARK_API_KEY for rerank API.")
        if self.provider == "local":
            default_base_url = "http://127.0.0.1:58080/rerank"
        elif self.provider == "openai_compatible":
            default_base_url = "http://127.0.0.1:8007/v1"
        elif self.provider == "qwen":
            default_base_url = "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"
        else:
            default_base_url = "https://ark.cn-beijing.volces.com/api/v3"
        self.base_url = os.getenv("RERANK_BASE_URL", "").strip() or default_base_url
        self.timeout = float(os.getenv("RERANK_TIMEOUT_SECONDS", "20").strip() or 20.0)
        if self.provider == "local":
            default_model = "BAAI/bge-reranker-v2-m3"
        elif self.provider == "openai_compatible":
            default_model = "bge-reranker-v2-m3"
        elif self.provider == "qwen":
            default_model = "qwen3-rerank"
        else:
            default_model = "bge-reranker-v2-m3"
        self.model = os.getenv("RERANK_MODEL", default_model).strip() or default_model
        self.rerank_instruction = os.getenv("RERANK_INSTRUCTION", "").strip()
        self._client: Ark | None = None
        self._openai_client: OpenAI | None = None

    def _post_json_without_env_proxy(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> httpx.Response:
        # Rerank requests should bypass HTTP(S)_PROXY from the host environment,
        # especially for DashScope/Qwen endpoints that may reject proxy routing.
        with httpx.Client(timeout=self.timeout, trust_env=False) as client:
            return client.post(
                url,
                headers=headers,
                json=payload,
            )

    def route_name(self) -> str:
        if self._is_local_mode():
            return "local_rerank"
        if self._is_openai_compatible_mode():
            return "openai_compatible_rerank"
        if self._is_qwen_mode():
            return "qwen_rerank"
        if self._is_knowledge_service_mode():
            return "knowledge_service_rerank"
        return "ark_rerank"

    def _is_local_mode(self) -> bool:
        if self.provider == "local":
            return True
        base = self.base_url.rstrip("/").lower()
        return base.startswith("http://127.0.0.1:58080") or base.startswith("http://localhost:58080")

    def _is_openai_compatible_mode(self) -> bool:
        if self.provider == "openai_compatible":
            return True
        base = self.base_url.rstrip("/").lower()
        return base.endswith(":8007/v1") or base.endswith("/v1")

    def _is_qwen_mode(self) -> bool:
        if self._is_local_mode() or self._is_openai_compatible_mode():
            return False
        if self.provider == "qwen":
            return True
        base = self.base_url.rstrip("/").lower()
        return "dashscope.aliyuncs.com" in base or base.endswith("/v1/reranks")

    def _is_knowledge_service_mode(self) -> bool:
        base = self.base_url.rstrip("/")
        return "/api/knowledge/service/rerank" in base

    def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[dict[str, Any]]:
        if not documents:
            return []
        normalized_top_n = max(1, min(int(top_n), len(documents)))
        if self._is_local_mode():
            return self._rerank_via_local(query, documents, normalized_top_n)
        if self._is_openai_compatible_mode():
            return self._rerank_via_openai_compatible(query, documents, normalized_top_n)
        if self._is_qwen_mode():
            return self._rerank_via_qwen(query, documents, normalized_top_n)
        if self._is_knowledge_service_mode():
            return self._rerank_via_knowledge_service(query, documents, normalized_top_n)
        return self._rerank_via_ark(query, documents, normalized_top_n)

    def _rerank_via_local(
        self,
        query: str,
        documents: Sequence[str],
        top_n: int,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "model": self.model,
            "query": query,
            "documents": list(documents),
            "top_n": top_n,
        }
        headers = {"Content-Type": "application/json"}
        response = self._post_json_without_env_proxy(
            url=self.base_url,
            headers=headers,
            payload=payload,
        )
        body: dict[str, Any] | None = None
        try:
            parsed = response.json()
            body = parsed if isinstance(parsed, dict) else None
        except Exception:
            body = None
        if response.status_code >= 400:
            detail = response.text
            if isinstance(body, dict):
                detail = str(body.get("detail") or body.get("message") or body)
            raise RuntimeError(f"Local rerank HTTP {response.status_code}: {detail}")
        if not isinstance(body, dict):
            raise RuntimeError("Local rerank returned non-JSON response.")
        results = body.get("results")
        if not isinstance(results, list):
            raise RuntimeError("Local rerank response missing results.")
        normalized: list[dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            try:
                normalized.append({"index": int(item.get("index")), "score": float(item.get("score"))})
            except (TypeError, ValueError):
                continue
        if not normalized:
            raise RuntimeError("Local rerank response contains no valid result items.")
        normalized.sort(key=lambda row: row["score"], reverse=True)
        return normalized[:top_n]

    def _rerank_via_openai_compatible(
        self,
        query: str,
        documents: Sequence[str],
        top_n: int,
    ) -> list[dict[str, Any]]:
        if self._openai_client is None:
            self._openai_client = OpenAI(
                api_key=self.api_key or "EMPTY",
                base_url=self.base_url,
                http_client=httpx.Client(timeout=self.timeout, trust_env=False),
            )
        response = self._openai_client.post(
            "rerank",
            cast_to=object,
            body={
                "model": self.model,
                "query": query,
                "documents": list(documents),
                "top_n": top_n,
            },
        )
        body = response if isinstance(response, dict) else dict(response)
        results = body.get("results")
        if not isinstance(results, list):
            raise RuntimeError("OpenAI-compatible rerank response missing results.")
        normalized: list[dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            try:
                normalized.append({
                    "index": int(item.get("index")),
                    "score": float(item.get("score", item.get("relevance_score"))),
                })
            except (TypeError, ValueError):
                continue
        if not normalized:
            raise RuntimeError("OpenAI-compatible rerank response contains no valid result items.")
        normalized.sort(key=lambda row: row["score"], reverse=True)
        return normalized[:top_n]

    def _rerank_via_qwen(
        self,
        query: str,
        documents: Sequence[str],
        top_n: int,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "model": self.model,
            "query": query,
            "documents": list(documents),
            "top_n": top_n,
        }
        if self.rerank_instruction:
            payload["instruct"] = self.rerank_instruction

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        response = self._post_json_without_env_proxy(
            url=self.base_url,
            headers=headers,
            payload=payload,
        )
        body: dict[str, Any] | None = None
        try:
            parsed = response.json()
            body = parsed if isinstance(parsed, dict) else None
        except Exception:
            body = None
        if response.status_code >= 400:
            detail = ""
            if isinstance(body, dict):
                detail = str(
                    body.get("message")
                    or body.get("error")
                    or body.get("msg")
                    or body
                )
            if not detail:
                detail = response.text
            raise RuntimeError(f"Qwen rerank HTTP {response.status_code}: {detail}")
        if not isinstance(body, dict):
            raise RuntimeError("Qwen rerank returned non-JSON response.")

        results: Any = body.get("results")
        if not isinstance(results, list):
            output = body.get("output")
            if isinstance(output, dict) and isinstance(output.get("results"), list):
                results = output.get("results")
            else:
                results = None

        if isinstance(results, list):
            normalized: list[dict[str, Any]] = []
            for item in results:
                if not isinstance(item, dict):
                    continue
                index = item.get("index")
                score = item.get("relevance_score", item.get("score"))
                try:
                    normalized.append({"index": int(index), "score": float(score)})
                except (TypeError, ValueError):
                    continue
            if not normalized:
                raise RuntimeError("Qwen rerank response contains no valid result items.")
            normalized.sort(key=lambda row: row["score"], reverse=True)
            return normalized[:top_n]

        scores = body.get("data")
        if isinstance(scores, list):
            ranked_pairs: list[tuple[int, float]] = []
            for index, score in enumerate(scores):
                try:
                    ranked_pairs.append((int(index), float(score)))
                except (TypeError, ValueError):
                    continue
            ranked_pairs.sort(key=lambda item: item[1], reverse=True)
            if ranked_pairs:
                return [{"index": idx, "score": score} for idx, score in ranked_pairs[:top_n]]

        raise RuntimeError("Qwen rerank response missing results.")

    def _rerank_via_knowledge_service(
        self,
        query: str,
        documents: Sequence[str],
        top_n: int,
    ) -> list[dict[str, Any]]:
        datas = [{"query": query, "content": doc} for doc in documents]
        payload: dict[str, Any] = {
            "rerank_model": self.model,
            "datas": datas,
        }
        if self.model == "doubao-seed-rerank" and self.rerank_instruction:
            payload["rerank_instruction"] = self.rerank_instruction

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        response = self._post_json_without_env_proxy(
            url=self.base_url,
            headers=headers,
            payload=payload,
        )
        body: dict[str, Any] | None = None
        try:
            parsed = response.json()
            body = parsed if isinstance(parsed, dict) else None
        except Exception:
            body = None
        if response.status_code >= 400:
            detail = ""
            if isinstance(body, dict):
                detail = str(body.get("message", "") or body)
            if not detail:
                detail = response.text
            raise RuntimeError(f"Knowledge rerank HTTP {response.status_code}: {detail}")
        if not isinstance(body, dict):
            raise RuntimeError("Knowledge rerank returned non-JSON response.")
        if int(body.get("code", -1)) != 0:
            raise RuntimeError(
                f"Knowledge rerank failed: code={body.get('code')} message={body.get('message')}"
            )

        scores = body.get("data")
        if not isinstance(scores, list):
            raise RuntimeError("Knowledge rerank response missing `data` score list.")
        ranked_pairs: list[tuple[int, float]] = []
        for index, score in enumerate(scores):
            try:
                ranked_pairs.append((int(index), float(score)))
            except (TypeError, ValueError):
                continue
        ranked_pairs.sort(key=lambda item: item[1], reverse=True)
        return [{"index": idx, "score": score} for idx, score in ranked_pairs[:top_n]]

    def _rerank_via_ark(
        self,
        query: str,
        documents: Sequence[str],
        top_n: int,
    ) -> list[dict[str, Any]]:
        if self._client is None:
            self._client = Ark(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout)

        payload = {
            "model": self.model,
            "query": query,
            "documents": list(documents),
            "top_n": top_n,
            "return_documents": False,
        }
        response = None
        for path in ("/rerank", "rerank"):
            try:
                response = self._client.post(path, cast_to=dict, body=payload)
                break
            except Exception:
                response = None
                continue
        if response is None:
            raise RuntimeError("Rerank API request failed.")
        results = response.get("results") if isinstance(response, dict) else None
        if not isinstance(results, list):
            raise RuntimeError("Rerank API returned unexpected schema.")

        normalized: list[dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            score = item.get("relevance_score", item.get("score"))
            try:
                normalized.append({"index": int(index), "score": float(score)})
            except (TypeError, ValueError):
                continue
        return normalized


@lru_cache(maxsize=1)
def _get_rerank_client() -> RerankClient:
    return RerankClient()


def _try_import_sparse_encoder():
    if not _hybrid_sparse_retrieval_enabled():
        return None
    try:
        from fastembed import SparseTextEmbedding  # type: ignore
    except Exception:
        return None
    model_name = os.getenv("FASTEMBED_SPARSE_MODEL", "").strip() or "prithivida/Splade_PP_en_v1"
    cache_dir = _resolve_fastembed_cache_dir()
    os.environ.setdefault(FASTEMBED_CACHE_PATH_ENV_VAR, str(cache_dir))
    try:
        return SparseTextEmbedding(model_name=model_name, cache_dir=str(cache_dir))
    except Exception as exc:
        logger.warning(
            "[HybridSearch] failed to load sparse model=%s cache_dir=%s error=%s",
            model_name,
            cache_dir,
            exc,
        )
        return None


@lru_cache(maxsize=1)
def _get_sparse_encoder():
    return _try_import_sparse_encoder()


def _hybrid_sparse_retrieval_enabled() -> bool:
    raw = str(os.getenv(HYBRID_SPARSE_RETRIEVAL_ENABLED_ENV_VAR, "0") or "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _extract_sparse_vector(embedding: Any) -> tuple[list[int], list[float]]:
    indices = getattr(embedding, "indices", None)
    values = getattr(embedding, "values", None)
    if indices is None and isinstance(embedding, dict):
        indices = embedding.get("indices")
        values = embedding.get("values")
    if indices is None or values is None:
        return [], []

    safe_indices: list[int] = []
    safe_values: list[float] = []
    for index, value in zip(list(indices), list(values)):
        try:
            safe_indices.append(int(index))
            safe_values.append(float(value))
        except (TypeError, ValueError):
            continue
    return safe_indices, safe_values


def _sparse_dot_product(
    left_indices: Sequence[int],
    left_values: Sequence[float],
    right_indices: Sequence[int],
    right_values: Sequence[float],
) -> float:
    i = 0
    j = 0
    score = 0.0
    while i < len(left_indices) and j < len(right_indices):
        left_idx = left_indices[i]
        right_idx = right_indices[j]
        if left_idx == right_idx:
            score += float(left_values[i]) * float(right_values[j])
            i += 1
            j += 1
            continue
        if left_idx < right_idx:
            i += 1
            continue
        j += 1
    return score


def _tokenize_for_overlap(text: str) -> set[str]:
    normalized = text.lower()
    words = re.findall(r"[\u4e00-\u9fff]+|[a-z0-9_]+", normalized)
    tokens: set[str] = set()
    for word in words:
        clean = word.strip()
        if not clean:
            continue
        tokens.add(clean)
        if len(clean) >= 2 and re.search(r"[\u4e00-\u9fff]", clean):
            for idx in range(len(clean) - 1):
                tokens.add(clean[idx : idx + 2])
    return tokens


@dataclass
class SparseDocItem:
    point_id: Any
    payload: dict[str, Any]
    text: str
    indices: list[int]
    values: list[float]


@dataclass
class SparseBookIndex:
    built_at: float
    docs: list[SparseDocItem]
    mode: str


_SPARSE_CACHE_LOCK = threading.Lock()
_SPARSE_INDEX_CACHE: dict[tuple[str, int], SparseBookIndex] = {}


def _build_sparse_book_index(collection_name: str, book_id: int, text_field: str) -> SparseBookIndex:
    store = get_qdrant_embedding_store()
    points = store.scroll_payloads(collection_name, book_id)
    docs = []
    for point in points:
        payload = dict(point.get("payload", {}) or {})
        docs.append(
            SparseDocItem(
                point_id=point.get("id"),
                payload=payload,
                text=_normalize_text(payload.get(text_field, "")),
                indices=[],
                values=[],
            )
        )

    sparse_encoder = _get_sparse_encoder()
    if sparse_encoder is None:
        return SparseBookIndex(
            built_at=time.time(),
            docs=docs,
            mode="token_overlap_fallback",
        )

    embeddings = list(sparse_encoder.embed([doc.text for doc in docs])) if docs else []
    if len(embeddings) != len(docs):
        logger.warning(
            "[HybridSearch] sparse embeddings size mismatch: docs=%d vectors=%d, fallback token overlap.",
            len(docs),
            len(embeddings),
        )
        return SparseBookIndex(
            built_at=time.time(),
            docs=docs,
            mode="token_overlap_fallback",
        )

    for doc, embedding in zip(docs, embeddings):
        indices, values = _extract_sparse_vector(embedding)
        doc.indices = indices
        doc.values = values
    return SparseBookIndex(
        built_at=time.time(),
        docs=docs,
        mode="fastembed_sparse",
    )


def _get_sparse_book_index(collection_name: str, book_id: int, text_field: str) -> SparseBookIndex:
    ttl = max(10, int(os.getenv("HYBRID_SPARSE_CACHE_TTL_SECONDS", "1800").strip() or 1800))
    cache_key = (collection_name, int(book_id))
    now = time.time()
    with _SPARSE_CACHE_LOCK:
        cached = _SPARSE_INDEX_CACHE.get(cache_key)
        if cached and (now - cached.built_at) < ttl:
            return cached
    built = _build_sparse_book_index(collection_name, book_id, text_field)
    with _SPARSE_CACHE_LOCK:
        _SPARSE_INDEX_CACHE[cache_key] = built
    return built


def _sparse_score_dense_candidates(
    query: str,
    dense_hits: Sequence[dict[str, Any]],
    text_field: str,
    limit: int,
) -> tuple[list[dict[str, Any]], str]:
    if not dense_hits:
        return [], "dense_pool_empty"

    candidates: list[tuple[Any, dict[str, Any], str]] = []
    for hit in dense_hits:
        payload = dict(hit.get("payload", {}) or {})
        candidates.append((hit.get("id"), payload, _normalize_text(payload.get(text_field, ""))))

    sparse_encoder = _get_sparse_encoder() if _hybrid_sparse_retrieval_enabled() else None
    if sparse_encoder is not None:
        try:
            query_embedding = list(sparse_encoder.embed([_normalize_text(query)]))
            q_idx, q_val = _extract_sparse_vector(query_embedding[0] if query_embedding else None)
            if q_idx and q_val:
                doc_embeddings = list(sparse_encoder.embed([item[2] for item in candidates]))
                if len(doc_embeddings) == len(candidates):
                    scored: list[dict[str, Any]] = []
                    for (point_id, payload, _), doc_embedding in zip(candidates, doc_embeddings):
                        d_idx, d_val = _extract_sparse_vector(doc_embedding)
                        if not d_idx or not d_val:
                            continue
                        score = _sparse_dot_product(q_idx, q_val, d_idx, d_val)
                        if score <= 0:
                            continue
                        scored.append(
                            {
                                "id": point_id,
                                "score": float(score),
                                "payload": dict(payload),
                            }
                        )
                    scored.sort(key=lambda item: item["score"], reverse=True)
                    return scored[: max(1, int(limit))], "fastembed_sparse_dense_pool"
        except Exception as exc:
            logger.warning("[HybridSearch] sparse score over dense pool failed, fallback token overlap: %s", exc)

    query_tokens = _tokenize_for_overlap(query)
    scored = []
    for point_id, payload, text in candidates:
        doc_tokens = _tokenize_for_overlap(text)
        if not query_tokens or not doc_tokens:
            continue
        overlap = len(query_tokens & doc_tokens)
        if overlap <= 0:
            continue
        score = overlap / max(1, len(query_tokens))
        scored.append(
            {
                "id": point_id,
                "score": float(score),
                "payload": dict(payload),
            }
        )
    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[: max(1, int(limit))], "token_overlap_dense_pool"


def _dense_and_sparse_retrieve(
    query: str,
    book_id: int,
    spec: HybridSpec,
    query_client: EmbeddingQueryClient | None = None,
    source_ids: Sequence[int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    store = get_qdrant_embedding_store()
    if not store.collection_exists(spec.collection_name):
        return [], [], "collection_missing"

    client = query_client or _get_embedding_query_client()
    query_vector = client.embed_query(query)
    dense_hits = store.dense_search(
        collection_name=spec.collection_name,
        book_id=book_id,
        query_vector=query_vector,
        limit=spec.default_dense_k,
        source_ids=source_ids,
    )
    sparse_hits, sparse_mode = _sparse_score_dense_candidates(
        query=query,
        dense_hits=dense_hits,
        text_field=spec.text_field,
        limit=spec.default_sparse_k,
    )
    return dense_hits, sparse_hits, sparse_mode


def _fuse_candidates(
    dense_hits: Sequence[dict[str, Any]],
    sparse_hits: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}

    def _touch(point: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        point_id = str(point.get("id"))
        existing = by_id.get(point_id)
        if existing is None:
            existing = {
                "id": point.get("id"),
                "payload": dict(point.get("payload", {}) or {}),
                "dense_rank": None,
                "dense_score": 0.0,
                "sparse_rank": None,
                "sparse_score": 0.0,
                "fused_score": 0.0,
            }
            by_id[point_id] = existing
        return point_id, existing

    for rank, point in enumerate(dense_hits, start=1):
        _, entry = _touch(point)
        entry["dense_rank"] = rank
        entry["dense_score"] = float(point.get("score", 0.0) or 0.0)
        entry["fused_score"] += 1.0 / (RRF_K + rank)

    for rank, point in enumerate(sparse_hits, start=1):
        _, entry = _touch(point)
        entry["sparse_rank"] = rank
        entry["sparse_score"] = float(point.get("score", 0.0) or 0.0)
        entry["fused_score"] += 1.0 / (RRF_K + rank)

    ordered = list(by_id.values())
    ordered.sort(key=lambda item: item["fused_score"], reverse=True)
    return ordered


def _rerank_candidates(
    query: str,
    candidates: Sequence[dict[str, Any]],
    text_field: str,
    top_n: int,
) -> tuple[list[dict[str, Any]], str]:
    del query, text_field
    if not candidates:
        return [], "no_candidates"
    fallback = list(candidates)[:top_n]
    for rank, item in enumerate(fallback, start=1):
        item["rerank_score"] = None
        item["rerank_rank"] = rank
    return fallback, "rerank_disabled"


def _filter_results_with_llm(
    *,
    agent_query: str,
    user_query: str,
    candidates: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    if not candidates:
        return [], "empty_candidates"

    should_filter, mode_reason = _should_run_llm_filter(
        agent_query=agent_query,
        user_query=user_query,
        candidates=candidates,
    )
    if not should_filter:
        return list(candidates), mode_reason

    real_user_query = str(user_query or "").strip() or str(agent_query or "").strip()
    llm_input = (
        f"agent_query: {agent_query}\n"
        f"user_query: {real_user_query}\n"
        "candidates_json:\n"
        f"{json.dumps(list(candidates), ensure_ascii=False)}\n"
    )
    try:
        llm = _get_hybrid_filter_llm()
        response = llm.invoke(
            [
                SystemMessage(content=HYBRID_RESULT_FILTER_PROMPT),
                HumanMessage(content=llm_input),
            ]
        )
        payload = _extract_json_object(_stringify_content(getattr(response, "content", response)))
        if payload is None:
            return list(candidates), "llm_parse_fallback"
        kept_raw = payload.get("kept_ranks")
        if not isinstance(kept_raw, list):
            return list(candidates), "llm_schema_fallback"
        kept_ranks = {int(item) for item in kept_raw if isinstance(item, int) or str(item).isdigit()}
        ranked_map: dict[int, dict[str, Any]] = {}
        for item in candidates:
            rank_value = item.get("rank")
            try:
                rank_num = int(rank_value)
            except (TypeError, ValueError):
                continue
            ranked_map[rank_num] = item
        filtered = [ranked_map[rank] for rank in sorted(kept_ranks) if rank in ranked_map]
        if not filtered:
            return list(candidates), "llm_zero_hit_fallback"
        return filtered, "llm_filtered"
    except Exception as exc:
        logger.warning("[HybridSearch] llm filtering failed, fallback to unfiltered results: %s", exc)
        return list(candidates), "llm_error_fallback"


def _format_chapter_result(rank: int, item: dict[str, Any]) -> dict[str, Any]:
    payload = dict(item.get("payload", {}) or {})
    return {
        "rank": rank,
        "rerank_score": item.get("rerank_score"),
        "fused_score": item.get("fused_score"),
        "dense_score": item.get("dense_score"),
        "sparse_score": item.get("sparse_score"),
        "book_id": payload.get("book_id"),
        "chapter_index": payload.get("chapter_index"),
        "plot_id": payload.get("plot_id"),
        "chapter_summary": payload.get("chapter_summary", ""),
    }


def _format_plot_result(rank: int, item: dict[str, Any]) -> dict[str, Any]:
    payload = dict(item.get("payload", {}) or {})
    return {
        "rank": rank,
        "rerank_score": item.get("rerank_score"),
        "fused_score": item.get("fused_score"),
        "dense_score": item.get("dense_score"),
        "sparse_score": item.get("sparse_score"),
        "book_id": payload.get("book_id"),
        "plot_id": payload.get("plot_id"),
        "volume_id": payload.get("volume_id"),
        "start_chapter_index": payload.get("start_chapter_index"),
        "end_chapter_index": payload.get("end_chapter_index"),
        "plot_summary": payload.get("plot_summary", ""),
    }


def _format_volume_result(rank: int, item: dict[str, Any]) -> dict[str, Any]:
    payload = dict(item.get("payload", {}) or {})
    return {
        "rank": rank,
        "rerank_score": item.get("rerank_score"),
        "fused_score": item.get("fused_score"),
        "dense_score": item.get("dense_score"),
        "sparse_score": item.get("sparse_score"),
        "book_id": payload.get("book_id"),
        "volume_index": payload.get("volume_index"),
        "start_plot_index": payload.get("start_plot_index"),
        "end_plot_index": payload.get("end_plot_index"),
        "volume_summary": payload.get("volume_summary", ""),
    }


def _format_entity_result(rank: int, item: dict[str, Any]) -> dict[str, Any]:
    payload = dict(item.get("payload", {}) or {})
    return {
        "rank": rank,
        "rerank_score": item.get("rerank_score"),
        "fused_score": item.get("fused_score"),
        "dense_score": item.get("dense_score"),
        "sparse_score": item.get("sparse_score"),
        "book_id": payload.get("book_id"),
        "source_id": payload.get("source_id"),
        "name": payload.get("name", ""),
        "chapter_index": payload.get("chapter_index"),
        "record": payload.get("record", ""),
    }


def _hybrid_search(
    *,
    query: str,
    user_query: str,
    novel_title: str,
    book_id: int,
    spec: HybridSpec,
    return_k: int,
    formatter,
    tool_name: str,
    query_client: EmbeddingQueryClient | None = None,
    source_ids: Sequence[int] | None = None,
) -> str:
    normalized_query = (query or "").strip()
    if not normalized_query:
        return json.dumps(
            {
                "status": "error",
                "tool": tool_name,
                "error": "query 不能为空。",
            },
            ensure_ascii=False,
            indent=2,
        )

    resolved_book_id = _coerce_positive_int(book_id) or _resolve_book_id(novel_title)
    if not resolved_book_id:
        return json.dumps(
            {
                "status": "error",
                "tool": tool_name,
                "error": "无法解析有效 book_id。",
                "novel_title": (novel_title or "").strip() or None,
            },
            ensure_ascii=False,
            indent=2,
        )

    try:
        dense_hits, sparse_hits, sparse_mode = _dense_and_sparse_retrieve(
            query=normalized_query,
            book_id=resolved_book_id,
            spec=spec,
            query_client=query_client,
            source_ids=source_ids,
        )
    except Exception as exc:
        return json.dumps(
            {
                "status": "error",
                "tool": tool_name,
                "book_id": resolved_book_id,
                "error": f"混合检索执行失败: {exc}",
            },
            ensure_ascii=False,
            indent=2,
        )
    fused_candidates = _fuse_candidates(dense_hits, sparse_hits)
    reranked, rerank_mode = _rerank_candidates(
        query=normalized_query,
        candidates=fused_candidates,
        text_field=spec.text_field,
        top_n=max(1, int(return_k)),
    )

    formatted_results = [formatter(rank, item) for rank, item in enumerate(reranked, start=1)]
    filtered_results, llm_filter_mode = _filter_results_with_llm(
        agent_query=normalized_query,
        user_query=user_query,
        candidates=formatted_results,
    )
    payload = {
        "status": "success",
        "tool": tool_name,
        "book_id": resolved_book_id,
        "query": normalized_query,
        "user_query": (str(user_query or "").strip() or normalized_query),
        "retrieval": {
            "collection": spec.collection_name,
            "dense_hits": len(dense_hits),
            "sparse_hits": len(sparse_hits),
            "fused_candidates": len(fused_candidates),
            "sparse_mode": sparse_mode,
            "rerank_mode": rerank_mode,
            "llm_filter_mode": llm_filter_mode,
            "before_filter_count": len(formatted_results),
            "after_filter_count": len(filtered_results),
            "filter_source_ids": [int(item) for item in (source_ids or []) if int(item) > 0],
            "filter_applied": bool(source_ids),
            "rerank_model": os.getenv("RERANK_MODEL", "bge-reranker-v2-m3").strip() or "bge-reranker-v2-m3",
            "embed_model": (
                str(getattr(query_client, "model", "") or "").strip()
                or os.getenv("EMBED_MODEL", "ep-20251224183557-n8vdn").strip()
                or "ep-20251224183557-n8vdn"
            ),
            "filter_llm_model": os.getenv("HYBRID_FILTER_LLM_MODEL", "deepseek-v3.2").strip() or "deepseek-v3.2",
        },
        "results": filtered_results,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


@tool("hybrid_retrieve_chapter_summaries")
def hybrid_retrieve_chapter_summaries(
    query: str,
    user_query: str = "",
    novel_title: str = "",
    book_id: int = 0,
    top_k: int = 25,
    request_id: str = "",
) -> str:
    """
    章节摘要混合检索工具（chapter_summary）。
    对 `chapterSummaryEmbedding` 执行向量检索 + FastEmbed 稀疏关键词检索，再统一 rerank。
    适用于先快速定位“最可能命中”的章节摘要位置，并返回 chapter_index/plot_id/chapter_summary。
    参数 user_query 可传入用户原始问题，用于召回结果的二次 LLM 相关性过滤。
    """
    return _hybrid_search(
        query=query,
        user_query=user_query,
        novel_title=novel_title,
        book_id=book_id,
        spec=CHAPTER_HYBRID_SPEC,
        return_k=max(1, int(top_k)),
        formatter=_format_chapter_result,
        tool_name="hybrid_retrieve_chapter_summaries",
    )


@tool("hybrid_retrieve_plots")
def hybrid_retrieve_plots(
    query: str,
    user_query: str = "",
    novel_title: str = "",
    book_id: int = 0,
    top_k: int = 10,
    request_id: str = "",
) -> str:
    """
    情节摘要混合检索工具（plot_summary）。
    对 `plotSummaryEmbedding` 执行向量检索 + FastEmbed 稀疏关键词检索，再统一 rerank。
    返回 plot_id/volume_id/start_chapter_index/end_chapter_index/plot_summary，便于继续下潜章节核验。
    参数 user_query 可传入用户原始问题，用于召回结果的二次 LLM 相关性过滤。
    """
    return _hybrid_search(
        query=query,
        user_query=user_query,
        novel_title=novel_title,
        book_id=book_id,
        spec=PLOT_HYBRID_SPEC,
        return_k=max(1, int(top_k)),
        formatter=_format_plot_result,
        tool_name="hybrid_retrieve_plots",
    )


@tool("hybrid_retrieve_volumes")
def hybrid_retrieve_volumes(
    query: str,
    user_query: str = "",
    novel_title: str = "",
    book_id: int = 0,
    top_k: int = 3,
    request_id: str = "",
) -> str:
    """
    卷摘要混合检索工具（volume_summary）。
    对 `volumeSummaryEmbedding` 执行向量检索 + FastEmbed 稀疏关键词检索，再统一 rerank。
    返回 volume_index/start_plot_index/end_plot_index/volume_summary，用于快速锁定全局范围。
    参数 user_query 可传入用户原始问题，用于召回结果的二次 LLM 相关性过滤。
    """
    return _hybrid_search(
        query=query,
        user_query=user_query,
        novel_title=novel_title,
        book_id=book_id,
        spec=VOLUME_HYBRID_SPEC,
        return_k=max(1, int(top_k)),
        formatter=_format_volume_result,
        tool_name="hybrid_retrieve_volumes",
    )


def _entity_hybrid_search(
    *,
    query: str,
    user_query: str,
    novel_title: str,
    book_id: int,
    top_k: int,
    spec: HybridSpec,
    tool_name: str,
    source_ids: Sequence[int] | None = None,
) -> str:
    filtered_raw = _hybrid_search(
        query=query,
        user_query=user_query,
        novel_title=novel_title,
        book_id=book_id,
        spec=spec,
        return_k=max(1, int(top_k)),
        formatter=_format_entity_result,
        tool_name=tool_name,
        query_client=_get_entity_embedding_query_client(),
        source_ids=source_ids,
    )
    if not source_ids:
        return filtered_raw

    filtered_payload = _extract_json_object(filtered_raw) or {}
    filtered_results = filtered_payload.get("results", []) if isinstance(filtered_payload.get("results"), list) else []
    if filtered_results:
        retrieval = filtered_payload.get("retrieval")
        if isinstance(retrieval, dict):
            retrieval["broad_retry_after_filter_miss"] = False
        return json.dumps(filtered_payload, ensure_ascii=False, indent=2)

    broad_raw = _hybrid_search(
        query=query,
        user_query=user_query,
        novel_title=novel_title,
        book_id=book_id,
        spec=spec,
        return_k=max(1, int(top_k)),
        formatter=_format_entity_result,
        tool_name=tool_name,
        query_client=_get_entity_embedding_query_client(),
        source_ids=None,
    )
    broad_payload = _extract_json_object(broad_raw) or {}
    retrieval = broad_payload.get("retrieval")
    if isinstance(retrieval, dict):
        retrieval["filter_applied"] = True
        retrieval["broad_retry_after_filter_miss"] = True
        retrieval["initial_filter_source_ids"] = [int(item) for item in source_ids if int(item) > 0]
    return json.dumps(broad_payload, ensure_ascii=False, indent=2)


@tool("hybrid_retrieve_characters")
def hybrid_retrieve_characters(
    query: str,
    user_query: str = "",
    novel_title: str = "",
    book_id: int = 0,
    top_k: int = 8,
    request_id: str = "",
    source_ids: Sequence[int] = (),
) -> str:
    """
    角色实体混合检索工具。
    对 `characters` collection 执行向量检索 + 稀疏关键词检索，再统一 rerank。
    返回实体名、record 文本与 chapter_index，适合做人名/称号/身份定位首跳。
    """
    _ = request_id
    return _entity_hybrid_search(
        query=query,
        user_query=user_query,
        novel_title=novel_title,
        book_id=book_id,
        top_k=top_k,
        spec=CHARACTER_HYBRID_SPEC,
        tool_name="hybrid_retrieve_characters",
        source_ids=source_ids,
    )


@tool("hybrid_retrieve_origanizations")
def hybrid_retrieve_origanizations(
    query: str,
    user_query: str = "",
    novel_title: str = "",
    book_id: int = 0,
    top_k: int = 8,
    request_id: str = "",
    source_ids: Sequence[int] = (),
) -> str:
    """
    组织/势力实体混合检索工具。
    对 `origanizations` collection 执行向量检索 + 稀疏关键词检索，再统一 rerank。
    返回实体名、record 文本与 chapter_index，适合做组织关系与归属问题首跳。
    """
    _ = request_id
    return _entity_hybrid_search(
        query=query,
        user_query=user_query,
        novel_title=novel_title,
        book_id=book_id,
        top_k=top_k,
        spec=ORIGANIZATION_HYBRID_SPEC,
        tool_name="hybrid_retrieve_origanizations",
        source_ids=source_ids,
    )


@tool("hybrid_retrieve_special_existences")
def hybrid_retrieve_special_existences(
    query: str,
    user_query: str = "",
    novel_title: str = "",
    book_id: int = 0,
    top_k: int = 6,
    request_id: str = "",
    source_ids: Sequence[int] = (),
) -> str:
    """
    特殊存在/特殊物品混合检索工具。
    对 `special_existences` collection 执行向量检索 + 稀疏关键词检索，再统一 rerank。
    返回实体名、record 文本与 chapter_index，适合做设定与特殊存在问题首跳。
    """
    _ = request_id
    return _entity_hybrid_search(
        query=query,
        user_query=user_query,
        novel_title=novel_title,
        book_id=book_id,
        top_k=top_k,
        spec=SPECIAL_EXISTENCE_HYBRID_SPEC,
        tool_name="hybrid_retrieve_special_existences",
        source_ids=source_ids,
    )


@tool("hybrid_retrieve_world_rules")
def hybrid_retrieve_world_rules(
    query: str,
    user_query: str = "",
    novel_title: str = "",
    book_id: int = 0,
    top_k: int = 6,
    request_id: str = "",
    source_ids: Sequence[int] = (),
) -> str:
    """
    世界规则/设定混合检索工具。
    对 `world_rules` collection 执行向量检索 + 稀疏关键词检索，再统一 rerank。
    返回实体名、record 文本与 chapter_index，适合做规则与设定问题首跳。
    """
    _ = request_id
    return _entity_hybrid_search(
        query=query,
        user_query=user_query,
        novel_title=novel_title,
        book_id=book_id,
        top_k=top_k,
        spec=WORLD_RULE_HYBRID_SPEC,
        tool_name="hybrid_retrieve_world_rules",
        source_ids=source_ids,
    )
