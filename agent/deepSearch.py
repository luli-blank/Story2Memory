from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import pymysql
from dotenv import load_dotenv
from langchain_core.messages import SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from pymysql.cursors import DictCursor

from agent.graph import apply_llm_network_settings, get_active_request_id, set_active_request_id, wrap_tracked_llm
from agent.prompt import COMPRESSION_PROMPT, DIRECTORY_COMPRESSION_PROMPT

logger = logging.getLogger(__name__)
ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_OVERRIDE_VAR = "STORY2MEMORY_ENV_OVERRIDE"

_OUTLINE_CACHE_LOCK = threading.Lock()
_VOLUME_ROWS_CACHE: dict[int, tuple[float, list[dict[str, Any]]]] = {}
_PLOT_ROWS_CACHE: dict[int, tuple[float, list[dict[str, Any]]]] = {}
CHAPTER_COVERAGE_KEYS = (
    "process",
    "title_source",
    "identity_definition",
    "first_appearance",
    "chapter_number",
    "ability",
    "causal_actor",
    "ending_fate",
)

CHAPTER_STRUCTURED_COMPRESSION_PROMPT = """
# Role
你是一个只返回严格 JSON 的章节证据聚合子引擎。你的任务不是写长摘要，而是为主控系统提供当前这一批章节里是否命中，以及命中了哪些章节。

# Inputs
- 目标意图：【{intent}】
- 数据层级：【{data_name}】
- 原始数据：{raw_data}

# Rules
1. 只根据 raw_data 判断，严禁编造。
2. 只输出 JSON，不要 markdown，不要解释，不要思考过程。
3. `hit_chapters` 只能填写当前批次 raw_data 中真实出现且与你判断相关的 chapter_index。
4. `best_evidence` 只写最关键的一条证据，控制在 220 字以内。
5. `needs_fulltext` 的含义：
   - 如果当前数据是章节摘要，且你认为还需要读取原文才能稳定回答，就填 true。
   - 如果当前数据已经是章节原文，通常填 false。
6. `coverage` 布尔字段含义：
   - `process`: 是否覆盖“过程/如何成为/怎么变成/演变链条”类信息。
   - `title_source`: 是否覆盖“称号来源/名字来源/谁改名/为何得名”类信息。
   - `identity_definition`: 是否覆盖“是什么/身份定义/本质是什么”类信息。
   - `first_appearance`: 是否覆盖“第一次出场/首次出现”类信息。
   - `chapter_number`: 是否覆盖“哪一章/第几章/章节编号”类信息。
   - `ability`: 是否覆盖“能力/手段/本领/技能”类信息。
   - `causal_actor`: 是否覆盖“是谁导致/是谁造成/责任主体”类信息。
   - `ending_fate`: 是否覆盖“结局/下场/命运/最终如何”类信息。
   若该批次没有覆盖，就填 false。

# Output JSON Schema
{{
  "status": "HIT",
  "hit_chapters": [1602, 1603],
  "best_evidence": "一句最关键证据",
  "needs_fulltext": true,
  "coverage": {{
    "process": false,
    "title_source": true,
    "identity_definition": false,
    "first_appearance": false,
    "chapter_number": false,
    "ability": false,
    "causal_actor": false,
    "ending_fate": false
  }},
  "reason": "一句话说明判断依据"
}}
"""


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
        "cursorclass": DictCursor,
    }


def _connect_mysql():
    _load_runtime_env()
    dsn = os.getenv("MYSQL_DSN", "").strip()
    cfg = _parse_mysql_dsn(dsn)
    if not cfg:
        raise RuntimeError("Missing or invalid MYSQL_DSN environment variable.")
    return pymysql.connect(**cfg)


def _int_bounds(rows: list[dict[str, Any]], key: str) -> tuple[int | None, int | None]:
    values: list[int] = []
    for row in rows:
        value = row.get(key)
        try:
            values.append(int(value))
        except (TypeError, ValueError):
            continue
    if not values:
        return None, None
    return min(values), max(values)


def to_positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def resolve_book_id(novel_title: str) -> int | None:
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
    except Exception as e:
        logger.error("[Tool Error] resolve_book_id by title failed: %s", e)
        return None


def _build_llm() -> ChatOpenAI:
    _load_runtime_env()

    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        raise ValueError("Missing LLM_API_KEY environment variable.")

    model_name = os.getenv("LLM_MODEL", "deepseek-v3.2").strip() or "deepseek-v3.2"
    base_url = os.getenv("LLM_BASE_URL", "").strip()

    kwargs: dict[str, Any] = {
        "model": model_name,
        "api_key": api_key,
        "temperature": 0,
    }
    if base_url:
        kwargs["base_url"] = base_url
    apply_llm_network_settings(kwargs)

    return wrap_tracked_llm(ChatOpenAI(**kwargs))


@lru_cache(maxsize=1)
def _get_llm() -> ChatOpenAI:
    return _build_llm()


def _outline_cache_ttl_seconds() -> int:
    return max(60, int(os.getenv("DEEPSEARCH_OUTLINE_CACHE_TTL_SECONDS", "1800").strip() or 1800))


def _clone_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _get_cached_rows(
    cache: dict[int, tuple[float, list[dict[str, Any]]]],
    book_id: int,
    loader,
) -> list[dict[str, Any]]:
    normalized_book_id = int(book_id)
    now = time.time()
    ttl = _outline_cache_ttl_seconds()
    with _OUTLINE_CACHE_LOCK:
        cached = cache.get(normalized_book_id)
        if cached and (now - cached[0]) < ttl:
            return _clone_rows(cached[1])

    rows = loader(normalized_book_id)
    with _OUTLINE_CACHE_LOCK:
        cache[normalized_book_id] = (time.time(), _clone_rows(rows))
    return _clone_rows(rows)


def _load_book_volume_rows(book_id: int) -> list[dict[str, Any]]:
    with _connect_mysql() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT volume_index, title, start_plot_index, end_plot_index, volume_summary, time_span
                FROM book_volumes
                WHERE book_id = %s
                ORDER BY volume_index ASC
                """,
                (book_id,),
            )
            return list(cursor.fetchall() or [])


def _load_book_plot_rows(book_id: int) -> list[dict[str, Any]]:
    with _connect_mysql() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    plot_id,
                    title,
                    start_chapter_index,
                    end_chapter_index,
                    plot_summary,
                    special_existence,
                    origanizations,
                    world_rules
                FROM book_plots
                WHERE book_id = %s
                ORDER BY plot_id ASC
                """,
                (book_id,),
            )
            return list(cursor.fetchall() or [])


def _get_cached_volume_rows(book_id: int) -> list[dict[str, Any]]:
    return _get_cached_rows(_VOLUME_ROWS_CACHE, book_id, _load_book_volume_rows)


def _get_cached_plot_rows(book_id: int) -> list[dict[str, Any]]:
    return _get_cached_rows(_PLOT_ROWS_CACHE, book_id, _load_book_plot_rows)


def warm_outline_cache(book_ids: list[int] | tuple[int, ...]) -> None:
    for raw_book_id in book_ids:
        book_id = to_positive_int(raw_book_id)
        if not book_id:
            continue
        try:
            _get_cached_volume_rows(book_id)
            _get_cached_plot_rows(book_id)
        except Exception as exc:
            logger.warning("[Prewarm] outline cache warm failed for book_id=%s: %s", book_id, exc)


def _compress_context(
    intent: str,
    data_name: str,
    raw_data: str,
    custom_prompt: str | None = None,
    request_id: str = "",
) -> str:
    if request_id:
        set_active_request_id(request_id)
    llm = _get_llm()
    prompt_template = custom_prompt or COMPRESSION_PROMPT
    prompt = prompt_template.format(intent=intent, data_name=data_name, raw_data=raw_data)
    try:
        logger.info(f"[Compress] Filtering {data_name} for intent: {intent}")
        response = llm.invoke([SystemMessage(content=prompt)])
        return response.content
    except Exception as e:
        logger.error(f"[Compress Error] Context compression failed: {e}")
        return f"信息提取失败，返回部分原始数据截断：{raw_data[:1000]}..."


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


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "是"}


def _normalize_hit_chapters(value: Any) -> list[int]:
    chapters: list[int] = []
    seen: set[int] = set()
    raw_values = value if isinstance(value, list) else []
    for item in raw_values:
        try:
            chapter_index = int(item)
        except (TypeError, ValueError):
            continue
        if chapter_index in seen:
            continue
        seen.add(chapter_index)
        chapters.append(chapter_index)
    return sorted(chapters)


def _normalize_coverage(value: Any) -> dict[str, bool]:
    payload = value if isinstance(value, dict) else {}
    return {
        key: _to_bool(payload.get(key, False))
        for key in CHAPTER_COVERAGE_KEYS
    }


def _fallback_hit_chapters_from_text(raw: str) -> list[int]:
    hits = [int(match) for match in re.findall(r"Chapter\s*\[?\s*(\d+)", str(raw or ""))]
    deduped: list[int] = []
    seen: set[int] = set()
    for chapter_index in hits:
        if chapter_index in seen:
            continue
        seen.add(chapter_index)
        deduped.append(chapter_index)
    return sorted(deduped)


def _normalize_structured_chapter_batch_result(
    raw: str,
    *,
    default_needs_fulltext: bool,
) -> dict[str, Any]:
    payload = _extract_json_object(raw)
    if isinstance(payload, dict):
        hit_chapters = _normalize_hit_chapters(payload.get("hit_chapters", []))
        status = str(payload.get("status", "") or "").strip().upper()
        if status not in {"HIT", "MISS"}:
            status = "HIT" if hit_chapters else "MISS"
        best_evidence = str(payload.get("best_evidence", "") or "").strip()
        reason = str(payload.get("reason", "") or "").strip()
        return {
            "status": status,
            "hit_chapters": hit_chapters,
            "best_evidence": best_evidence,
            "needs_fulltext": _to_bool(payload.get("needs_fulltext", default_needs_fulltext)),
            "coverage": _normalize_coverage(payload.get("coverage")),
            "reason": reason,
        }

    hit_chapters = _fallback_hit_chapters_from_text(raw)
    normalized_raw = str(raw or "").strip()
    return {
        "status": "HIT" if hit_chapters else "MISS",
        "hit_chapters": hit_chapters,
        "best_evidence": normalized_raw[:220],
        "needs_fulltext": default_needs_fulltext and bool(hit_chapters),
        "coverage": {key: False for key in CHAPTER_COVERAGE_KEYS},
        "reason": normalized_raw[:220],
    }


def _select_best_evidence(batch_results: list[dict[str, Any]]) -> str:
    candidates = [
        str(item.get("best_evidence", "") or "").strip()
        for item in batch_results
        if str(item.get("best_evidence", "") or "").strip()
    ]
    if not candidates:
        return ""
    return max(candidates, key=len)


def _aggregate_structured_chapter_results(
    *,
    data_name: str,
    requested_start: int,
    requested_end: int,
    batch_results: list[dict[str, Any]],
) -> str:
    hit_chapters: list[int] = []
    seen: set[int] = set()
    coverage = {key: False for key in CHAPTER_COVERAGE_KEYS}
    hit_batches = 0

    for item in batch_results:
        if str(item.get("status", "") or "").upper() == "HIT":
            hit_batches += 1
        for chapter_index in item.get("hit_chapters", []) or []:
            try:
                normalized = int(chapter_index)
            except (TypeError, ValueError):
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            hit_chapters.append(normalized)
        item_coverage = item.get("coverage", {}) or {}
        for key in CHAPTER_COVERAGE_KEYS:
            coverage[key] = coverage[key] or _to_bool(item_coverage.get(key, False))

    hit_chapters.sort()
    status = "HIT" if hit_chapters else "MISS"
    result = {
        "status": status,
        "data_name": data_name,
        "requested_range": {
            "start_chapter_index": int(requested_start),
            "end_chapter_index": int(requested_end),
        },
        "hit_chapters": hit_chapters,
        "hit_count": len(hit_chapters),
        "best_evidence": _select_best_evidence(batch_results),
        "needs_fulltext": any(_to_bool(item.get("needs_fulltext", False)) for item in batch_results),
        "coverage": coverage,
        "matched_batches": hit_batches,
        "reason": "",
    }
    if status == "MISS":
        result["reason"] = f"未在本次检索的【{data_name}】中发现可稳定回答问题的章节证据。"
    return json.dumps(result, ensure_ascii=False)


def _compress_context_in_batches(
    intent: str,
    data_name: str,
    rows: list[dict[str, Any]],
    batch_size: int,
    request_id: str = "",
) -> str:
    active_request_id = request_id or get_active_request_id()
    if len(rows) <= batch_size:
        raw_json = json.dumps(rows, ensure_ascii=False)
        return _compress_context(intent, data_name, raw_json, request_id=active_request_id)

    batch_payloads: list[tuple[int, str]] = []
    for idx, start in enumerate(range(0, len(rows), batch_size), start=1):
        batch_rows = rows[start : start + batch_size]
        batch_payloads.append((idx, json.dumps(batch_rows, ensure_ascii=False)))

    ordered_results: list[str] = [""] * len(batch_payloads)
    max_workers = min(len(batch_payloads), 8)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_pos = {
            executor.submit(
                _compress_context,
                intent,
                f"{data_name}-batch-{idx}",
                raw_json,
                None,
                active_request_id,
            ): idx - 1
            for idx, raw_json in batch_payloads
        }
        for future, pos in future_to_pos.items():
            ordered_results[pos] = future.result()

    return "\n\n".join(ordered_results)


def _compress_structured_chapter_context_in_batches(
    intent: str,
    data_name: str,
    rows: list[dict[str, Any]],
    batch_size: int,
    requested_start: int,
    requested_end: int,
    request_id: str = "",
) -> str:
    active_request_id = request_id or get_active_request_id()
    batch_payloads: list[tuple[int, str]] = []
    for idx, start in enumerate(range(0, len(rows), max(1, batch_size)), start=1):
        batch_rows = rows[start : start + max(1, batch_size)]
        batch_payloads.append((idx, json.dumps(batch_rows, ensure_ascii=False)))

    ordered_results: list[dict[str, Any]] = [{} for _ in batch_payloads]
    default_needs_fulltext = data_name == "章节摘要(chapter_summaries)"

    with ThreadPoolExecutor(max_workers=min(len(batch_payloads), 8) or 1) as executor:
        future_to_pos = {
            executor.submit(
                _compress_context,
                intent,
                f"{data_name}-batch-{idx}",
                raw_json,
                CHAPTER_STRUCTURED_COMPRESSION_PROMPT,
                active_request_id,
            ): idx - 1
            for idx, raw_json in batch_payloads
        }
        for future, pos in future_to_pos.items():
            ordered_results[pos] = _normalize_structured_chapter_batch_result(
                future.result(),
                default_needs_fulltext=default_needs_fulltext,
            )

    return _aggregate_structured_chapter_results(
        data_name=data_name,
        requested_start=requested_start,
        requested_end=requested_end,
        batch_results=ordered_results,
    )


@tool
def retrieve_volumes(book_id: int, intent: str) -> str:
    """
    检索全书的卷级 (Volume) 宏观摘要和情节范围。
    当需要了解整体故事脉络、时间线，或不确定具体情节在哪一卷时使用。
    参数 intent：说明你当前想要寻找的具体目标或问题（如“主角第一次获得XXX”），工具会自动过滤无关剧情并返回精简结论。
    """
    logger.info("[Tool] Executing retrieve_volumes for book_id=%d", book_id)
    try:
        rows = _get_cached_volume_rows(book_id)
        volume_start, volume_end = _int_bounds(rows, "volume_index")
        plot_start, _ = _int_bounds(rows, "start_plot_index")
        _, plot_end = _int_bounds(rows, "end_plot_index")
        logger.info(
            "[Trace][Info] volume_summary_index_range=%s-%s, mapped_plot_range=%s-%s, count=%d",
            volume_start,
            volume_end,
            plot_start,
            plot_end,
            len(rows),
        )
        if not rows:
            return "数据库中未找到该书的卷级信息。"

        return _compress_context_in_batches(
            intent,
            "卷级摘要(volumes)",
            rows,
            batch_size=1,
            request_id=get_active_request_id(),
        )
    except Exception as e:
        logger.error(f"[Tool Error] retrieve_volumes: {e}")
        return f"检索失败: {str(e)}"


@tool
def retrieve_plots(book_id: int, start_plot_index: int, end_plot_index: int, intent: str) -> str:
    """
    检索特定范围内的情节级 (Plot) 摘要和章节范围。
    必须先通过 retrieve_volumes 获取正确的 plot_index 范围。
    参数 intent：说明你当前想要寻找的具体目标或问题，工具会自动过滤无关剧情并返回精简结论。
    """
    logger.info("[Tool] Executing retrieve_plots for plots %d to %d", start_plot_index, end_plot_index)
    try:
        rows = [
            row
            for row in _get_cached_plot_rows(book_id)
            if start_plot_index <= int(row.get("plot_id") or 0) <= end_plot_index
        ]
        actual_plot_start, actual_plot_end = _int_bounds(rows, "plot_id")
        chapter_start, _ = _int_bounds(rows, "start_chapter_index")
        _, chapter_end = _int_bounds(rows, "end_chapter_index")
        logger.info(
            "[Trace][Info] plot_summary_requested_range=%d-%d, plot_summary_returned_range=%s-%s, mapped_chapter_range=%s-%s, count=%d",
            start_plot_index,
            end_plot_index,
            actual_plot_start,
            actual_plot_end,
            chapter_start,
            chapter_end,
            len(rows),
        )
        if not rows:
            return "未找到指定范围内的情节信息，请检查 plot_index 是否正确。"

        return _compress_context_in_batches(
            intent,
            "情节摘要(plots)",
            rows,
            batch_size=5,
            request_id=get_active_request_id(),
        )
    except Exception as e:
        logger.error(f"[Tool Error] retrieve_plots: {e}")
        return f"检索失败: {str(e)}"


@tool
def retrieve_chapter_summaries(book_id: int, start_chapter_index: int, end_chapter_index: int, intent: str) -> str:
    """
    检索章节级摘要（chapter_summary）和标题，用于近观层分析。
    适用于在 plot 范围已锁定后，先用摘要综合判断，再决定是否继续下潜到原文。
    参数 intent：说明你当前想要寻找的具体目标或问题，工具会自动过滤无关剧情并返回精简结论。
    """
    logger.info(
        "[Tool] Executing retrieve_chapter_summaries for chapters %d to %d",
        start_chapter_index,
        end_chapter_index,
    )

    try:
        with _connect_mysql() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT chapter_index, title, chapter_summary
                    FROM book_chapters
                    WHERE book_id = %s AND chapter_index BETWEEN %s AND %s
                    ORDER BY chapter_index ASC
                    """,
                    (book_id, start_chapter_index, end_chapter_index),
                )
                rows = list(cursor.fetchall() or [])
        actual_chapter_start, actual_chapter_end = _int_bounds(rows, "chapter_index")
        logger.info(
            "[Trace][Info] chapter_summary_requested_range=%d-%d, chapter_summary_returned_range=%s-%s, count=%d",
            start_chapter_index,
            end_chapter_index,
            actual_chapter_start,
            actual_chapter_end,
            len(rows),
        )

        if not rows:
            return "未找到指定范围内的章节摘要信息。"

        for row in rows:
            if row.get("chapter_summary") is None:
                row["chapter_summary"] = ""

        return _compress_structured_chapter_context_in_batches(
            intent,
            "章节摘要(chapter_summaries)",
            rows,
            batch_size=5,
            requested_start=start_chapter_index,
            requested_end=end_chapter_index,
            request_id=get_active_request_id(),
        )
    except Exception as e:
        logger.error(f"[Tool Error] retrieve_chapter_summaries: {e}")
        return f"检索失败: {str(e)}"


@tool
def retrieve_chapters(book_id: int, start_chapter_index: int, end_chapter_index: int, intent: str) -> str:
    """
    检索底层章节正文和对话细节。
    限制：为了防止信息过载，一次查询的章节跨度不得超过 10 章。
    参数 intent：说明你当前想要寻找的具体目标或问题，工具会自动提取原文中的核心对话和动作并返回。
    """
    logger.info("[Tool] Executing retrieve_chapters for chapters %d to %d", start_chapter_index, end_chapter_index)

    if end_chapter_index - start_chapter_index > 10:
        return "【系统警告】：请求的章节跨度过大，一次最多检索 10 章。请重新规划你的 start_chapter_index 和 end_chapter_index。"

    try:
        with _connect_mysql() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT chapter_index, title, content
                    FROM book_chapters
                    WHERE book_id = %s AND chapter_index BETWEEN %s AND %s
                    ORDER BY chapter_index ASC
                    """,
                    (book_id, start_chapter_index, end_chapter_index),
                )
                rows = list(cursor.fetchall() or [])
        actual_chapter_start, actual_chapter_end = _int_bounds(rows, "chapter_index")
        logger.info(
            "[Trace][Info] chapter_content_requested_range=%d-%d, chapter_content_returned_range=%s-%s, count=%d",
            start_chapter_index,
            end_chapter_index,
            actual_chapter_start,
            actual_chapter_end,
            len(rows),
        )

        if not rows:
            return "未找到指定范围内的章节原文。"

        for row in rows:
            content = row.get("content", "")
            if len(content) > 6000:
                row["content"] = content[:6000] + "\n...[内容已截断]"

        return _compress_structured_chapter_context_in_batches(
            intent,
            "章节原文(chapters)",
            rows,
            batch_size=5,
            requested_start=start_chapter_index,
            requested_end=end_chapter_index,
            request_id=get_active_request_id(),
        )
    except Exception as e:
        logger.error(f"[Tool Error] retrieve_chapters: {e}")
        return f"检索失败: {str(e)}"


@tool
def retrieve_chapter_directory(book_id: int, start_chapter_index: int, end_chapter_index: int, intent: str) -> str:
    """
    检索真实章节号 (chapter_index) 与章节文本标题 (title) 的映射目录。
    【极度重要】：当你发现 plot 中提到的中文序号（如“第1330章”）与你尝试检索的物理 chapter_index 内容不符时，必须调用此工具。

    【参数 intent 的绝对禁忌】：
    intent 只能用于指定你想要匹配的“中文数字序号”或“核心标题名词”！
    正确示例："寻找标题为『第一千三百三十章』对应的真实 chapter_index"。
    严禁在 intent 中输入任何剧情动作（如“张伟遗言”）。
    """
    logger.info("[Tool] Executing retrieve_chapter_directory for index %d to %d", start_chapter_index, end_chapter_index)

    safe_start = max(1, start_chapter_index - 75)
    safe_end = end_chapter_index + 75

    try:
        with _connect_mysql() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT chapter_index, title
                    FROM book_chapters
                    WHERE book_id = %s AND chapter_index BETWEEN %s AND %s
                    ORDER BY chapter_index ASC
                    """,
                    (book_id, safe_start, safe_end),
                )
                rows = list(cursor.fetchall() or [])

        if not rows:
            return "未找到对应范围的章节目录映射。"

        raw_json = json.dumps(rows, ensure_ascii=False)
        compressed = _compress_context(
            intent=intent,
            data_name="章节目录映射表(chapter_directory)",
            raw_data=raw_json,
            custom_prompt=DIRECTORY_COMPRESSION_PROMPT,
            request_id=get_active_request_id(),
        )
        process_info = f"[处理信息]：目录采样范围 chapter_index={safe_start}-{safe_end}，命中 {len(rows)} 条。\n"
        return process_info + compressed

    except Exception as e:
        logger.error(f"[Tool Error] retrieve_chapter_directory: {e}")
        return f"目录检索失败: {str(e)}"


__all__ = [
    "retrieve_volumes",
    "retrieve_plots",
    "retrieve_chapter_summaries",
    "retrieve_chapters",
    "retrieve_chapter_directory",
    "resolve_book_id",
    "to_positive_int",
]
