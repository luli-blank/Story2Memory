from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urlparse

import pymysql
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool, tool
from langchain_openai import ChatOpenAI

from agent.deepSearch import (
    resolve_book_id,
    retrieve_chapter_directory,
    retrieve_chapter_summaries,
    retrieve_chapters,
    warm_outline_cache,
)
from agent.hybridSearch import (
    hybrid_retrieve_chapter_summaries,
    hybrid_retrieve_characters,
    hybrid_retrieve_origanizations,
    hybrid_retrieve_plots,
    hybrid_retrieve_special_existences,
    hybrid_retrieve_volumes,
    hybrid_retrieve_world_rules,
    warm_hybrid_runtime,
)
from agent.graph import apply_llm_network_settings, wrap_tracked_llm
from agent.skills.retrieval_route_skill.route_skill import plan_multi_search_route
from database.qdrant_client import get_qdrant_embedding_store

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_LOG_DIR = Path.home() / ".story2memory" / "logs" / "search_agent"
PROJECT_LOG_PROXY_DIR = ROOT_DIR / "data" / "logs" / "search_agent_runtime"
SEARCH_AGENT_LOG_DIR_ENV_VAR = "SEARCH_AGENT_LOG_DIR"
ENV_OVERRIDE_VAR = "STORY2MEMORY_ENV_OVERRIDE"
MAX_WINDOW_SIZE = 8
MAX_TOOL_CALLS = 6
MAX_FULLTEXT_WINDOWS = 2
LOW_CONFIDENCE_THRESHOLD = 0.45
FULLTEXT_INTENTS = {"quote_micro_detail"}
SUMMARY_ONLY_SOURCES = {"chapter_summaries", "chapter_summaries_verify"}
RECOVERY_CONFIDENCE_THRESHOLD = 0.78
RECOVERY_MAX_STEPS = 3
RECOVERY_MAX_EXTRA_FULLTEXT_READS = 1
MIN_GLOBAL_TOOL_CALLS = 4
MAX_GLOBAL_TOOL_CALLS = 14
MIN_GLOBAL_FULLTEXT_WINDOWS = 1
MAX_GLOBAL_FULLTEXT_WINDOWS = 3
MIN_PARALLEL_FIRST_HOP = 1
MAX_PARALLEL_FIRST_HOP = 4
MAX_RECOVERY_STEPS_GLOBAL = 4
MAX_RECOVERY_STEPS_PER_SUBQUERY = 2
BASE_TOOL_BUDGET_BY_INTENT = {
    "existence_check": 4,
    "first_appearance": 5,
    "identity_ability": 6,
    "ending_fate": 6,
    "general_fact": 6,
    "causal_motivation": 7,
    "timeline_evolution": 8,
    "quote_micro_detail": 8,
}
BASELINE_TOOL_BUDGET_BY_SUBQUERY_COUNT = {
    1: 8,
    2: 10,
    3: 12,
}
FULLTEXT_HEAVY_INTENTS = {"timeline_evolution", "quote_micro_detail"}
FULLTEXT_MEDIUM_INTENTS = {"causal_motivation", "identity_ability", "ending_fate", "first_appearance", "general_fact"}
ENTITY_TABLES = (
    "characters",
    "origanizations",
    "special_existences",
    "world_rules",
)
ENTITY_TOOL_BY_TYPE = {
    "characters": "hybrid_retrieve_characters",
    "origanizations": "hybrid_retrieve_origanizations",
    "special_existences": "hybrid_retrieve_special_existences",
    "world_rules": "hybrid_retrieve_world_rules",
}
EDGE_RECORD_TOOL_NAME = "retrieve_entity_edge_records"
DIRECT_FULLTEXT_INTENTS = {"first_appearance", "ending_fate", "quote_micro_detail"}
ENTITY_EVIDENCE_SOURCES = {
    "characters",
    "origanizations",
    "special_existences",
    "world_rules",
    "entity_edge_first",
    "entity_edge_last",
}
PLOT_EVIDENCE_SOURCES = {"plots", "volumes"}
QUERY_REFINEMENT_STOPWORDS = {
    "请",
    "确认",
    "核验",
    "提取",
    "围绕",
    "以下",
    "线索",
    "剧情",
    "内容",
    "相关",
    "章节",
    "原文",
    "依据",
    "故事",
    "问题",
    "本章",
    "本章节",
    "主要",
    "存在",
    "一种",
    "一个",
}
RECOVERY_PLANNER_PROMPT = """
# Role
你是 Story2Memory search agent 的恢复式检索规划器。固定检索路径没有拿到足够稳定的证据时，你需要在允许的工具集合内选择下一步最有价值的一次调用。

# Output
只输出 JSON，不要解释，不要 markdown。

# JSON Schema
{
  "next_tool": "retrieve_chapter_summaries",
  "args": {
    "query": "关键词",
    "start_chapter_index": 1,
    "end_chapter_index": 3,
    "top_k": 5,
    "intent": "完整问题"
  },
  "reason": "一句话说明为什么这样做",
  "expected_gain": "将获得什么新坐标或证据"
}

# Rules
1. 只能从允许的工具中选一个。
2. 优先最小增量：先拿新坐标，再决定是否读原文。
3. 禁止重复相同工具和相同参数。
4. 若当前已有候选 chapter window，优先围绕它细化；不要无边界扩张。
5. 只有在需要原文细节或章节摘要无效时，才选 `retrieve_chapters`。
6. 若涉及“标题章号不一致”，才选 `retrieve_chapter_directory`。
7. `args` 只填写本次工具真正需要的参数。
"""


@dataclass
class EvidenceItem:
    source: str
    snippet: str
    score: float
    subquery_ids: tuple[str, ...] = ()
    chapter_index: int | None = None
    plot_id: int | None = None
    volume_index: int | None = None
    start_chapter_index: int | None = None
    end_chapter_index: int | None = None
    source_name: str = ""
    raw: dict[str, Any] | None = None


@dataclass
class GroundingMatch:
    entity_type: str
    source_id: int
    canonical_name: str
    matched_name: str
    matched_alias: str
    match_mode: str
    confidence: float


@dataclass
class SubqueryState:
    subquery_id: str
    label: str
    user_goal: str
    intent_type: str
    priority: int
    is_explicit: bool
    plan: dict[str, Any]
    status: str = "pending"
    candidate_window: tuple[int, int] | None = None
    used_tools: list[dict[str, Any]] | None = None
    confidence: float = 0.0
    recovery_used: bool = False
    recovery_trace: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.used_tools is None:
            self.used_tools = []
        if self.recovery_trace is None:
            self.recovery_trace = []


@dataclass(frozen=True)
class ConfirmedEvidenceItem:
    subquery_id: str
    intent_type: str
    label: str
    chapter_index: int
    source: str
    source_name: str
    claim: str
    excerpt: str
    confidence: float


class _SearchAgentGraph:
    def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
        messages = list(state.get("messages", []) or [])
        user_query = ""
        for message in reversed(messages):
            content = getattr(message, "content", "")
            if str(content or "").strip():
                user_query = str(content or "").strip()
                break
        book_id = _to_positive_int(state.get("book_id")) or 0
        result = run_deep_research(book_id=book_id, query=user_query)
        messages.append(AIMessage(content=str(result.get("answer", "") or "")))
        return {"messages": messages, "search_result": result}


def _load_runtime_env() -> None:
    load_dotenv(dotenv_path=ROOT_DIR / ".env")
    override_path = os.getenv(ENV_OVERRIDE_VAR)
    if override_path:
        load_dotenv(dotenv_path=override_path, override=True)


def _parse_mysql_dsn(dsn: str) -> dict[str, Any] | None:
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


def _to_positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


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


def _strip_name_wrappers(text: str) -> str:
    cleaned = str(text or "").strip()
    if cleaned.startswith("<") and cleaned.endswith(">") and len(cleaned) > 2:
        cleaned = cleaned[1:-1].strip()
    if cleaned.startswith("《") and cleaned.endswith("》") and len(cleaned) > 2:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def _canonicalize_entity_name(value: Any) -> str:
    return _strip_name_wrappers(str(value or "").strip())


def _normalize_entity_name(value: Any) -> str:
    text = _canonicalize_entity_name(value)
    return re.sub(r"\s+", "", text).lower()


def _clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(minimum, min(maximum, parsed))


@lru_cache(maxsize=128)
def _load_entity_grounding_rows(book_id: int, table_name: str) -> tuple[tuple[int, str, tuple[str, ...]], ...]:
    if table_name not in ENTITY_TABLES:
        return tuple()
    try:
        with _connect_mysql() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT id, name, aliases
                    FROM `{table_name}`
                    WHERE book_id = %s
                    ORDER BY id ASC
                    """,
                    (int(book_id),),
                )
                rows = list(cursor.fetchall() or [])
    except Exception:
        logger.exception("Failed to load entity grounding rows: book_id=%s table=%s", book_id, table_name)
        return tuple()

    normalized_rows: list[tuple[int, str, tuple[str, ...]]] = []
    for row in rows:
        source_id = _to_positive_int(row.get("id"))
        name = _canonicalize_entity_name(row.get("name"))
        if not source_id or not name:
            continue
        aliases = []
        for alias in _parse_json_list(row.get("aliases")):
            alias_text = _canonicalize_entity_name(alias)
            if alias_text and alias_text not in aliases and alias_text != name:
                aliases.append(alias_text)
        normalized_rows.append((int(source_id), name, tuple(aliases)))
    return tuple(normalized_rows)


def _extract_json_object(raw: Any) -> dict[str, Any] | None:
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


def _stringify_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
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


def _dedupe_terms(terms: Sequence[str], limit: int) -> list[str]:
    collected: list[str] = []
    seen: set[str] = set()
    for item in terms:
        text = str(item or "").strip()
        if not text:
            continue
        normalized = re.sub(r"\s+", "", text).lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        collected.append(text)
        if len(collected) >= limit:
            break
    return collected


def _extract_refinement_terms(text: str, limit: int = 6) -> list[str]:
    fragments = re.split(r"[\s\n，,。；;：:、!?？（）()<>《》【】\\[\\]\\-]+", str(text or ""))
    terms: list[str] = []
    for fragment in fragments:
        candidate = fragment.strip()
        if not candidate or len(candidate) < 2 or len(candidate) > 16:
            continue
        if candidate in QUERY_REFINEMENT_STOPWORDS:
            continue
        if any(stopword in candidate for stopword in ("当前批次", "未提及任何", "无相关", "证据不足")):
            continue
        terms.append(candidate)
    return _dedupe_terms(terms, limit=limit)


def _theme_overlap_ratio(term: str, anchor_term: str) -> float:
    left = {char for char in str(term or "").strip() if char and not char.isspace()}
    right = {char for char in str(anchor_term or "").strip() if char and not char.isspace()}
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, len(right))


def _looks_like_negative_evidence(text: str) -> bool:
    normalized = _stringify_content(text)
    if not normalized:
        return True
    negative_markers = (
        "未提及",
        "未发现",
        "无相关",
        "没有提及",
        "无法确认",
        "证据不足",
        "不支持",
        "未在本次",
        "未找到",
    )
    return any(marker in normalized for marker in negative_markers)


def _window_span(window: tuple[int, int] | None) -> int:
    if window is None:
        return 0
    return max(0, int(window[1]) - int(window[0]) + 1)


def _normalize_question_text(text: Any) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return "请定位可回答该问题的章节级证据。"
    if not normalized.endswith(("？", "?", "。")):
        normalized += "？"
    return normalized


def _fetch_chapter_content(book_id: int, chapter_index: int) -> str:
    try:
        with _connect_mysql() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT content
                    FROM book_chapters
                    WHERE book_id = %s AND chapter_index = %s
                    LIMIT 1
                    """,
                    (int(book_id), int(chapter_index)),
                )
                row = cursor.fetchone()
        if not row:
            return ""
        return _stringify_content(row.get("content", ""))
    except Exception:
        logger.exception("Failed to fetch chapter content: book_id=%s chapter_index=%s", book_id, chapter_index)
        return ""


def _confirmed_evidence_limit(intent_type: str) -> int:
    if intent_type in {"existence_check", "first_appearance"}:
        return 1
    if intent_type in {"identity_ability", "ending_fate", "general_fact", "causal_motivation", "quote_micro_detail"}:
        return 2
    if intent_type == "timeline_evolution":
        return 3
    return 2


def _resolve_log_dir() -> Path:
    raw_dir = str(os.getenv(SEARCH_AGENT_LOG_DIR_ENV_VAR, "") or "").strip()
    if raw_dir:
        candidate = Path(raw_dir).expanduser()
        if not candidate.is_absolute():
            candidate = (ROOT_DIR / candidate).resolve()
        else:
            candidate = candidate.resolve()
    else:
        candidate = DEFAULT_LOG_DIR

    root_dir = ROOT_DIR.resolve()
    if candidate == root_dir or root_dir in candidate.parents:
        logger.warning(
            "[SearchAgent] log dir is inside project tree: %s. Fallback to runtime log dir outside project: %s",
            candidate,
            DEFAULT_LOG_DIR,
        )
        return DEFAULT_LOG_DIR
    return candidate


def _ensure_project_log_proxy(log_dir: Path) -> Path | None:
    try:
        proxy_path = PROJECT_LOG_PROXY_DIR
        if proxy_path.exists():
            if proxy_path.is_symlink():
                try:
                    if proxy_path.resolve() == log_dir.resolve():
                        return proxy_path
                except Exception:
                    pass
            return None

        proxy_path.parent.mkdir(parents=True, exist_ok=True)
        proxy_path.symlink_to(log_dir, target_is_directory=True)
        return proxy_path
    except Exception:
        logger.exception("Failed to create project log proxy for search agent logs.")
        return None


def _safe_log_filename_fragment(value: str, default: str = "search") -> str:
    text = str(value or "").strip()
    if not text:
        return default
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    normalized = normalized.strip("._-")
    return normalized[:80] or default


def _json_dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _build_search_log_markdown(context: dict[str, Any], result: dict[str, Any]) -> str:
    generated_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    request_id = str(result.get("request_id") or context.get("request_id") or "")
    lines = [
        "# Search Agent Log",
        "",
        f"- generated_at: {generated_at}",
        f"- request_id: {request_id or 'N/A'}",
        f"- status: {result.get('status', 'unknown')}",
        f"- route_type: {result.get('route_type', context.get('intent_type', ''))}",
        f"- input_book_id: {context.get('input_book_id', 0)}",
        f"- resolved_book_id: {context.get('resolved_book_id', result.get('book_id', 0))}",
        f"- novel_title: {context.get('novel_title', '')}",
        f"- query: {context.get('query', result.get('query', ''))}",
        f"- elapsed_sec: {result.get('elapsed_sec', context.get('elapsed_sec', 'N/A'))}",
        "",
        "## Invocation Context",
        "```json",
        _json_dump(context),
        "```",
        "",
        "## Grounding",
        "```json",
        _json_dump(result.get("grounding", {})),
        "```",
        "",
        "## Decomposition",
        "```json",
        _json_dump(result.get("decomposition", [])),
        "```",
        "",
        "## Subquery Plans",
        "```json",
        _json_dump(result.get("subqueries", [])),
        "```",
        "",
        "## Subquery States",
        "```json",
        _json_dump(result.get("subquery_states", [])),
        "```",
        "",
        "## Shared Evidence Pool Summary",
        "```json",
        _json_dump(result.get("shared_evidence_pool_summary", [])),
        "```",
        "",
        "- tool_calls:",
        "```json",
        _json_dump(result.get("tool_calls", [])),
        "```",
        "",
        "## Evidence",
        "```json",
        _json_dump(result.get("evidence", [])),
        "```",
        "",
        "## Final Result",
        "```json",
        _json_dump(result),
        "```",
        "",
        "## Recovery",
        "```json",
        _json_dump(
            {
                "recovery_used": result.get("recovery_used", False),
                "recovery_reason": result.get("recovery_reason", ""),
                "recovery_trace": result.get("recovery_trace", []),
                "recovery_trace_by_subquery": result.get("recovery_trace_by_subquery", {}),
            }
        ),
        "```",
        "",
    ]
    return "\n".join(lines)


def _write_search_log(context: dict[str, Any], result: dict[str, Any]) -> None:
    try:
        log_dir = _resolve_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        proxy_dir = _ensure_project_log_proxy(log_dir)
        request_id = str(result.get("request_id") or context.get("request_id") or "")
        timestamp_ms = int(context.get("started_at_ms") or time.time() * 1000)
        filename = f"{timestamp_ms}-{_safe_log_filename_fragment(request_id)}.md"
        path = log_dir / filename
        path.write_text(_build_search_log_markdown(context, result), encoding="utf-8")
        result["log_path"] = str(path)
        if proxy_dir is not None:
            result["project_log_path"] = str(proxy_dir / filename)
    except Exception:
        logger.exception("Failed to write search log: request_id=%s", result.get("request_id") or context.get("request_id"))


def _finalize_search_result(context: dict[str, Any], result: dict[str, Any], started_at: float) -> dict[str, Any]:
    if "elapsed_sec" not in result:
        result["elapsed_sec"] = round(time.perf_counter() - started_at, 3)
    context["elapsed_sec"] = result.get("elapsed_sec")
    _write_search_log(context, result)
    return result


def _extract_score(payload: dict[str, Any]) -> float:
    for key in ("rerank_score", "fused_score", "dense_score", "sparse_score", "score"):
        value = payload.get(key)
        try:
            if value is None:
                continue
            return float(value)
        except (TypeError, ValueError):
            continue
    rank = _to_positive_int(payload.get("rank"))
    if rank:
        return 1.0 / float(rank)
    return 0.0


def _source_weight(source: str) -> float:
    if source == "chapters":
        return 1.0
    if source == "chapter_summaries_verify":
        return 0.7
    if source == "chapter_summaries":
        return 0.62
    if source == "plots":
        return 0.72
    if source == "volumes":
        return 0.55
    return 0.65


def _evidence_score(source: str, payload: dict[str, Any]) -> float:
    base = _extract_score(payload)
    if base <= 0:
        rank = _to_positive_int(payload.get("rank")) or 5
        base = 1.0 / float(rank)
    return base * _source_weight(source)


def _tool_to_entity_type(tool_name: str) -> str | None:
    for entity_type, entity_tool in ENTITY_TOOL_BY_TYPE.items():
        if entity_tool == tool_name:
            return entity_type
    return None


def _best_grounding_match_for_row(
    *,
    scan_texts: Sequence[str],
    canonical_name: str,
    aliases: Sequence[str],
    entity_type: str,
    source_id: int,
) -> GroundingMatch | None:
    normalized_name = _normalize_entity_name(canonical_name)
    best_match: GroundingMatch | None = None

    def _consider(match_text: str, match_mode: str, confidence: float) -> None:
        nonlocal best_match
        candidate = GroundingMatch(
            entity_type=entity_type,
            source_id=int(source_id),
            canonical_name=canonical_name,
            matched_name=canonical_name,
            matched_alias=match_text,
            match_mode=match_mode,
            confidence=float(confidence),
        )
        if best_match is None or candidate.confidence > best_match.confidence:
            best_match = candidate

    for raw_scan in scan_texts:
        rendered = str(raw_scan or "").strip()
        if not rendered:
            continue
        normalized_scan = _normalize_entity_name(rendered)
        if canonical_name and canonical_name in rendered:
            _consider(canonical_name, "exact_name", 1.0)
        elif normalized_name and len(normalized_name) >= 2 and normalized_name in normalized_scan:
            _consider(canonical_name, "substring_alias", 0.86)

        for alias in aliases:
            normalized_alias = _normalize_entity_name(alias)
            if len(normalized_alias) < 2:
                continue
            if alias in rendered:
                _consider(alias, "exact_alias", 0.97)
            elif normalized_alias in normalized_scan:
                _consider(alias, "substring_alias", 0.84)
    return best_match


def _collect_entity_grounding(
    *,
    book_id: int,
    user_query: str,
    entity_query: str,
    entity_tables: Sequence[str],
) -> dict[str, Any]:
    scan_texts = [str(user_query or "").strip(), str(entity_query or "").strip()]
    tables = [table for table in entity_tables if table in ENTITY_TABLES]
    if not tables:
        tables = list(ENTITY_TABLES)

    raw_matches: list[GroundingMatch] = []
    for table_name in tables:
        for source_id, canonical_name, aliases in _load_entity_grounding_rows(int(book_id), table_name):
            match = _best_grounding_match_for_row(
                scan_texts=scan_texts,
                canonical_name=canonical_name,
                aliases=aliases,
                entity_type=table_name,
                source_id=source_id,
            )
            if match is not None:
                raw_matches.append(match)

    raw_matches.sort(
        key=lambda item: (
            item.confidence,
            len(item.matched_alias or ""),
            len(item.canonical_name or ""),
        ),
        reverse=True,
    )
    serialized_raw = [asdict(item) for item in raw_matches[:20]]
    if not raw_matches:
        return {
            "applied": False,
            "reason": "no_grounding_match",
            "entity_type": "",
            "source_ids": [],
            "selected": [],
            "raw_candidates": serialized_raw,
        }

    best_confidence = raw_matches[0].confidence
    high_conf_matches = [item for item in raw_matches if item.confidence >= 0.97 and item.match_mode in {"exact_name", "exact_alias"}]
    if not high_conf_matches:
        return {
            "applied": False,
            "reason": "no_high_confidence_grounding",
            "entity_type": "",
            "source_ids": [],
            "selected": [],
            "raw_candidates": serialized_raw,
        }

    unique_sources = {(item.entity_type, item.source_id) for item in high_conf_matches}
    unique_targets = {(item.entity_type, _normalize_entity_name(item.canonical_name)) for item in high_conf_matches}
    if len(unique_sources) > 1 and len(unique_targets) > 1:
        return {
            "applied": False,
            "reason": "ambiguous_grounding",
            "entity_type": "",
            "source_ids": [],
            "selected": [],
            "raw_candidates": serialized_raw,
        }

    selected_matches = high_conf_matches
    selected_entity_type = selected_matches[0].entity_type
    selected_source_ids = sorted({int(item.source_id) for item in selected_matches if item.entity_type == selected_entity_type})
    return {
        "applied": True,
        "reason": "high_confidence_alias_grounding",
        "entity_type": selected_entity_type,
        "source_ids": selected_source_ids,
        "selected": [asdict(item) for item in selected_matches],
        "raw_candidates": serialized_raw,
        "top_confidence": best_confidence,
    }


@tool(EDGE_RECORD_TOOL_NAME)
def retrieve_entity_edge_records(
    entity_query: str,
    novel_title: str = "",
    book_id: int = 0,
    entity_type_hint: str = "",
    edge: str = "first",
    limit: int = 3,
    request_id: str = "",
) -> str:
    """
    直接提取单个实体在 qdrant collection 中的边界 records。
    先用实体名/aliases 做精确配对，再按 chapter_index 排序，返回最早或最后的若干条记录。
    """
    _ = (novel_title, request_id)
    normalized_query = str(entity_query or "").strip()
    if not normalized_query:
        return json.dumps(
            {
                "status": "error",
                "tool": EDGE_RECORD_TOOL_NAME,
                "error": "entity_query 不能为空。",
                "records": [],
            },
            ensure_ascii=False,
            indent=2,
        )

    resolved_book_id = _to_positive_int(book_id)
    if not resolved_book_id:
        return json.dumps(
            {
                "status": "error",
                "tool": EDGE_RECORD_TOOL_NAME,
                "error": "无法解析有效 book_id。",
                "records": [],
            },
            ensure_ascii=False,
            indent=2,
        )

    entity_tables = [str(entity_type_hint or "").strip()] if str(entity_type_hint or "").strip() in ENTITY_TABLES else list(ENTITY_TABLES)
    payload = _resolve_entity_edge_records(
        book_id=int(resolved_book_id),
        entity_query=normalized_query,
        entity_tables=entity_tables,
        edge=edge,
        limit=max(1, int(limit)),
    )
    payload["tool"] = EDGE_RECORD_TOOL_NAME
    payload["book_id"] = int(resolved_book_id)
    payload["entity_query"] = normalized_query
    payload["entity_type_hint"] = str(entity_type_hint or "").strip() or None
    payload["edge"] = str(edge or "first").strip().lower() or "first"
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _resolve_entity_edge_records(
    *,
    book_id: int,
    entity_query: str,
    entity_tables: Sequence[str],
    edge: str,
    limit: int,
) -> dict[str, Any]:
    normalized_edge = str(edge or "").strip().lower()
    if normalized_edge not in {"first", "last"}:
        return {
            "status": "error",
            "error": "invalid_edge",
            "records": [],
        }

    grounding = _collect_entity_grounding(
        book_id=int(book_id),
        user_query=entity_query,
        entity_query=entity_query,
        entity_tables=entity_tables,
    )
    if not grounding.get("applied"):
        return {
            "status": "error",
            "error": str(grounding.get("reason", "grounding_failed") or "grounding_failed"),
            "grounding": grounding,
            "records": [],
        }

    entity_type = str(grounding.get("entity_type", "") or "").strip()
    source_ids = [int(item) for item in grounding.get("source_ids", []) if int(item) > 0]
    if entity_type not in ENTITY_TABLES or not source_ids:
        return {
            "status": "error",
            "error": "invalid_grounding_result",
            "grounding": grounding,
            "records": [],
        }

    store = get_qdrant_embedding_store()
    if not store.collection_exists(entity_type):
        return {
            "status": "error",
            "error": "entity_collection_missing",
            "grounding": grounding,
            "records": [],
        }

    selected_source_id = int(source_ids[0])
    points = store.scroll_payloads(entity_type, int(book_id))
    chapter_to_record: dict[int, dict[str, Any]] = {}
    for point in points:
        payload = dict(point.get("payload", {}) or {})
        if int(payload.get("source_id") or 0) != selected_source_id:
            continue
        chapter_index = _to_positive_int(payload.get("chapter_index"))
        record_text = _stringify_content(payload.get("record", ""))
        if not chapter_index or not record_text:
            continue
        if chapter_index not in chapter_to_record:
            chapter_to_record[chapter_index] = {
                "chapter_index": int(chapter_index),
                "record": record_text,
            }
    ordered_records = [chapter_to_record[key] for key in sorted(chapter_to_record)]
    if not ordered_records:
        return {
            "status": "error",
            "error": "no_records_for_entity",
            "grounding": grounding,
            "records": [],
        }

    safe_limit = max(1, int(limit))
    selected_records = ordered_records[:safe_limit] if normalized_edge == "first" else ordered_records[-safe_limit:]
    selected_match = list(grounding.get("selected", []) or [])
    matched_alias = ""
    matched_entity = ""
    if selected_match and isinstance(selected_match[0], dict):
        matched_alias = str(selected_match[0].get("matched_alias", "") or "").strip()
        matched_entity = str(selected_match[0].get("canonical_name", "") or "").strip()

    return {
        "status": "success",
        "edge": normalized_edge,
        "entity_type": entity_type,
        "source_id": selected_source_id,
        "matched_entity": matched_entity,
        "matched_alias": matched_alias,
        "record_count": len(selected_records),
        "total_record_count": len(ordered_records),
        "first_chapter_index": int(ordered_records[0]["chapter_index"]),
        "last_chapter_index": int(ordered_records[-1]["chapter_index"]),
        "selected_chapter_indices": [int(item["chapter_index"]) for item in selected_records],
        "records": selected_records,
        "grounding": {
            "applied": True,
            "entity_type": entity_type,
            "source_ids": source_ids,
            "reason": grounding.get("reason", ""),
        },
    }


def _tool_registry() -> dict[str, BaseTool]:
    return {
        EDGE_RECORD_TOOL_NAME: retrieve_entity_edge_records,
        "hybrid_retrieve_characters": hybrid_retrieve_characters,
        "hybrid_retrieve_origanizations": hybrid_retrieve_origanizations,
        "hybrid_retrieve_special_existences": hybrid_retrieve_special_existences,
        "hybrid_retrieve_world_rules": hybrid_retrieve_world_rules,
        "hybrid_retrieve_chapter_summaries": hybrid_retrieve_chapter_summaries,
        "hybrid_retrieve_plots": hybrid_retrieve_plots,
        "hybrid_retrieve_volumes": hybrid_retrieve_volumes,
        "retrieve_chapter_summaries": retrieve_chapter_summaries,
        "retrieve_chapters": retrieve_chapters,
        "retrieve_chapter_directory": retrieve_chapter_directory,
    }


def _invoke_tool(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    registry = _tool_registry()
    tool_obj = registry[tool_name]
    started_at = time.perf_counter()
    raw = tool_obj.invoke(args)
    elapsed_sec = time.perf_counter() - started_at
    return {
        "tool": tool_name,
        "args": dict(args),
        "elapsed_sec": round(elapsed_sec, 3),
        "raw": raw,
        "payload": _extract_json_object(raw),
    }


def _build_entry_args(
    tool_name: str,
    *,
    rewrites: dict[str, Any],
    user_query: str,
    novel_title: str,
    book_id: int,
    request_id: str,
    intent_type: str,
    grounding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    top_k = 6
    query = rewrites.get("plot_query") or rewrites.get("hybrid_query") or user_query
    grounding_payload = grounding or {}
    if tool_name == EDGE_RECORD_TOOL_NAME:
        entity_tables = []
        if grounding_payload.get("entity_type"):
            entity_tables = [str(grounding_payload.get("entity_type", ""))]
        elif intent_type in {"first_appearance", "ending_fate"}:
            if "组织" in str(user_query or ""):
                entity_tables = ["origanizations"]
            else:
                entity_tables = ["characters"]
        return {
            "entity_query": str(rewrites.get("entity_query") or user_query or "").strip(),
            "novel_title": str(novel_title or "").strip(),
            "book_id": int(book_id),
            "entity_type_hint": entity_tables[0] if entity_tables else "",
            "edge": "first" if intent_type == "first_appearance" else "last",
            "limit": 3,
            "request_id": request_id,
        }
    if tool_name in {
        "hybrid_retrieve_characters",
        "hybrid_retrieve_origanizations",
        "hybrid_retrieve_special_existences",
        "hybrid_retrieve_world_rules",
    }:
        query = rewrites.get("entity_query") or user_query
        top_k = 12 if intent_type == "first_appearance" else 8
    elif tool_name == "hybrid_retrieve_chapter_summaries":
        query = rewrites.get("chapter_query") or user_query
        top_k = 10 if intent_type == "first_appearance" else 8
    elif tool_name == "hybrid_retrieve_plots":
        query = rewrites.get("plot_query") or user_query
        top_k = 6
    elif tool_name == "hybrid_retrieve_volumes":
        query = rewrites.get("plot_query") or user_query
        top_k = 3
    args = {
        "query": str(query or "").strip(),
        "user_query": str(rewrites.get("normalized_question") or user_query or "").strip(),
        "novel_title": str(novel_title or "").strip(),
        "book_id": int(book_id),
        "top_k": top_k,
        "request_id": request_id,
    }
    if (
        tool_name in ENTITY_TOOL_BY_TYPE.values()
        and grounding_payload.get("applied")
        and _tool_to_entity_type(tool_name) == grounding_payload.get("entity_type")
        and intent_type in {"identity_ability", "existence_check", "first_appearance", "ending_fate"}
    ):
        args["source_ids"] = list(grounding_payload.get("source_ids", []) or [])
    return args


def _normalize_hybrid_payload(tool_name: str, payload: dict[str, Any]) -> list[EvidenceItem]:
    results = payload.get("results", []) if isinstance(payload.get("results"), list) else []
    source_name_map = {
        "hybrid_retrieve_characters": "characters",
        "hybrid_retrieve_origanizations": "origanizations",
        "hybrid_retrieve_special_existences": "special_existences",
        "hybrid_retrieve_world_rules": "world_rules",
        "hybrid_retrieve_chapter_summaries": "chapter_summaries",
        "hybrid_retrieve_plots": "plots",
        "hybrid_retrieve_volumes": "volumes",
    }
    source = source_name_map.get(tool_name, tool_name)
    evidence: list[EvidenceItem] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        evidence.append(
            EvidenceItem(
                source=source,
                source_name=str(item.get("name", "") or "").strip(),
                snippet=_stringify_content(
                    item.get("record")
                    or item.get("chapter_summary")
                    or item.get("plot_summary")
                    or item.get("volume_summary")
                    or ""
                ),
                score=_evidence_score(source, item),
                chapter_index=_to_positive_int(item.get("chapter_index")),
                plot_id=_to_positive_int(item.get("plot_id")),
                volume_index=_to_positive_int(item.get("volume_index")),
                start_chapter_index=_to_positive_int(item.get("start_chapter_index")),
                end_chapter_index=_to_positive_int(item.get("end_chapter_index")),
                raw=dict(item),
            )
        )
    return evidence


def _normalize_structured_result(tool_name: str, payload: dict[str, Any]) -> list[EvidenceItem]:
    hit_chapters = payload.get("hit_chapters", []) if isinstance(payload.get("hit_chapters"), list) else []
    best_evidence = _stringify_content(payload.get("best_evidence", ""))
    source = "chapters" if tool_name == "retrieve_chapters" else "chapter_summaries_verify"
    if not best_evidence:
        return []
    evidence: list[EvidenceItem] = []
    for chapter_index in hit_chapters:
        normalized = _to_positive_int(chapter_index)
        if not normalized:
            continue
        evidence.append(
            EvidenceItem(
                source=source,
                snippet=best_evidence,
                score=0.95 if source == "chapters" else 0.66,
                chapter_index=normalized,
                raw=dict(payload),
            )
        )
    return evidence


def _merge_evidence(items: list[EvidenceItem]) -> list[EvidenceItem]:
    merged: dict[tuple[Any, ...], EvidenceItem] = {}
    for item in items:
        key = (
            item.source,
            item.chapter_index,
            item.plot_id,
            item.volume_index,
            item.start_chapter_index,
            item.end_chapter_index,
            item.source_name,
            item.snippet[:120],
        )
        existing = merged.get(key)
        if existing is None:
            merged[key] = item
            continue
        merged_ids = tuple(sorted({*existing.subquery_ids, *item.subquery_ids}))
        if existing.score < item.score:
            item.subquery_ids = merged_ids
            merged[key] = item
        else:
            existing.subquery_ids = merged_ids
    return sorted(merged.values(), key=lambda item: item.score, reverse=True)


def _cluster_chapters(chapter_scores: dict[int, float]) -> list[tuple[list[int], float]]:
    ordered = sorted(chapter_scores)
    if not ordered:
        return []
    clusters: list[list[int]] = [[ordered[0]]]
    for chapter_index in ordered[1:]:
        if chapter_index - clusters[-1][-1] <= 2:
            clusters[-1].append(chapter_index)
        else:
            clusters.append([chapter_index])
    scored: list[tuple[list[int], float]] = []
    for cluster in clusters:
        scored.append((cluster, sum(chapter_scores[item] for item in cluster)))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


def _clip_window(start: int, end: int, *, limit: int = MAX_WINDOW_SIZE) -> tuple[int, int]:
    safe_start = max(1, int(start))
    safe_end = max(safe_start, int(end))
    if safe_end - safe_start + 1 <= limit:
        return safe_start, safe_end
    return safe_start, safe_start + limit - 1


def _fetch_plot_window(book_id: int, plot_id: int) -> tuple[int, int] | None:
    try:
        with _connect_mysql() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT start_chapter_index, end_chapter_index
                    FROM book_plots
                    WHERE book_id = %s AND plot_id = %s
                    LIMIT 1
                    """,
                    (int(book_id), int(plot_id)),
                )
                row = cursor.fetchone()
        if not row:
            return None
        start = _to_positive_int(row.get("start_chapter_index"))
        end = _to_positive_int(row.get("end_chapter_index"))
        if start and end:
            return start, end
    except Exception:
        logger.exception("Failed to fetch plot window: book_id=%s plot_id=%s", book_id, plot_id)
    return None


def _pick_candidate_window(
    *,
    book_id: int,
    intent_type: str,
    evidence_items: list[EvidenceItem],
) -> tuple[int, int] | None:
    chapter_scores: dict[int, float] = {}
    for item in evidence_items:
        if item.chapter_index:
            chapter_scores[item.chapter_index] = chapter_scores.get(item.chapter_index, 0.0) + float(item.score)
    if chapter_scores:
        if intent_type == "first_appearance":
            earliest = min(chapter_scores)
            return _clip_window(max(1, earliest - 1), earliest + 1, limit=4)
        if intent_type == "ending_fate":
            latest = max(chapter_scores)
            return _clip_window(max(1, latest - 1), latest + 1, limit=4)
        best_cluster = _cluster_chapters(chapter_scores)
        if best_cluster:
            chapters, _ = best_cluster[0]
            return _clip_window(min(chapters), max(chapters), limit=MAX_WINDOW_SIZE)

    plot_candidates = [
        item for item in evidence_items if item.start_chapter_index and item.end_chapter_index
    ]
    if plot_candidates:
        best_plot = max(plot_candidates, key=lambda item: item.score)
        return _clip_window(
            int(best_plot.start_chapter_index or 1),
            int(best_plot.end_chapter_index or best_plot.start_chapter_index or 1),
        )

    plot_ids = [item.plot_id for item in evidence_items if item.plot_id]
    for plot_id in plot_ids:
        window = _fetch_plot_window(book_id, int(plot_id))
        if window:
            return _clip_window(window[0], window[1])
    return None


def _refine_rewrites_with_evidence(
    *,
    rewrites: dict[str, Any],
    user_goal: str,
    evidence_items: list[EvidenceItem],
) -> dict[str, Any]:
    if not evidence_items:
        return dict(rewrites)

    normalized_question = _normalize_question_text(rewrites.get("normalized_question", user_goal))
    base_terms = _extract_refinement_terms(
        str(rewrites.get("entity_query") or rewrites.get("plot_query") or user_goal),
        limit=6,
    )
    anchor_term = max(base_terms or [normalized_question], key=lambda item: len(str(item or "").replace(" ", "")))
    evidence_terms: list[str] = []
    for item in evidence_items[:3]:
        if item.source_name and _theme_overlap_ratio(item.source_name, anchor_term) >= 0.34:
            evidence_terms.append(str(item.source_name).strip())
        for term in _extract_refinement_terms(_stringify_content(item.snippet), limit=3):
            if _theme_overlap_ratio(term, anchor_term) >= 0.34:
                evidence_terms.append(term)

    anchored_terms = _dedupe_terms(base_terms + evidence_terms, limit=8)
    if not anchored_terms:
        return dict(rewrites)

    focused_terms = anchored_terms[:4]
    refined = dict(rewrites)
    refined["entity_query"] = " ".join(_dedupe_terms(base_terms + focused_terms, limit=8)).strip()
    refined["plot_query"] = " ".join(_dedupe_terms(base_terms + anchored_terms, limit=10)).strip()
    refined["chapter_query"] = (
        f"{normalized_question.rstrip('？?。')} 请重点核验以下线索：{'、'.join(focused_terms)}。"
    )
    refined["fulltext_query"] = (
        f"{normalized_question.rstrip('？?。')} 请围绕以下线索提取原文依据并排除无关内容：{'、'.join(focused_terms)}。"
    )
    refined["rewrite_source"] = "evidence_refined"
    return refined


def _choose_supplemental_hybrid_tool(
    *,
    state: SubqueryState,
    evidence_items: list[EvidenceItem],
) -> str | None:
    if any(item.source == "chapters" for item in evidence_items):
        return None
    if not any(item.source in ENTITY_EVIDENCE_SOURCES for item in evidence_items):
        return None
    if state.intent_type in {"identity_ability", "causal_motivation", "timeline_evolution", "general_fact"}:
        if not any(item.source == "plots" for item in evidence_items):
            return "hybrid_retrieve_plots"
    if state.intent_type in {"existence_check", "general_fact"}:
        if not any(item.source == "chapter_summaries" for item in evidence_items):
            return "hybrid_retrieve_chapter_summaries"
    return None


def _should_skip_summary_verify(state: SubqueryState) -> bool:
    span = _window_span(state.candidate_window)
    if span <= 0:
        return False
    if state.intent_type in DIRECT_FULLTEXT_INTENTS and span <= 3:
        return True
    return False


def _needs_fulltext(
    *,
    intent_type: str,
    summary_payload: dict[str, Any] | None,
    confidence: float,
    user_query: str,
) -> bool:
    text = str(user_query or "")
    if intent_type in FULLTEXT_INTENTS or intent_type in DIRECT_FULLTEXT_INTENTS:
        return True
    if any(marker in text for marker in ("原文", "原话", "对白", "逐字")):
        return True
    # Chapter summaries are only coordinate hints; any candidate summary evidence
    # must be validated against chapter content before it can be trusted.
    if isinstance(summary_payload, dict):
        return True
    if intent_type in {"causal_motivation", "timeline_evolution"} and confidence < 0.82:
        return True
    return confidence < 0.72


def _build_citations(evidence_items: list[EvidenceItem]) -> list[str]:
    citations: list[str] = []
    for item in evidence_items:
        if item.chapter_index:
            token = f"Chapter {item.chapter_index}"
        elif item.plot_id:
            token = f"Plot {item.plot_id}"
        elif item.volume_index:
            token = f"Volume {item.volume_index}"
        else:
            continue
        if token not in citations:
            citations.append(token)
    return citations[:6]


@lru_cache(maxsize=1)
def _get_recovery_planner_llm() -> ChatOpenAI:
    _load_runtime_env()
    api_key = (
        os.getenv("RECOVERY_LLM_API_KEY", "").strip()
        or os.getenv("LLM_API_KEY", "").strip()
    )
    if not api_key:
        raise RuntimeError("Missing RECOVERY_LLM_API_KEY or LLM_API_KEY for recovery planner.")

    model_name = (
        os.getenv("RECOVERY_LLM_MODEL", "").strip()
        or os.getenv("LLM_MODEL", "deepseek-v3.2").strip()
        or "deepseek-v3.2"
    )
    base_url = (
        os.getenv("RECOVERY_LLM_BASE_URL", "").strip()
        or os.getenv("LLM_BASE_URL", "").strip()
    )
    timeout = float(os.getenv("RECOVERY_LLM_TIMEOUT_SECONDS", "20").strip() or 20.0)
    kwargs: dict[str, Any] = {
        "model": model_name,
        "api_key": api_key,
        "temperature": 0,
        "timeout": timeout,
    }
    if base_url:
        kwargs["base_url"] = base_url
    apply_llm_network_settings(kwargs)
    return wrap_tracked_llm(ChatOpenAI(**kwargs))


def _has_stable_chapter_evidence(evidence_items: list[EvidenceItem]) -> bool:
    return any(
        item.chapter_index and item.source == "chapters"
        for item in evidence_items
    )


def _is_subquery_resolved(intent_type: str, evidence_items: list[EvidenceItem], confidence: float) -> bool:
    if _has_stable_chapter_evidence(evidence_items):
        return True
    return False


def _drop_summary_evidence_for_window(
    evidence_items: list[EvidenceItem],
    *,
    subquery_id: str,
    window: tuple[int, int] | None,
) -> list[EvidenceItem]:
    if window is None:
        return evidence_items
    start, end = int(window[0]), int(window[1])
    filtered: list[EvidenceItem] = []
    for item in evidence_items:
        if (
            subquery_id in item.subquery_ids
            and item.source in SUMMARY_ONLY_SOURCES
            and item.chapter_index
            and start <= int(item.chapter_index) <= end
        ):
            continue
        filtered.append(item)
    return filtered


def _finalize_evidence_for_output(evidence_items: list[EvidenceItem]) -> list[EvidenceItem]:
    return _merge_evidence(
        [item for item in evidence_items if item.source not in SUMMARY_ONLY_SOURCES]
    )


def _has_conflicting_evidence(evidence_items: list[EvidenceItem]) -> bool:
    chapter_hits = sorted({int(item.chapter_index) for item in evidence_items if item.chapter_index})
    if len(chapter_hits) >= 3 and (chapter_hits[-1] - chapter_hits[0]) > 40:
        return True
    plot_hits = {int(item.plot_id) for item in evidence_items if item.plot_id}
    return len(plot_hits) >= 3 and not _has_stable_chapter_evidence(evidence_items)


def _summarize_tool_call_for_recovery(item: dict[str, Any]) -> dict[str, Any]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    results = payload.get("results", []) if isinstance(payload.get("results"), list) else []
    hit_chapters = payload.get("hit_chapters", []) if isinstance(payload.get("hit_chapters"), list) else []
    return {
        "tool": item.get("tool"),
        "args": item.get("args", {}),
        "elapsed_sec": item.get("elapsed_sec"),
        "results_count": len(results),
        "hit_chapters": hit_chapters[:6],
        "status": payload.get("status") if isinstance(payload, dict) else None,
    }


def _summarize_evidence_for_recovery(evidence_items: list[EvidenceItem]) -> list[dict[str, Any]]:
    summary = []
    for item in evidence_items[:6]:
        summary.append(
            {
                "source": item.source,
                "chapter_index": item.chapter_index,
                "plot_id": item.plot_id,
                "volume_index": item.volume_index,
                "source_name": item.source_name,
                "score": round(float(item.score), 3),
                "snippet": _stringify_content(item.snippet)[:220],
            }
        )
    return summary


def _plan_recovery_action(
    *,
    user_query: str,
    intent_type: str,
    rewrites: dict[str, Any],
    used_tools: list[dict[str, Any]],
    evidence_items: list[EvidenceItem],
    grounding: dict[str, Any],
    candidate_window: tuple[int, int] | None,
    recovery_policy: dict[str, Any],
    remaining_tool_budget: int,
    remaining_fulltext_budget: int,
) -> dict[str, Any] | None:
    allowed_tools = list(recovery_policy.get("allowed_tools", []) or [])
    llm_input = {
        "user_query": user_query,
        "intent_type": intent_type,
        "rewrites": {
            key: rewrites.get(key)
            for key in (
                "normalized_question",
                "entity_query",
                "plot_query",
                "chapter_query",
                "fulltext_query",
            )
        },
        "grounding": {
            "applied": grounding.get("applied", False),
            "entity_type": grounding.get("entity_type", ""),
            "source_ids": grounding.get("source_ids", []),
            "selected": list(grounding.get("selected", []) or [])[:3],
        },
        "candidate_window": list(candidate_window) if candidate_window else None,
        "used_tools": [_summarize_tool_call_for_recovery(item) for item in used_tools[-8:]],
        "evidence_summary": _summarize_evidence_for_recovery(evidence_items),
        "allowed_tools": allowed_tools,
        "remaining_tool_budget": remaining_tool_budget,
        "remaining_fulltext_budget": remaining_fulltext_budget,
    }
    llm = _get_recovery_planner_llm()
    response = llm.invoke(
        [
            SystemMessage(content=RECOVERY_PLANNER_PROMPT),
            HumanMessage(content=_json_dump(llm_input)),
        ]
    )
    payload = _extract_json_object(_stringify_content(getattr(response, "content", response)))
    return payload if isinstance(payload, dict) else None


def _default_recovery_args(
    *,
    tool_name: str,
    rewrites: dict[str, Any],
    user_query: str,
    novel_title: str,
    book_id: int,
    request_id: str,
    intent_type: str,
    candidate_window: tuple[int, int] | None,
    grounding: dict[str, Any],
) -> dict[str, Any]:
    args = _build_entry_args(
        tool_name,
        rewrites=rewrites,
        user_query=user_query,
        novel_title=novel_title,
        book_id=book_id,
        request_id=request_id,
        intent_type=intent_type,
        grounding=grounding,
    )
    if tool_name in {"retrieve_chapter_summaries", "retrieve_chapters", "retrieve_chapter_directory"}:
        start, end = candidate_window if candidate_window else (1, min(MAX_WINDOW_SIZE, 8))
        args = {
            "book_id": int(book_id),
            "start_chapter_index": int(start),
            "end_chapter_index": int(end),
            "intent": str(rewrites.get("chapter_query") or user_query),
        }
        if tool_name == "retrieve_chapters":
            args["intent"] = str(rewrites.get("fulltext_query") or user_query)
    return args


def _normalize_recovery_action(
    *,
    action: dict[str, Any] | None,
    rewrites: dict[str, Any],
    user_query: str,
    novel_title: str,
    book_id: int,
    request_id: str,
    intent_type: str,
    candidate_window: tuple[int, int] | None,
    grounding: dict[str, Any],
    recovery_policy: dict[str, Any],
) -> tuple[str | None, dict[str, Any], str]:
    if not isinstance(action, dict):
        return None, {}, "planner_returned_invalid_payload"
    tool_name = str(action.get("next_tool", "") or "").strip()
    if not tool_name:
        return None, {}, "planner_returned_empty_tool"
    allowed_tools = set(recovery_policy.get("allowed_tools", []) or [])
    if tool_name not in allowed_tools:
        return None, {}, f"planner_selected_disallowed_tool:{tool_name}"

    args = _default_recovery_args(
        tool_name=tool_name,
        rewrites=rewrites,
        user_query=user_query,
        novel_title=novel_title,
        book_id=book_id,
        request_id=request_id,
        intent_type=intent_type,
        candidate_window=candidate_window,
        grounding=grounding,
    )
    planner_args = action.get("args", {})
    if isinstance(planner_args, dict):
        for key, value in planner_args.items():
            if value is None:
                continue
            args[key] = value

    if tool_name in {"retrieve_chapter_summaries", "retrieve_chapters", "retrieve_chapter_directory"}:
        start = _to_positive_int(args.get("start_chapter_index"))
        end = _to_positive_int(args.get("end_chapter_index"))
        if not start or not end:
            return None, {}, f"planner_missing_window:{tool_name}"
        clipped_start, clipped_end = _clip_window(start, end)
        args["start_chapter_index"] = clipped_start
        args["end_chapter_index"] = clipped_end
        args["book_id"] = int(book_id)
        if tool_name == "retrieve_chapter_directory":
            args["intent"] = str(args.get("intent") or rewrites.get("chapter_query") or user_query)
    elif tool_name.startswith("hybrid_retrieve_"):
        args["book_id"] = int(book_id)
        args["novel_title"] = str(novel_title or "")
        args["request_id"] = request_id
        if tool_name in ENTITY_TOOL_BY_TYPE.values() and grounding.get("applied") and not args.get("source_ids"):
            if _tool_to_entity_type(tool_name) == grounding.get("entity_type"):
                args["source_ids"] = list(grounding.get("source_ids", []) or [])

    return tool_name, args, str(action.get("reason", "") or "")


def _normalize_directory_result(raw: str) -> list[EvidenceItem]:
    chapter_hits = [int(item) for item in re.findall(r"chapter_index\D+(\d+)", str(raw or ""), re.I)]
    evidence: list[EvidenceItem] = []
    for chapter_index in sorted(set(chapter_hits)):
        evidence.append(
            EvidenceItem(
                source="chapter_directory",
                snippet=_stringify_content(raw)[:320],
                score=0.7,
                chapter_index=chapter_index,
            )
        )
    return evidence


def _normalize_entity_edge_result(payload: dict[str, Any]) -> list[EvidenceItem]:
    records = payload.get("records", []) if isinstance(payload.get("records"), list) else []
    edge = str(payload.get("edge", "") or "").strip().lower()
    matched_entity = str(payload.get("matched_entity", "") or "").strip()
    selected = list(records)
    if edge == "last":
        ordered = list(reversed(selected))
    else:
        ordered = list(selected)
    evidence: list[EvidenceItem] = []
    base_score = 0.86
    for index, item in enumerate(ordered, start=1):
        if not isinstance(item, dict):
            continue
        chapter_index = _to_positive_int(item.get("chapter_index"))
        if not chapter_index:
            continue
        score = max(0.72, base_score - (index - 1) * 0.04)
        evidence.append(
            EvidenceItem(
                source=f"entity_edge_{edge or 'first'}",
                source_name=matched_entity,
                snippet=_stringify_content(item.get("record", "")),
                score=score,
                chapter_index=chapter_index,
                raw=dict(item),
            )
        )
    return evidence


def _normalize_tool_evidence(
    *,
    tool_name: str,
    raw: str,
    payload: dict[str, Any] | None,
) -> list[EvidenceItem]:
    if isinstance(payload, dict) and tool_name == EDGE_RECORD_TOOL_NAME:
        return _normalize_entity_edge_result(payload)
    if isinstance(payload, dict) and tool_name.startswith("hybrid_retrieve_"):
        return _normalize_hybrid_payload(tool_name, payload)
    if isinstance(payload, dict) and tool_name in {"retrieve_chapter_summaries", "retrieve_chapters"}:
        return _normalize_structured_result(tool_name, payload)
    if tool_name == "retrieve_chapter_directory":
        return _normalize_directory_result(raw)
    return []


def _tag_evidence_with_subquery_ids(items: list[EvidenceItem], subquery_ids: Sequence[str]) -> list[EvidenceItem]:
    normalized_ids = tuple(sorted({str(item).strip() for item in subquery_ids if str(item).strip()}))
    tagged: list[EvidenceItem] = []
    for item in items:
        item.subquery_ids = tuple(sorted({*item.subquery_ids, *normalized_ids}))
        tagged.append(item)
    return tagged


def _subquery_local_evidence(evidence_pool: list[EvidenceItem], subquery_id: str) -> list[EvidenceItem]:
    return [item for item in evidence_pool if subquery_id in item.subquery_ids]


def _candidate_validation_window(
    *,
    item: EvidenceItem,
    fallback_window: tuple[int, int] | None,
    intent_type: str,
) -> tuple[int, int] | None:
    if item.chapter_index:
        chapter = int(item.chapter_index)
        if intent_type in {"timeline_evolution", "causal_motivation"}:
            return _clip_window(max(1, chapter - 1), chapter + 1, limit=3)
        return _clip_window(chapter, chapter, limit=3)
    if item.start_chapter_index and item.end_chapter_index:
        start = int(item.start_chapter_index)
        end = int(item.end_chapter_index)
        return _clip_window(start, min(end, start + 2), limit=3)
    return fallback_window


def _build_confirmed_evidence_item(
    *,
    state: SubqueryState,
    chapter_index: int,
    source: str,
    source_name: str,
    claim: str,
    excerpt: str,
    confidence: float,
) -> ConfirmedEvidenceItem:
    return ConfirmedEvidenceItem(
        subquery_id=state.subquery_id,
        intent_type=state.intent_type,
        label=state.label,
        chapter_index=int(chapter_index),
        source=source,
        source_name=source_name,
        claim=_stringify_content(claim),
        excerpt=_stringify_content(excerpt),
        confidence=round(float(confidence), 3),
    )


def _validate_confirmed_evidence_for_subquery(
    *,
    state: SubqueryState,
    evidence_pool: list[EvidenceItem],
    novel_title: str,
    book_id: int,
    request_id: str,
    remaining_global_tool_budget: int,
    remaining_global_fulltext_budget: int,
) -> tuple[list[ConfirmedEvidenceItem], list[EvidenceItem], int, int, list[dict[str, Any]]]:
    del novel_title, request_id
    local_evidence = _subquery_local_evidence(evidence_pool, state.subquery_id)
    if not local_evidence:
        return [], evidence_pool, remaining_global_tool_budget, remaining_global_fulltext_budget, []

    rewrites = dict(state.plan.get("query_rewrites", {}) or {})
    confirmed: list[ConfirmedEvidenceItem] = []
    executed: list[dict[str, Any]] = []
    seen_chapters: set[int] = set()

    for item in local_evidence:
        if (
            item.source == "chapters"
            and item.chapter_index
            and _stringify_content(item.snippet)
            and not _looks_like_negative_evidence(item.snippet)
        ):
            chapter_num = int(item.chapter_index)
            if chapter_num in seen_chapters:
                continue
            chapter_content = _fetch_chapter_content(int(book_id), chapter_num)
            confirmed.append(
                _build_confirmed_evidence_item(
                    state=state,
                    chapter_index=chapter_num,
                    source=item.source,
                    source_name=item.source_name,
                    claim=item.snippet,
                    excerpt=chapter_content or item.snippet,
                    confidence=item.score,
                )
            )
            seen_chapters.add(chapter_num)
            if len(confirmed) >= _confirmed_evidence_limit(state.intent_type):
                return confirmed, evidence_pool, remaining_global_tool_budget, remaining_global_fulltext_budget, executed

    candidate_items = [
        item for item in local_evidence
        if item.source != "chapters"
    ]

    fallback_window = state.candidate_window
    for item in candidate_items:
        if remaining_global_tool_budget <= 0 or remaining_global_fulltext_budget <= 0:
            break
        window = _candidate_validation_window(
            item=item,
            fallback_window=fallback_window,
            intent_type=state.intent_type,
        )
        if window is None:
            continue
        window_start, window_end = int(window[0]), int(window[1])
        signature = (
            "retrieve_chapters",
            json.dumps(
                {
                    "book_id": int(book_id),
                    "start_chapter_index": window_start,
                    "end_chapter_index": window_end,
                    "intent": str(rewrites.get("fulltext_query") or state.user_goal),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        if any(
            (tool_item.get("tool"), json.dumps(tool_item.get("args", {}), ensure_ascii=False, sort_keys=True)) == signature
            for tool_item in state.used_tools or []
        ):
            continue

        invoked = _invoke_tool(
            "retrieve_chapters",
            {
                "book_id": int(book_id),
                "start_chapter_index": window_start,
                "end_chapter_index": window_end,
                "intent": str(rewrites.get("fulltext_query") or state.user_goal),
            },
        )
        executed.append(invoked)
        remaining_global_tool_budget -= 1
        remaining_global_fulltext_budget -= 1
        evidence_pool = _apply_tool_result_to_state(state=state, tool_result=invoked, evidence_pool=evidence_pool)
        evidence_pool = _drop_summary_evidence_for_window(
            evidence_pool,
            subquery_id=state.subquery_id,
            window=(window_start, window_end),
        )
        payload = invoked.get("payload") if isinstance(invoked.get("payload"), dict) else {}
        if str(payload.get("status", "") or "").upper() != "HIT":
            continue
        hit_chapters = [int(value) for value in payload.get("hit_chapters", []) if _to_positive_int(value)]
        best_excerpt = _stringify_content(payload.get("best_evidence", ""))
        if not hit_chapters or not best_excerpt or _looks_like_negative_evidence(best_excerpt):
            continue
        chapter_num = int(hit_chapters[0])
        if chapter_num in seen_chapters:
            continue
        chapter_content = _fetch_chapter_content(int(book_id), chapter_num)
        confirmed.append(
            _build_confirmed_evidence_item(
                state=state,
                chapter_index=chapter_num,
                source="chapters",
                source_name=item.source_name,
                claim=item.snippet,
                excerpt=chapter_content or best_excerpt,
                confidence=max(item.score, 0.9),
            )
        )
        seen_chapters.add(chapter_num)
        if len(confirmed) >= _confirmed_evidence_limit(state.intent_type):
            break

    return confirmed, evidence_pool, remaining_global_tool_budget, remaining_global_fulltext_budget, executed


def _subquery_state_snapshot(state: SubqueryState) -> dict[str, Any]:
    return {
        "subquery_id": state.subquery_id,
        "label": state.label,
        "user_goal": state.user_goal,
        "intent_type": state.intent_type,
        "priority": state.priority,
        "is_explicit": state.is_explicit,
        "status": state.status,
        "candidate_window": list(state.candidate_window) if state.candidate_window else None,
        "used_tools": [item.get("tool") for item in state.used_tools or []],
        "confidence": state.confidence,
        "recovery_used": state.recovery_used,
    }


def _coordinate_signature(evidence_items: list[EvidenceItem]) -> set[tuple[str, int]]:
    signature: set[tuple[str, int]] = set()
    for item in evidence_items:
        if item.chapter_index:
            signature.add(("chapter", int(item.chapter_index)))
        if item.plot_id:
            signature.add(("plot", int(item.plot_id)))
        if item.volume_index:
            signature.add(("volume", int(item.volume_index)))
    return signature


def _build_compact_search_packet(result: dict[str, Any]) -> dict[str, Any]:
    evidence_rows = result.get("evidence", []) if isinstance(result.get("evidence"), list) else []
    compact_evidence: list[dict[str, Any]] = []
    for item in evidence_rows[:4]:
        if not isinstance(item, dict):
            continue
        compact_evidence.append(
            {
                "source": item.get("source"),
                "chapter_index": item.get("chapter_index"),
                "plot_id": item.get("plot_id"),
                "volume_index": item.get("volume_index"),
                "source_name": item.get("source_name", ""),
                "snippet": _stringify_content(item.get("snippet", ""))[:320],
                "score": item.get("score"),
            }
        )

    confirmed_rows = result.get("confirmed_evidence", []) if isinstance(result.get("confirmed_evidence"), list) else []
    compact_confirmed: list[dict[str, Any]] = []
    for item in confirmed_rows[:6]:
        if not isinstance(item, dict):
            continue
        compact_confirmed.append(
            {
                "subquery_id": item.get("subquery_id"),
                "label": item.get("label", ""),
                "chapter_index": item.get("chapter_index"),
                "source_name": item.get("source_name", ""),
                "claim": _stringify_content(item.get("claim", "")),
                "excerpt": _stringify_content(item.get("excerpt", "")),
                "confidence": item.get("confidence"),
            }
        )

    return {
        "status": result.get("status"),
        "route_type": result.get("route_type"),
        "confidence": result.get("confidence"),
        "overall_summary": result.get("overall_summary", ""),
        "grounding": {
            "applied": bool(result.get("grounding", {}).get("applied")) if isinstance(result.get("grounding"), dict) else False,
            "entity_type": result.get("grounding", {}).get("entity_type", "") if isinstance(result.get("grounding"), dict) else "",
            "source_ids": list(result.get("grounding", {}).get("source_ids", []) or [])[:8] if isinstance(result.get("grounding"), dict) else [],
            "reason": result.get("grounding", {}).get("reason", "") if isinstance(result.get("grounding"), dict) else "",
            "selected": list(result.get("grounding", {}).get("selected", []) or [])[:3] if isinstance(result.get("grounding"), dict) else [],
        },
        "recovery_used": bool(result.get("recovery_used", False)),
        "subqueries": list(result.get("subqueries", []) or [])[:3],
        "answer": result.get("answer", ""),
        "draft_answer": result.get("draft_answer", ""),
        "citations": list(result.get("citations", []) or []),
        "chapter_hits": list(result.get("chapter_hits", []) or [])[:8],
        "plot_hits": list(result.get("plot_hits", []) or [])[:8],
        "volume_hits": list(result.get("volume_hits", []) or [])[:8],
        "evidence": compact_evidence,
        "confirmed_evidence": compact_confirmed,
        "elapsed_sec": result.get("elapsed_sec"),
        "request_id": result.get("request_id", ""),
        "book_id": result.get("book_id"),
        "query": result.get("query", ""),
        "log_path": result.get("log_path", ""),
        "project_log_path": result.get("project_log_path", ""),
    }


def _normalize_execution_policy(multi_plan: dict[str, Any]) -> dict[str, int | bool]:
    raw = multi_plan.get("execution_policy", {}) if isinstance(multi_plan.get("execution_policy"), dict) else {}
    subqueries = list(multi_plan.get("subqueries", []) or []) if isinstance(multi_plan.get("subqueries"), list) else []
    intents = [
        str(item.get("intent_type", "") or "general_fact").strip() or "general_fact"
        for item in subqueries
        if isinstance(item, dict)
    ]
    subquery_count = max(1, len(intents))

    intent_tool_budget = sum(BASE_TOOL_BUDGET_BY_INTENT.get(intent, BASE_TOOL_BUDGET_BY_INTENT["general_fact"]) for intent in intents)
    shared_discount = min(4, 2 * max(0, subquery_count - 1))
    recovery_reserve = min(3, subquery_count)
    baseline_tool_budget = BASELINE_TOOL_BUDGET_BY_SUBQUERY_COUNT.get(
        min(subquery_count, 3),
        BASELINE_TOOL_BUDGET_BY_SUBQUERY_COUNT[3],
    )
    recommended_tool_budget = max(
        baseline_tool_budget,
        min(MAX_GLOBAL_TOOL_CALLS, intent_tool_budget - shared_discount + recovery_reserve),
    )

    if any(intent in FULLTEXT_HEAVY_INTENTS for intent in intents):
        recommended_fulltext_windows = 3
    elif any(intent in FULLTEXT_MEDIUM_INTENTS for intent in intents):
        recommended_fulltext_windows = 2
    else:
        recommended_fulltext_windows = 2

    global_max_tool_calls = max(
        recommended_tool_budget,
        _clamp_int(raw.get("global_max_tool_calls"), MIN_GLOBAL_TOOL_CALLS, MAX_GLOBAL_TOOL_CALLS, 8),
    )
    global_max_fulltext_windows = max(
        recommended_fulltext_windows,
        _clamp_int(raw.get("global_max_fulltext_windows"), MIN_GLOBAL_FULLTEXT_WINDOWS, MAX_GLOBAL_FULLTEXT_WINDOWS, 2),
    )

    return {
        "global_max_tool_calls": min(MAX_GLOBAL_TOOL_CALLS, int(global_max_tool_calls)),
        "global_max_fulltext_windows": min(MAX_GLOBAL_FULLTEXT_WINDOWS, int(global_max_fulltext_windows)),
        "max_parallel_first_hop_calls": _clamp_int(raw.get("max_parallel_first_hop_calls"), MIN_PARALLEL_FIRST_HOP, MAX_PARALLEL_FIRST_HOP, 4),
        "per_subquery_verify_min": 1,
        "recovery_enabled": bool(raw.get("recovery_enabled", True)),
        "recovery_max_steps_global": _clamp_int(raw.get("recovery_max_steps_global"), 0, MAX_RECOVERY_STEPS_GLOBAL, 3),
        "recovery_max_steps_per_subquery": _clamp_int(raw.get("recovery_max_steps_per_subquery"), 0, MAX_RECOVERY_STEPS_PER_SUBQUERY, 2),
    }


def _build_subquery_section(
    state: SubqueryState,
    evidence_pool: list[EvidenceItem],
    confirmed_evidence_map: dict[str, list[ConfirmedEvidenceItem]] | None = None,
) -> dict[str, Any]:
    local_evidence = _subquery_local_evidence(evidence_pool, state.subquery_id)
    confirmed_items = list((confirmed_evidence_map or {}).get(state.subquery_id, []) or [])
    if confirmed_items:
        citations = [f"Chapter {item.chapter_index}" for item in confirmed_items[:6]]
        snippets = [item.excerpt for item in confirmed_items[:2] if _stringify_content(item.excerpt)]
        resolved = True
        confidence = max(state.confidence, max((item.confidence for item in confirmed_items), default=0.0))
    else:
        citations = _build_citations(local_evidence)
        snippets = [_stringify_content(item.snippet) for item in local_evidence[:2] if _stringify_content(item.snippet)]
        resolved = _is_subquery_resolved(state.intent_type, local_evidence, state.confidence)
        confidence = state.confidence
    if resolved and snippets:
        conclusion = snippets[0]
    else:
        conclusion = "证据不足，暂时无法稳定回答这一子问题。"
    return {
        "subquery_id": state.subquery_id,
        "label": state.label,
        "user_goal": state.user_goal,
        "intent_type": state.intent_type,
        "status": "resolved" if resolved else "unresolved",
        "confidence": round(float(confidence), 3),
        "citations": citations,
        "conclusion": conclusion,
        "evidence": snippets[:2],
        "recovery_used": state.recovery_used,
    }


def _build_overall_summary(subquery_sections: list[dict[str, Any]]) -> str:
    resolved = [item for item in subquery_sections if item.get("status") == "resolved"]
    unresolved = [item for item in subquery_sections if item.get("status") != "resolved"]
    if not subquery_sections:
        return "未找到可回答问题的稳定证据。"
    if not resolved:
        return "未能获得足够稳定的章节级证据来完整回答该问题。"
    labels = "、".join(str(item.get("label", "")) for item in resolved if str(item.get("label", "")))
    if unresolved:
        return f"已就{labels}获得阶段性结论，但仍有部分子问题证据不足。"
    return f"已完成对{labels}的检索与证据整合。"


def _build_multi_intent_answer(
    *,
    overall_summary: str,
    subquery_sections: list[dict[str, Any]],
) -> str:
    lines = [f"总述：{overall_summary}"]
    for section in subquery_sections:
        lines.append("")
        lines.append(f"{section.get('label', '问题')}：")
        lines.append(str(section.get("conclusion", "") or "证据不足，暂时无法稳定回答这一子问题。"))
        evidence = list(section.get("evidence", []) or [])
        if section.get("status") != "resolved" and evidence:
            lines.append(f"补充依据：{evidence[0]}")
        elif len(evidence) > 1:
            lines.append(f"补充依据：{evidence[1]}")
        citations = list(section.get("citations", []) or [])
        if citations:
            lines.append(f"定位：{', '.join(citations)}")
        if section.get("status") != "resolved":
            lines.append("状态：证据不足")
    return "\n".join(lines)


def _build_single_subquery_answer(section: dict[str, Any]) -> str:
    lines = [str(section.get("conclusion", "") or "证据不足，暂时无法稳定回答这一子问题。")]
    evidence = list(section.get("evidence", []) or [])
    if section.get("status") != "resolved" and evidence:
        lines.append(f"依据：{evidence[0]}")
    elif len(evidence) > 1:
        lines.append(f"依据：{evidence[1]}")
    citations = list(section.get("citations", []) or [])
    if citations:
        lines.append(f"定位：{', '.join(citations)}")
    if section.get("status") != "resolved":
        lines.append("状态：证据不足")
    return "\n".join(lines)


def _estimate_confidence(
    *,
    evidence_items: list[EvidenceItem],
    used_tools: list[dict[str, Any]],
) -> float:
    if not evidence_items:
        return 0.18
    sources = {item.source for item in evidence_items}
    confidence = min(0.95, max(item.score for item in evidence_items))
    if "chapters" in sources:
        confidence = max(confidence, 0.9)
    elif "chapter_summaries_verify" in sources:
        confidence = max(confidence, 0.68)
    elif "chapter_summaries" in sources:
        confidence = max(confidence, 0.6)
    elif "plots" in sources:
        confidence = max(confidence, 0.62)
    elif any(source in {"characters", "origanizations", "special_existences", "world_rules"} for source in sources):
        confidence = max(confidence, 0.55)
    if len(used_tools) >= 4 and confidence > 0.1:
        confidence = min(0.95, confidence + 0.03)
    return round(confidence, 3)


def _build_subquery_states(multi_plan: dict[str, Any]) -> list[SubqueryState]:
    states: list[SubqueryState] = []
    for item in list(multi_plan.get("subqueries", []) or []):
        if not isinstance(item, dict):
            continue
        plan = item.get("plan", {}) if isinstance(item.get("plan"), dict) else {}
        states.append(
            SubqueryState(
                subquery_id=str(item.get("subquery_id", "") or f"sq{len(states)+1}"),
                label=str(item.get("label", "") or f"问题{len(states)+1}"),
                user_goal=str(item.get("user_goal", "") or "").strip(),
                intent_type=str(item.get("intent_type", "general_fact") or "general_fact"),
                priority=int(item.get("priority", len(states) + 1) or len(states) + 1),
                is_explicit=bool(item.get("is_explicit", False)),
                plan=plan,
            )
        )
    return states


def _run_parallel_branch_first_hop(
    *,
    states: list[SubqueryState],
    novel_title: str,
    book_id: int,
    request_id: str,
    grounding: dict[str, Any],
    max_workers: int,
) -> list[tuple[str, dict[str, Any]]]:
    tasks: list[tuple[str, str, dict[str, Any]]] = []
    for state in sorted(states, key=lambda item: item.priority):
        entry_tools = list(state.plan.get("entry_tools", []) or [])[:2]
        rewrites = dict(state.plan.get("query_rewrites", {}) or {})
        for tool_name in entry_tools:
            args = _build_entry_args(
                tool_name,
                rewrites=rewrites,
                user_query=state.user_goal,
                novel_title=novel_title,
                book_id=book_id,
                request_id=request_id,
                intent_type=state.intent_type,
                grounding=grounding,
            )
            tasks.append((state.subquery_id, tool_name, args))

    if not tasks:
        return []

    with ThreadPoolExecutor(max_workers=min(max_workers, len(tasks))) as executor:
        futures = [
            executor.submit(_invoke_tool, tool_name, args)
            for _, tool_name, args in tasks
        ]
    return [(subquery_id, future.result()) for (subquery_id, _, _), future in zip(tasks, futures)]


def _legacy_first_hop_tools_for_subquery(state: SubqueryState) -> list[str]:
    entity_domains = list(state.plan.get("entity_domains", []) or [])
    if state.intent_type == "first_appearance":
        return ([ENTITY_TOOL_BY_TYPE.get(entity_domains[0], "hybrid_retrieve_characters")] if entity_domains else ["hybrid_retrieve_characters"]) + ["hybrid_retrieve_chapter_summaries"]
    if state.intent_type == "ending_fate":
        return ([ENTITY_TOOL_BY_TYPE.get(entity_domains[0], "hybrid_retrieve_characters")] if entity_domains else ["hybrid_retrieve_characters"]) + ["hybrid_retrieve_plots"]
    return []


def _apply_tool_result_to_state(
    *,
    state: SubqueryState,
    tool_result: dict[str, Any],
    evidence_pool: list[EvidenceItem],
) -> list[EvidenceItem]:
    state.used_tools.append(tool_result)
    payload = tool_result.get("payload") if isinstance(tool_result.get("payload"), dict) else None
    new_items = _normalize_tool_evidence(
        tool_name=str(tool_result.get("tool") or ""),
        raw=str(tool_result.get("raw", "") or ""),
        payload=payload,
    )
    if new_items:
        evidence_pool = _merge_evidence(
            evidence_pool + _tag_evidence_with_subquery_ids(new_items, [state.subquery_id])
        )
    return evidence_pool


def _update_subquery_window_and_confidence(
    *,
    state: SubqueryState,
    evidence_pool: list[EvidenceItem],
) -> None:
    local_evidence = _subquery_local_evidence(evidence_pool, state.subquery_id)
    state.candidate_window = _pick_candidate_window(
        book_id=int(local_evidence[0].raw.get("book_id") if local_evidence and isinstance(local_evidence[0].raw, dict) and local_evidence[0].raw.get("book_id") else 0) or 1,
        intent_type=state.intent_type,
        evidence_items=local_evidence,
    ) if local_evidence else None
    state.confidence = _estimate_confidence(evidence_items=local_evidence, used_tools=state.used_tools)
    state.status = "resolved" if _is_subquery_resolved(state.intent_type, local_evidence, state.confidence) else ("unresolved" if local_evidence else "pending")


def _update_subquery_window_and_confidence_for_book(
    *,
    state: SubqueryState,
    evidence_pool: list[EvidenceItem],
    book_id: int,
) -> None:
    local_evidence = _subquery_local_evidence(evidence_pool, state.subquery_id)
    state.candidate_window = _pick_candidate_window(
        book_id=book_id,
        intent_type=state.intent_type,
        evidence_items=local_evidence,
    ) if local_evidence else None
    state.confidence = _estimate_confidence(evidence_items=local_evidence, used_tools=state.used_tools)
    state.status = "resolved" if _is_subquery_resolved(state.intent_type, local_evidence, state.confidence) else ("unresolved" if local_evidence else "pending")


def _refresh_subquery_rewrites_from_evidence(
    *,
    state: SubqueryState,
    evidence_pool: list[EvidenceItem],
) -> None:
    local_evidence = _subquery_local_evidence(evidence_pool, state.subquery_id)
    if not local_evidence:
        return
    rewrites = dict(state.plan.get("query_rewrites", {}) or {})
    refined = _refine_rewrites_with_evidence(
        rewrites=rewrites,
        user_goal=state.user_goal,
        evidence_items=local_evidence,
    )
    if refined != rewrites:
        state.plan["query_rewrites"] = refined


def _should_enter_subquery_recovery(
    *,
    state: SubqueryState,
    evidence_pool: list[EvidenceItem],
    remaining_global_tool_budget: int,
    remaining_global_fulltext_budget: int,
) -> tuple[bool, str]:
    local_evidence = _subquery_local_evidence(evidence_pool, state.subquery_id)
    if _is_subquery_resolved(state.intent_type, local_evidence, state.confidence):
        return False, "already_resolved"
    if remaining_global_tool_budget <= 0:
        return False, "global_tool_budget_exhausted"
    if remaining_global_fulltext_budget < 0:
        return False, "global_fulltext_budget_exhausted"
    if not local_evidence:
        return True, "no_chapter_evidence"
    if state.confidence < RECOVERY_CONFIDENCE_THRESHOLD:
        return True, "low_confidence"
    if _has_conflicting_evidence(local_evidence):
        return True, "conflicting_evidence"
    return False, "not_needed"


def _run_subquery_recovery_mode(
    *,
    state: SubqueryState,
    evidence_pool: list[EvidenceItem],
    grounding: dict[str, Any],
    book_id: int,
    novel_title: str,
    request_id: str,
    max_steps: int,
    remaining_global_tool_budget: int,
    remaining_global_fulltext_budget: int,
) -> tuple[list[EvidenceItem], int, int, list[dict[str, Any]]]:
    executed_tools: list[dict[str, Any]] = []
    local_evidence = _subquery_local_evidence(evidence_pool, state.subquery_id)
    no_new_coordinates_streak = 0
    rewrites = dict(state.plan.get("query_rewrites", {}) or {})
    recovery_policy = dict(state.plan.get("recovery", {}) or {})

    for step_index in range(1, max_steps + 1):
        if remaining_global_tool_budget <= 0:
            state.recovery_trace.append({"step": step_index, "stopped": True, "reason": "global_budget_exhausted"})
            break
        local_evidence = _subquery_local_evidence(evidence_pool, state.subquery_id)
        if _is_subquery_resolved(state.intent_type, local_evidence, state.confidence):
            state.recovery_trace.append({"step": step_index, "stopped": True, "reason": "stable_chapter_evidence_present"})
            break

        planner_action = _plan_recovery_action(
            user_query=state.user_goal,
            intent_type=state.intent_type,
            rewrites=rewrites,
            used_tools=state.used_tools,
            evidence_items=local_evidence,
            grounding=grounding,
            candidate_window=state.candidate_window,
            recovery_policy=recovery_policy,
            remaining_tool_budget=remaining_global_tool_budget,
            remaining_fulltext_budget=remaining_global_fulltext_budget,
        )
        tool_name, args, planner_reason = _normalize_recovery_action(
            action=planner_action,
            rewrites=rewrites,
            user_query=state.user_goal,
            novel_title=novel_title,
            book_id=book_id,
            request_id=request_id,
            intent_type=state.intent_type,
            candidate_window=state.candidate_window,
            grounding=grounding,
            recovery_policy=recovery_policy,
        )
        if not tool_name:
            state.recovery_trace.append({"step": step_index, "planner_action": planner_action, "stopped": True, "reason": planner_reason})
            break

        signature = (tool_name, json.dumps(args, ensure_ascii=False, sort_keys=True))
        if any((item.get("tool"), json.dumps(item.get("args", {}), ensure_ascii=False, sort_keys=True)) == signature for item in state.used_tools):
            state.recovery_trace.append({"step": step_index, "planner_action": planner_action, "stopped": True, "reason": "repeated_same_tool_same_args"})
            break

        before_coordinates = _coordinate_signature(local_evidence)
        invoked = _invoke_tool(tool_name, args)
        executed_tools.append(invoked)
        remaining_global_tool_budget -= 1
        if tool_name == "retrieve_chapters":
            if remaining_global_fulltext_budget <= 0:
                state.recovery_trace.append({"step": step_index, "planner_action": planner_action, "stopped": True, "reason": "global_fulltext_budget_exhausted"})
                break
            remaining_global_fulltext_budget -= 1
        evidence_pool = _apply_tool_result_to_state(state=state, tool_result=invoked, evidence_pool=evidence_pool)
        if tool_name == "retrieve_chapters":
            evidence_pool = _drop_summary_evidence_for_window(
                evidence_pool,
                subquery_id=state.subquery_id,
                window=(
                    _to_positive_int(args.get("start_chapter_index")) or 1,
                    _to_positive_int(args.get("end_chapter_index"))
                    or _to_positive_int(args.get("start_chapter_index"))
                    or 1,
                ),
            )
        if (
            tool_name == "retrieve_chapter_summaries"
            and isinstance(invoked.get("payload"), dict)
            and str(invoked["payload"].get("status", "") or "").upper() == "HIT"
            and remaining_global_tool_budget > 0
            and remaining_global_fulltext_budget > 0
        ):
            forced_window = _window_from_hits(
                [int(item) for item in invoked["payload"].get("hit_chapters", []) if _to_positive_int(item)],
                state.candidate_window or (
                    _to_positive_int(args.get("start_chapter_index")) or 1,
                    _to_positive_int(args.get("end_chapter_index"))
                    or _to_positive_int(args.get("start_chapter_index"))
                    or 1,
                ),
            )
            forced_args = {
                "book_id": int(book_id),
                "start_chapter_index": int(forced_window[0]),
                "end_chapter_index": int(forced_window[1]),
                "intent": str(rewrites.get("fulltext_query") or state.user_goal),
            }
            forced_signature = ("retrieve_chapters", json.dumps(forced_args, ensure_ascii=False, sort_keys=True))
            if not any(
                (item.get("tool"), json.dumps(item.get("args", {}), ensure_ascii=False, sort_keys=True)) == forced_signature
                for item in state.used_tools + executed_tools
            ):
                forced_fulltext = _invoke_tool("retrieve_chapters", forced_args)
                executed_tools.append(forced_fulltext)
                remaining_global_tool_budget -= 1
                remaining_global_fulltext_budget -= 1
                evidence_pool = _apply_tool_result_to_state(state=state, tool_result=forced_fulltext, evidence_pool=evidence_pool)
                evidence_pool = _drop_summary_evidence_for_window(
                    evidence_pool,
                    subquery_id=state.subquery_id,
                    window=forced_window,
                )
        local_evidence = _subquery_local_evidence(evidence_pool, state.subquery_id)
        after_coordinates = _coordinate_signature(local_evidence)
        if after_coordinates == before_coordinates:
            no_new_coordinates_streak += 1
        else:
            no_new_coordinates_streak = 0
        _update_subquery_window_and_confidence_for_book(state=state, evidence_pool=evidence_pool, book_id=book_id)
        state.recovery_trace.append(
            {
                "step": step_index,
                "planner_action": planner_action,
                "tool": tool_name,
                "args": args,
                "planner_reason": planner_reason,
                "new_coordinates_found": after_coordinates != before_coordinates,
                "candidate_window": list(state.candidate_window) if state.candidate_window else None,
            }
        )
        if no_new_coordinates_streak >= 2:
            state.recovery_trace.append({"step": step_index, "stopped": True, "reason": "no_new_coordinates_twice"})
            break

    state.recovery_used = bool(state.recovery_trace)
    if state.status != "resolved":
        final_local_evidence = _subquery_local_evidence(evidence_pool, state.subquery_id)
        state.status = "resolved" if _is_subquery_resolved(state.intent_type, final_local_evidence, state.confidence) else "unresolved"
    return evidence_pool, remaining_global_tool_budget, remaining_global_fulltext_budget, executed_tools


def _window_from_hits(hit_chapters: list[int], fallback_window: tuple[int, int]) -> tuple[int, int]:
    normalized = [chapter for chapter in hit_chapters if _to_positive_int(chapter)]
    if not normalized:
        return fallback_window
    return _clip_window(min(normalized), max(normalized))


def build_agentic_research_graph() -> _SearchAgentGraph:
    return _SearchAgentGraph()


@lru_cache(maxsize=1)
def get_agentic_research_graph() -> _SearchAgentGraph:
    return build_agentic_research_graph()


def warm_search_runtime(book_ids: Sequence[int] = ()) -> None:
    try:
        warm_hybrid_runtime()
    except Exception:
        logger.exception("Failed to warm hybrid runtime.")
    try:
        warm_outline_cache(tuple(book_ids))
    except Exception:
        logger.exception("Failed to warm outline cache.")


def run_deep_research(
    *,
    book_id: int,
    query: str,
    request_id: str = "",
    novel_title: str = "",
) -> dict[str, Any]:
    started_at = time.perf_counter()
    log_context: dict[str, Any] = {
        "request_id": str(request_id or ""),
        "input_book_id": int(book_id or 0),
        "novel_title": str(novel_title or "").strip(),
        "query": str(query or "").strip(),
        "started_at_ms": int(time.time() * 1000),
    }
    normalized_query = str(query or "").strip()
    if not normalized_query:
        return _finalize_search_result(log_context, {
            "book_id": int(book_id or 0),
            "query": "",
            "request_id": str(request_id or ""),
            "answer": "query 不能为空。",
            "status": "error",
            "error": "empty_query",
            "messages": [],
        }, started_at)

    resolved_book_id = _to_positive_int(book_id) or resolve_book_id(novel_title)
    log_context["resolved_book_id"] = int(resolved_book_id or 0)
    if not resolved_book_id:
        return _finalize_search_result(log_context, {
            "book_id": 0,
            "query": normalized_query,
            "request_id": str(request_id or ""),
            "answer": "无法解析有效 book_id，检索终止。",
            "status": "error",
            "error": "invalid_book_id",
            "messages": [],
        }, started_at)

    multi_plan = plan_multi_search_route(normalized_query)
    execution_policy = _normalize_execution_policy(multi_plan)
    multi_plan["execution_policy"] = execution_policy
    subquery_states = _build_subquery_states(multi_plan)
    aggregate_entity_query = " ".join(
        str(state.plan.get("query_rewrites", {}).get("entity_query", "") or "").strip()
        for state in subquery_states
        if str(state.plan.get("query_rewrites", {}).get("entity_query", "") or "").strip()
    ).strip()
    grounding_tables = list(multi_plan.get("entity_grounding", {}).get("tables", []) or [])
    grounding = _collect_entity_grounding(
        book_id=int(resolved_book_id),
        user_query=normalized_query,
        entity_query=aggregate_entity_query or normalized_query,
        entity_tables=grounding_tables,
    )
    log_context["route_plan"] = multi_plan
    log_context["execution_policy"] = execution_policy
    log_context["decomposition"] = list(multi_plan.get("subqueries", []) or [])
    log_context["intent_type"] = "multi_intent" if len(subquery_states) > 1 else (subquery_states[0].intent_type if subquery_states else "general_fact")
    log_context["grounding"] = grounding
    used_tools: list[dict[str, Any]] = []
    all_evidence: list[EvidenceItem] = []
    fulltext_windows = 0
    recovery_used = False
    recovery_reason = ""
    recovery_trace: list[dict[str, Any]] = []
    summary_payloads: dict[str, dict[str, Any]] = {}

    try:
        max_parallel_first_hop = int(multi_plan.get("controller", {}).get("max_parallel_first_hop_calls", 4) or 4)
        max_parallel_first_hop = int(execution_policy["max_parallel_first_hop_calls"])
        global_tool_budget = int(execution_policy["global_max_tool_calls"])
        global_fulltext_budget = int(execution_policy["global_max_fulltext_windows"])
        per_subquery_verify_min = int(execution_policy["per_subquery_verify_min"])
        recovery_enabled = bool(execution_policy["recovery_enabled"])
        recovery_max_steps_global = int(execution_policy["recovery_max_steps_global"])
        recovery_max_steps_per_subquery = int(execution_policy["recovery_max_steps_per_subquery"])

        entry_results = _run_parallel_branch_first_hop(
            states=subquery_states,
            novel_title=novel_title,
            book_id=int(resolved_book_id),
            request_id=request_id,
            grounding=grounding,
            max_workers=max_parallel_first_hop,
        )
        for subquery_id, tool_result in entry_results:
            state = next((item for item in subquery_states if item.subquery_id == subquery_id), None)
            if state is None:
                continue
            used_tools.append(tool_result)
            all_evidence = _apply_tool_result_to_state(state=state, tool_result=tool_result, evidence_pool=all_evidence)
        global_tool_budget = max(0, global_tool_budget - len(used_tools))
        for state in subquery_states:
            _update_subquery_window_and_confidence_for_book(state=state, evidence_pool=all_evidence, book_id=int(resolved_book_id))
            _refresh_subquery_rewrites_from_evidence(state=state, evidence_pool=all_evidence)

        # Edge-record tool miss: fallback to legacy hybrid first-hop for that subquery.
        for state in sorted(subquery_states, key=lambda item: item.priority):
            entry_tools = list(state.plan.get("entry_tools", []) or [])
            if EDGE_RECORD_TOOL_NAME not in entry_tools:
                continue
            if _subquery_local_evidence(all_evidence, state.subquery_id):
                continue
            legacy_tools = _legacy_first_hop_tools_for_subquery(state)[:2]
            for tool_name in legacy_tools:
                if global_tool_budget <= 0:
                    break
                fallback_result = _invoke_tool(
                    tool_name,
                    _build_entry_args(
                        tool_name,
                        rewrites=dict(state.plan.get("query_rewrites", {}) or {}),
                        user_query=state.user_goal,
                        novel_title=novel_title,
                        book_id=int(resolved_book_id),
                        request_id=request_id,
                        intent_type=state.intent_type,
                        grounding=grounding,
                    ),
                )
                used_tools.append(fallback_result)
                all_evidence = _apply_tool_result_to_state(state=state, tool_result=fallback_result, evidence_pool=all_evidence)
                global_tool_budget -= 1
            _update_subquery_window_and_confidence_for_book(state=state, evidence_pool=all_evidence, book_id=int(resolved_book_id))
            _refresh_subquery_rewrites_from_evidence(state=state, evidence_pool=all_evidence)

        # Phase 1.5: if entity-like evidence is already strong, tighten query and do one
        # intent-aware hybrid follow-up before summary verification.
        for state in sorted(subquery_states, key=lambda item: item.priority):
            if global_tool_budget <= 0:
                break
            local_evidence = _subquery_local_evidence(all_evidence, state.subquery_id)
            if not local_evidence:
                continue
            _refresh_subquery_rewrites_from_evidence(state=state, evidence_pool=all_evidence)
            tool_name = _choose_supplemental_hybrid_tool(state=state, evidence_items=local_evidence)
            if not tool_name:
                continue
            if any(item.get("tool") == tool_name for item in state.used_tools or []):
                continue
            followup = _invoke_tool(
                tool_name,
                _build_entry_args(
                    tool_name,
                    rewrites=dict(state.plan.get("query_rewrites", {}) or {}),
                    user_query=state.user_goal,
                    novel_title=novel_title,
                    book_id=int(resolved_book_id),
                    request_id=request_id,
                    intent_type=state.intent_type,
                    grounding=grounding,
                ),
            )
            used_tools.append(followup)
            all_evidence = _apply_tool_result_to_state(state=state, tool_result=followup, evidence_pool=all_evidence)
            global_tool_budget -= 1
            _update_subquery_window_and_confidence_for_book(state=state, evidence_pool=all_evidence, book_id=int(resolved_book_id))
            _refresh_subquery_rewrites_from_evidence(state=state, evidence_pool=all_evidence)

        # Phase 2: every subquery gets one summary verification chance before any fulltext.
        for state in sorted(subquery_states, key=lambda item: item.priority):
            if global_tool_budget <= 0 or per_subquery_verify_min <= 0:
                if state.status != "resolved":
                    state.status = "unresolved"
                continue
            if state.candidate_window is None:
                if state.status != "resolved":
                    state.status = "unresolved"
                continue
            if _should_skip_summary_verify(state):
                if state.status != "resolved":
                    state.status = "unresolved"
                continue
            verify = _invoke_tool(
                "retrieve_chapter_summaries",
                {
                    "book_id": int(resolved_book_id),
                    "start_chapter_index": int(state.candidate_window[0]),
                    "end_chapter_index": int(state.candidate_window[1]),
                    "intent": str(state.plan.get("query_rewrites", {}).get("chapter_query") or state.user_goal),
                },
            )
            used_tools.append(verify)
            all_evidence = _apply_tool_result_to_state(state=state, tool_result=verify, evidence_pool=all_evidence)
            if isinstance(verify.get("payload"), dict):
                summary_payloads[state.subquery_id] = dict(verify.get("payload") or {})
            global_tool_budget -= 1
            _update_subquery_window_and_confidence_for_book(state=state, evidence_pool=all_evidence, book_id=int(resolved_book_id))
            if state.status != "resolved":
                state.status = "unresolved"

        # Phase 3: only unresolved subqueries may consume fulltext budget.
        for state in sorted(subquery_states, key=lambda item: item.priority):
            if state.status == "resolved":
                continue
            if global_tool_budget <= 0 or fulltext_windows >= global_fulltext_budget:
                break
            if state.candidate_window is None:
                continue
            local_evidence = _subquery_local_evidence(all_evidence, state.subquery_id)
            if not _needs_fulltext(
                intent_type=state.intent_type,
                summary_payload=summary_payloads.get(state.subquery_id),
                confidence=state.confidence,
                user_query=state.user_goal,
            ):
                continue
            fulltext_window = state.candidate_window
            payload = summary_payloads.get(state.subquery_id)
            if isinstance(payload, dict):
                fulltext_window = _window_from_hits(
                    [int(item) for item in payload.get("hit_chapters", []) if _to_positive_int(item)],
                    state.candidate_window,
                )
            chapter_read = _invoke_tool(
                "retrieve_chapters",
                {
                    "book_id": int(resolved_book_id),
                    "start_chapter_index": int(fulltext_window[0]),
                    "end_chapter_index": int(fulltext_window[1]),
                    "intent": str(state.plan.get("query_rewrites", {}).get("fulltext_query") or state.user_goal),
                },
            )
            used_tools.append(chapter_read)
            all_evidence = _apply_tool_result_to_state(state=state, tool_result=chapter_read, evidence_pool=all_evidence)
            all_evidence = _drop_summary_evidence_for_window(
                all_evidence,
                subquery_id=state.subquery_id,
                window=fulltext_window,
            )
            global_tool_budget -= 1
            fulltext_windows += 1
            _update_subquery_window_and_confidence_for_book(state=state, evidence_pool=all_evidence, book_id=int(resolved_book_id))
            if state.status != "resolved":
                state.status = "unresolved"

        # Phase 4: guarded recovery only for unresolved subqueries, explicit first.
        remaining_recovery_steps = recovery_max_steps_global
        for state in sorted(subquery_states, key=lambda item: (not item.is_explicit, item.priority)):
            if not recovery_enabled:
                break
            remaining_fulltext_budget = max(0, global_fulltext_budget - fulltext_windows)
            should_recover, reason = _should_enter_subquery_recovery(
                state=state,
                evidence_pool=all_evidence,
                remaining_global_tool_budget=global_tool_budget,
                remaining_global_fulltext_budget=remaining_fulltext_budget,
            )
            if not should_recover or remaining_recovery_steps <= 0:
                continue
            recovery_used = True
            recovery_reason = reason
            max_steps = min(recovery_max_steps_per_subquery, remaining_recovery_steps)
            all_evidence, global_tool_budget, remaining_fulltext_budget, executed = _run_subquery_recovery_mode(
                state=state,
                evidence_pool=all_evidence,
                grounding=grounding,
                book_id=int(resolved_book_id),
                novel_title=novel_title,
                request_id=request_id,
                max_steps=max_steps,
                remaining_global_tool_budget=global_tool_budget,
                remaining_global_fulltext_budget=remaining_fulltext_budget,
            )
            used_tools.extend(executed)
            fulltext_windows = global_fulltext_budget - remaining_fulltext_budget
            remaining_recovery_steps -= sum(1 for item in executed if item.get("tool"))

        confirmed_evidence_map: dict[str, list[ConfirmedEvidenceItem]] = {}
        remaining_confirm_tool_budget = global_tool_budget
        remaining_confirm_fulltext_budget = max(0, global_fulltext_budget - fulltext_windows)
        for state in sorted(subquery_states, key=lambda item: item.priority):
            confirmed_items, all_evidence, remaining_confirm_tool_budget, remaining_confirm_fulltext_budget, executed = _validate_confirmed_evidence_for_subquery(
                state=state,
                evidence_pool=all_evidence,
                novel_title=novel_title,
                book_id=int(resolved_book_id),
                request_id=request_id,
                remaining_global_tool_budget=remaining_confirm_tool_budget,
                remaining_global_fulltext_budget=remaining_confirm_fulltext_budget,
            )
            if confirmed_items:
                confirmed_evidence_map[state.subquery_id] = confirmed_items
            used_tools.extend(executed)
        global_tool_budget = remaining_confirm_tool_budget
        fulltext_windows = global_fulltext_budget - remaining_confirm_fulltext_budget

        final_evidence = _finalize_evidence_for_output(all_evidence)
        for state in subquery_states:
            _update_subquery_window_and_confidence_for_book(
                state=state,
                evidence_pool=final_evidence,
                book_id=int(resolved_book_id),
            )
            if confirmed_evidence_map.get(state.subquery_id):
                state.status = "resolved"
                state.confidence = max(
                    state.confidence,
                    max((item.confidence for item in confirmed_evidence_map[state.subquery_id]), default=state.confidence),
                )
        shared_citations = _build_citations(final_evidence)
        overall_confidence = _estimate_confidence(evidence_items=final_evidence, used_tools=used_tools)
        subquery_sections = [
            _build_subquery_section(state, final_evidence, confirmed_evidence_map)
            for state in sorted(subquery_states, key=lambda item: item.priority)
        ]
        overall_summary = _build_overall_summary(subquery_sections)
        if len(subquery_sections) == 1:
            rendered_answer = _build_single_subquery_answer(subquery_sections[0])
        else:
            rendered_answer = _build_multi_intent_answer(
                overall_summary=overall_summary,
                subquery_sections=subquery_sections,
            )
        route_type = "multi_intent" if len(subquery_states) > 1 else (subquery_states[0].intent_type if subquery_states else "general_fact")
        resolved_count = sum(1 for item in subquery_sections if item.get("status") == "resolved")
        unresolved_count = len(subquery_sections) - resolved_count
        recovery_trace_by_subquery = {
            state.subquery_id: state.recovery_trace
            for state in subquery_states
            if state.recovery_trace
        }
        recovery_trace = [
            {"subquery_id": state.subquery_id, "trace": state.recovery_trace}
            for state in subquery_states
            if state.recovery_trace
        ]
        status = "success" if resolved_count > 0 else "no_evidence"
        log_context["subquery_states"] = [_subquery_state_snapshot(state) for state in subquery_states]
        log_context["shared_evidence_pool_summary"] = _summarize_evidence_for_recovery(final_evidence)
        log_context["fulltext_windows"] = fulltext_windows
        log_context["tool_call_count"] = len(used_tools)
        log_context["confidence"] = overall_confidence
        log_context["recovery_used"] = recovery_used
        log_context["recovery_reason"] = recovery_reason
        log_context["recovery_trace"] = recovery_trace
        log_context["grounding"] = grounding
        return _finalize_search_result(log_context, {
            "book_id": int(resolved_book_id),
            "query": normalized_query,
            "request_id": str(request_id or ""),
            "status": status,
            "route_type": route_type,
            "route_plan": multi_plan,
            "decomposition": list(multi_plan.get("subqueries", []) or []),
            "rewrites": {
                item.subquery_id: item.plan.get("query_rewrites", {})
                for item in subquery_states
            },
            "grounding": grounding,
            "tool_calls": used_tools,
            "evidence": [asdict(item) for item in final_evidence[:8]],
            "confirmed_evidence": [
                asdict(item)
                for subquery_id in sorted(confirmed_evidence_map)
                for item in confirmed_evidence_map[subquery_id]
            ],
            "chapter_hits": [item.chapter_index for item in final_evidence if item.chapter_index][:8],
            "plot_hits": [item.plot_id for item in final_evidence if item.plot_id][:8],
            "volume_hits": [item.volume_index for item in final_evidence if item.volume_index][:8],
            "subqueries": subquery_sections,
            "subquery_states": [_subquery_state_snapshot(state) for state in subquery_states],
            "shared_evidence_pool_summary": _summarize_evidence_for_recovery(final_evidence),
            "overall_summary": overall_summary,
            "resolved_subquery_count": resolved_count,
            "unresolved_subquery_count": unresolved_count,
            "confidence": overall_confidence,
            "answer": rendered_answer,
            "draft_answer": overall_summary,
            "citations": shared_citations,
            "recovery_used": recovery_used,
            "recovery_reason": recovery_reason,
            "recovery_trace": recovery_trace,
            "recovery_trace_by_subquery": recovery_trace_by_subquery,
            "messages": [],
        }, started_at)
    except Exception as exc:
        logger.exception("Search agent failed: book_id=%s request_id=%s", resolved_book_id, request_id)
        log_context["tool_call_count"] = len(used_tools)
        log_context["fulltext_windows"] = fulltext_windows
        log_context["recovery_used"] = recovery_used
        log_context["recovery_reason"] = recovery_reason
        log_context["recovery_trace"] = recovery_trace
        return _finalize_search_result(log_context, {
            "book_id": int(resolved_book_id),
            "query": normalized_query,
            "request_id": str(request_id or ""),
            "status": "error",
            "error": str(exc),
            "answer": "检索执行失败，请稍后重试。",
            "grounding": grounding,
            "recovery_used": recovery_used,
            "recovery_reason": recovery_reason,
            "recovery_trace": recovery_trace,
            "tool_calls": used_tools,
            "messages": [],
        }, started_at)


@tool("contentSearch")
def contentSearch(query: str, novel_title: str = "", book_id: int = 0, request_id: str = "") -> str:
    """
    小说内容检索主入口。
    内部执行 route skill + query rewrite + hybrid/entity retrieval + chapter verification，
    最终返回结构化 search packet，供 chat agent 进行上下文融合和最终回答。
    """
    result = run_deep_research(
        book_id=int(book_id or 0),
        query=query,
        request_id=request_id,
        novel_title=novel_title,
    )
    return json.dumps(_build_compact_search_packet(result), ensure_ascii=False)


if __name__ == "__main__":
    sample = run_deep_research(book_id=1, query="杨间为什么叫杨戬")
    print(json.dumps(sample, ensure_ascii=False, indent=2))
