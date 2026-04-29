from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from agent.graph import build_llm
from core.public_runtime import require_runtime_llm_model
from dotenv import load_dotenv
import pymysql
import pydantic

from rag.plotsToVolumes import VolumeSegmentationEngine
from rag.prompt import PLOT_ANALYSIS_PROMPT

ROOT_DIR = Path(__file__).resolve().parents[1]
logger = logging.getLogger(__name__)
BOOK_PLOTS_IF_EXISTS_ENV_VAR = "BOOK_PLOTS_IF_EXISTS"
_STEP2_RETRY_LOCK = threading.Lock()
_STEP_RETRY_TIMERS: dict[tuple[str, int, int], threading.Timer] = {}
_STEP2_RETRY_EXECUTION_LOCK = threading.Lock()
PLOT_JSON_PARSE_PRIMARY_ATTEMPTS = 3
PLOT_JSON_PARSE_FALLBACK_ATTEMPTS = 2


def _plot_json_parse_fallback_model() -> str:
    return str(os.getenv("PLOT_JSON_PARSE_FALLBACK_MODEL", "")).strip() or require_runtime_llm_model()


def _plot_manual_error_retry_model() -> str:
    return str(os.getenv("PLOT_MANUAL_ERROR_RETRY_MODEL", "")).strip() or require_runtime_llm_model()


def _progress_bar(current: int, total: int, width: int = 24) -> str:
    safe_total = max(1, int(total))
    safe_current = max(0, min(int(current), safe_total))
    filled = int(width * safe_current / safe_total)
    return f"[{'#' * filled}{'-' * (width - filled)}]"


def _load_runtime_env() -> None:
    load_dotenv(dotenv_path=ROOT_DIR / ".env")
    override_path = os.getenv("STORY2MEMORY_ENV_OVERRIDE")
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


def _connect():
    _load_runtime_env()
    dsn = os.getenv("MYSQL_DSN", "").strip()
    cfg = _parse_mysql_dsn(dsn)
    if not cfg:
        raise RuntimeError("Missing or invalid MYSQL_DSN.")
    return pymysql.connect(**cfg)


def _next_id(cursor, table_name: str) -> int:
    if table_name != "book_plots":
        raise ValueError(f"Unsupported table name: {table_name}")
    cursor.execute(f"SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM `{table_name}`")
    row = cursor.fetchone() or {}
    return int(row.get("next_id") or 1)


def _utcnow() -> dt.datetime:
    return dt.datetime.utcnow().replace(microsecond=0)


def _normalize_retry_count(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _parse_datetime_value(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _retry_delay_seconds(retry_count: int) -> int:
    return min(60 * 60, (2 ** max(0, int(retry_count))) * 60)


class PlotChapterItem(pydantic.BaseModel):
    chapter_index: int
    title: str = ""
    chapter_summary: str = ""
    character_text: str = ""
    special_existence_text: str = ""
    organizations_text: str = ""
    world_rules_text: str = ""
    raw_summary_json_text: str = ""
    raw_summary_json_value: Any = None
    parsed_summary_payload: dict[str, Any] | None = None


class PlotSourceGroup(pydantic.BaseModel):
    book_id: int
    plot_id: int
    start_chapter: int
    end_chapter: int
    chapters: list[PlotChapterItem]
    chapter_data: str
    character: list[dict[str, Any]] = pydantic.Field(default_factory=list)
    special_existence: list[dict[str, Any]] = pydantic.Field(default_factory=list)
    origanizations: list[dict[str, Any]] = pydantic.Field(default_factory=list)
    world_rules: list[dict[str, Any]] = pydantic.Field(default_factory=list)


class PlotAnalysisResult(pydantic.BaseModel):
    plot_id: int
    start_chapter: int
    end_chapter: int
    title: str
    status: str = "pending"
    plot_summary: str
    character: list[dict[str, Any]] = pydantic.Field(default_factory=list)
    special_existence: list[dict[str, Any]] = pydantic.Field(default_factory=list)
    origanizations: list[dict[str, Any]] = pydantic.Field(default_factory=list)
    world_rules: list[dict[str, Any]] = pydantic.Field(default_factory=list)
    metadata: dict[str, Any] = pydantic.Field(default_factory=dict)
    search_content: str = ""
    raw_plot_json: dict[str, Any] = pydantic.Field(default_factory=dict)
    raw_plot_step1_json: dict[str, Any] = pydantic.Field(default_factory=dict)
    raw_plot_step2_json: dict[str, Any] | None = None
    step1_status: str = "success"
    step1_retry_count: int = 0
    step1_last_error: str = ""
    step1_next_retry_at: dt.datetime | None = None
    step2_status: str = "pending"
    step2_retry_count: int = 0
    step2_last_error: str = ""
    step2_next_retry_at: dt.datetime | None = None


class PlotRecordBuilder:
    def __init__(
        self,
        max_concurrency: int = 25,
        max_parse_retries: int = 10,
        primary_model_override: str | None = None,
        fallback_model_override: str | None = None,
    ) -> None:
        self.max_concurrency = min(10, max(1, int(max_concurrency)))
        self.max_parse_retries = max(1, int(max_parse_retries))
        _load_runtime_env()
        self.llm_client = build_llm(primary_model_override)
        self._fallback_llm_client = None
        self._fallback_model_override = fallback_model_override
        self._api_semaphore = asyncio.Semaphore(self.max_concurrency)
        self.if_exists = self._resolve_if_exists_mode()

    def run(self, book_id: int) -> dict[str, Any]:
        existing_count = self._count_existing_book_plots(book_id)
        plot_ids = self._load_plot_ids(book_id)
        successful_plot_ids = self._load_successful_plot_ids(book_id)
        plots_complete = set(successful_plot_ids) >= set(plot_ids)
        if existing_count > 0:
            logger.warning(
                "[分析进度][情节摘要入库][book=%s] 预检查：book_plots已有 %d 条，策略=%s。",
                book_id,
                existing_count,
                self.if_exists,
            )
            if self.if_exists == "skip" and plots_complete:
                logger.warning(
                    "[分析进度][情节摘要入库][book=%s] 跳过生成，保留既有 book_plots。",
                    book_id,
                )
                volume_stats = VolumeSegmentationEngine().run(book_id)
                return {
                    "book_id": book_id,
                    "plot_count": existing_count,
                    "skipped": True,
                    "entity_sync_skipped": True,
                    "volume": volume_stats,
                }

        if not plot_ids:
            logger.warning("[分析进度][情节摘要入库][book=%s] 未检测到有效 plot_id，跳过。", book_id)
            return {"book_id": book_id, "plot_count": 0}

        logger.warning(
            "[分析进度][情节摘要入库][book=%s] 准备处理 %d 个 plot_id。",
            book_id,
            len(plot_ids if self.if_exists != "skip" else [pid for pid in plot_ids if pid not in successful_plot_ids]),
        )
        plot_ids_to_process = plot_ids if self.if_exists != "skip" else [pid for pid in plot_ids if pid not in successful_plot_ids]
        self._prune_stale_book_plots(book_id, plot_ids)
        groups = asyncio.run(self._load_plot_groups_concurrently(book_id, plot_ids_to_process))
        prepared_groups, prepare_failures = self._prepare_structured_groups(groups)
        if prepare_failures:
            for failed_group, error_message in prepare_failures:
                self._mark_plot_preflight_error(
                    failed_group.book_id,
                    plot_id=failed_group.plot_id,
                    start_chapter=failed_group.start_chapter,
                    end_chapter=failed_group.end_chapter,
                    error_message=error_message,
                )
            failed_plot_ids = [group.plot_id for group, _ in prepare_failures]
            raise RuntimeError(
                f"plot_structured_prepare_failed book_id={book_id} failed_plots={failed_plot_ids}"
            )

        self._upsert_structured_groups(book_id, prepared_groups)
        results = asyncio.run(self._analyze_groups_concurrently(prepared_groups))
        logger.warning("[分析进度][book=%s] 开始生成实体表并同步实体Qdrant...", book_id)
        entity_stats = self._rebuild_entity_tables_and_qdrant(book_id)
        volume_stats = VolumeSegmentationEngine().run(book_id)
        return {
            "book_id": book_id,
            "plot_count": len(results),
            "entity": entity_stats,
            "volume": volume_stats,
        }

    def _rebuild_entity_tables_and_qdrant(self, book_id: int) -> dict[str, Any]:
        from rag.rebuildEntityTablesAndQdrant import rebuild_entity_tables_and_qdrant

        stats = rebuild_entity_tables_and_qdrant(book_id)
        logger.warning(
            "[分析进度][book=%s] 实体表与实体Qdrant同步完成：%s",
            book_id,
            json.dumps(stats, ensure_ascii=False),
        )
        return stats

    def _prune_stale_book_plots(self, book_id: int, plot_ids: list[int]) -> None:
        with _connect() as conn:
            with conn.cursor() as cursor:
                if not plot_ids:
                    cursor.execute("DELETE FROM book_plots WHERE book_id = %s", (book_id,))
                    return
                placeholders = ", ".join(["%s"] * len(plot_ids))
                cursor.execute(
                    f"DELETE FROM book_plots WHERE book_id = %s AND plot_id NOT IN ({placeholders})",
                    (book_id, *plot_ids),
                )

    def _load_plot_ids(self, book_id: int) -> list[int]:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT plot_id
                    FROM book_chapters
                    WHERE book_id = %s AND COALESCE(plot_id, 0) <> 0
                    ORDER BY plot_id ASC
                    """,
                    (book_id,),
                )
                rows = list(cursor.fetchall() or [])
        return [int(row.get("plot_id") or 0) for row in rows if int(row.get("plot_id") or 0) > 0]

    def _load_successful_plot_ids(self, book_id: int) -> list[int]:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT plot_id
                    FROM book_plots
                    WHERE book_id = %s
                      AND status = 'success'
                      AND `character` IS NOT NULL
                      AND special_existence IS NOT NULL
                      AND origanizations IS NOT NULL
                      AND world_rules IS NOT NULL
                    ORDER BY plot_id ASC
                    """,
                    (book_id,),
                )
                rows = list(cursor.fetchall() or [])
        return [int(row.get("plot_id") or 0) for row in rows if int(row.get("plot_id") or 0) > 0]

    def _get_fallback_llm_client(self):
        if self._fallback_llm_client is None:
            self._fallback_llm_client = build_llm(self._fallback_model_override or _plot_json_parse_fallback_model())
        return self._fallback_llm_client

    def _backfill_plot_step_statuses(self, book_id: int) -> None:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE book_plots
                    SET
                        step1_status = CASE
                            WHEN (COALESCE(step1_status, '') = '' OR step1_status = 'pending')
                                 AND COALESCE(step1_last_error, '') = ''
                                 AND COALESCE(plot_summary, '') <> ''
                            THEN 'success'
                            ELSE step1_status
                        END,
                        step2_status = CASE
                            WHEN (COALESCE(step2_status, '') = '' OR step2_status = 'pending')
                                 AND COALESCE(step2_last_error, '') = ''
                                 AND COALESCE(metadata, JSON_OBJECT()) IS NOT NULL
                                 AND JSON_VALID(metadata)
                            THEN 'success'
                            WHEN (COALESCE(step2_status, '') = '' OR step2_status = 'pending')
                                 AND COALESCE(step2_last_error, '') = ''
                                 AND COALESCE(plot_summary, '') <> ''
                            THEN 'success'
                            ELSE step2_status
                        END
                    WHERE book_id = %s
                    """,
                    (book_id,),
                )

    def _load_plot_row(self, book_id: int, plot_id: int) -> dict[str, Any] | None:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT *
                    FROM book_plots
                    WHERE book_id = %s AND plot_id = %s
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (book_id, plot_id),
                )
                row = cursor.fetchone() or None
        return dict(row) if row else None

    def _load_retry_candidate_plot_ids(self, book_id: int, *, exhausted_only: bool, step: str) -> list[int]:
        if step == "step1":
            status_col = "step1_status"
            retry_col = "step1_retry_count"
        else:
            status_col = "step2_status"
            retry_col = "step2_retry_count"
        clause = f"COALESCE({retry_col}, 0) >= 10" if exhausted_only else f"COALESCE({retry_col}, 0) < 10"
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT plot_id
                    FROM book_plots
                    WHERE book_id = %s
                      AND {status_col} = 'error'
                      AND {clause}
                    ORDER BY plot_id ASC
                    """,
                    (book_id,),
                )
                rows = list(cursor.fetchall() or [])
        return [int(row.get("plot_id") or 0) for row in rows if int(row.get("plot_id") or 0) > 0]

    def _retry_error_step2_rows(self, book_id: int) -> dict[str, int]:
        plot_ids = self._load_retry_candidate_plot_ids(book_id, exhausted_only=False, step="step2")
        stats = {"recovered": 0, "failed": 0}
        total = len(plot_ids)
        if total:
            logger.warning(
                "[分析进度][情节摘要LLM-Step2重跑][book=%s] %s %d/%d",
                book_id,
                _progress_bar(0, total),
                0,
                total,
            )
        for plot_id in plot_ids:
            try:
                retry_builder = PlotRecordBuilder(
                    max_concurrency=self.max_concurrency,
                    max_parse_retries=self.max_parse_retries,
                    primary_model_override=_plot_manual_error_retry_model(),
                    fallback_model_override=_plot_manual_error_retry_model(),
                )
                updated = retry_builder.retry_step2_for_plot(book_id, plot_id, background=False)
            except Exception:
                logger.exception("[PlotStep2Retry] foreground retry failed. book=%s plot_id=%s", book_id, plot_id)
                updated = False
            if updated:
                stats["recovered"] += 1
            else:
                stats["failed"] += 1
            if total:
                done = stats["recovered"] + stats["failed"]
                logger.warning(
                    "[分析进度][情节摘要LLM-Step2重跑][book=%s] %s %d/%d",
                    book_id,
                    _progress_bar(done, total),
                    done,
                    total,
                )
        return stats

    def _retry_error_step1_rows(self, book_id: int) -> dict[str, int]:
        plot_ids = self._load_retry_candidate_plot_ids(book_id, exhausted_only=False, step="step1")
        stats = {"recovered": 0, "failed": 0}
        total = len(plot_ids)
        if total:
            logger.warning(
                "[分析进度][情节摘要LLM-Step1重跑][book=%s] %s %d/%d",
                book_id,
                _progress_bar(0, total),
                0,
                total,
            )
        for plot_id in plot_ids:
            try:
                retry_builder = PlotRecordBuilder(
                    max_concurrency=self.max_concurrency,
                    max_parse_retries=self.max_parse_retries,
                    primary_model_override=_plot_manual_error_retry_model(),
                    fallback_model_override=_plot_manual_error_retry_model(),
                )
                updated = retry_builder.retry_step1_for_plot(book_id, plot_id, background=False)
            except Exception:
                logger.exception("[PlotStep1Retry] foreground retry failed. book=%s plot_id=%s", book_id, plot_id)
                updated = False
            if updated:
                stats["recovered"] += 1
            else:
                stats["failed"] += 1
            if total:
                done = stats["recovered"] + stats["failed"]
                logger.warning(
                    "[分析进度][情节摘要LLM-Step1重跑][book=%s] %s %d/%d",
                    book_id,
                    _progress_bar(done, total),
                    done,
                    total,
                )
        return stats

    def _schedule_results_background_retries(self, book_id: int, results: list[PlotAnalysisResult]) -> None:
        for item in results:
            if item.step1_status == "error":
                self._schedule_background_retry(
                    step="step1",
                    book_id=book_id,
                    plot_id=item.plot_id,
                    retry_count=item.step1_retry_count,
                    next_retry_at=item.step1_next_retry_at,
                )
            if item.step2_status != "error":
                continue
            self._schedule_background_retry(
                step="step2",
                book_id=book_id,
                plot_id=item.plot_id,
                retry_count=item.step2_retry_count,
                next_retry_at=item.step2_next_retry_at,
            )

    def _schedule_pending_background_retries(self, book_id: int) -> None:
        for plot_id in self._load_retry_candidate_plot_ids(book_id, exhausted_only=False, step="step1"):
            row = self._load_plot_row(book_id, plot_id)
            if not row:
                continue
            self._schedule_background_retry(
                step="step1",
                book_id=book_id,
                plot_id=plot_id,
                retry_count=_normalize_retry_count(row.get("step1_retry_count")),
                next_retry_at=_parse_datetime_value(row.get("step1_next_retry_at")),
            )
        for plot_id in self._load_retry_candidate_plot_ids(book_id, exhausted_only=False, step="step2"):
            row = self._load_plot_row(book_id, plot_id)
            if not row:
                continue
            self._schedule_background_retry(
                step="step2",
                book_id=book_id,
                plot_id=plot_id,
                retry_count=_normalize_retry_count(row.get("step2_retry_count")),
                next_retry_at=_parse_datetime_value(row.get("step2_next_retry_at")),
            )

    def _schedule_background_retry(
        self,
        *,
        step: str,
        book_id: int,
        plot_id: int,
        retry_count: int,
        next_retry_at: dt.datetime | None,
    ) -> None:
        key = (str(step), int(book_id), int(plot_id))
        now = _utcnow()
        delay_seconds = _retry_delay_seconds(retry_count)
        if next_retry_at is not None:
            delay_seconds = max(0, int((next_retry_at - now).total_seconds()))
        with _STEP2_RETRY_LOCK:
            existing = _STEP_RETRY_TIMERS.get(key)
            if existing is not None and existing.is_alive():
                return
            timer = threading.Timer(
                max(0, delay_seconds),
                self._background_retry_entrypoint,
                kwargs={"step": str(step), "book_id": int(book_id), "plot_id": int(plot_id)},
            )
            timer.daemon = True
            _STEP_RETRY_TIMERS[key] = timer
            timer.start()

    def _background_retry_entrypoint(self, *, step: str, book_id: int, plot_id: int) -> None:
        key = (str(step), int(book_id), int(plot_id))
        with _STEP2_RETRY_LOCK:
            _STEP_RETRY_TIMERS.pop(key, None)
        with _STEP2_RETRY_EXECUTION_LOCK:
            try:
                retry_builder = PlotRecordBuilder(
                    max_concurrency=self.max_concurrency,
                    max_parse_retries=self.max_parse_retries,
                )
                if step == "step1":
                    retry_builder.retry_step1_for_plot(book_id, plot_id, background=True)
                else:
                    retry_builder.retry_step2_for_plot(book_id, plot_id, background=True)
            except Exception:
                logger.exception("[PlotRetry] background retry crashed. step=%s book=%s plot_id=%s", step, book_id, plot_id)

    async def _load_plot_groups_concurrently(
        self, book_id: int, plot_ids: list[int]
    ) -> list[PlotSourceGroup]:
        tasks = [self._load_single_group(book_id, plot_id) for plot_id in plot_ids]
        total = len(tasks)
        done = 0
        if total:
            logger.warning(
                "[分析进度][情节摘要分组读取] %s %d/%d",
                _progress_bar(0, total),
                0,
                total,
            )
        rows_by_plot_id: dict[int, list[dict[str, Any]]] = {}
        for task in asyncio.as_completed(tasks):
            plot_id, rows = await task
            rows_by_plot_id[plot_id] = rows
            done += 1
            logger.warning(
                "[分析进度][情节摘要分组读取] %s %d/%d",
                _progress_bar(done, total),
                done,
                total,
            )

        groups: list[PlotSourceGroup] = []
        for plot_id in plot_ids:
            rows = rows_by_plot_id.get(plot_id, [])
            group = self._build_plot_group(book_id, plot_id, rows)
            if group is not None:
                groups.append(group)
        return groups

    async def _load_single_group(self, book_id: int, plot_id: int) -> tuple[int, list[dict[str, Any]]]:
        rows = await asyncio.to_thread(self._load_plot_group_rows, book_id, plot_id)
        return plot_id, rows

    def _load_plot_group_rows(self, book_id: int, plot_id: int) -> list[dict[str, Any]]:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        chapter_index,
                        title,
                        chapter_summary,
                        `character`,
                        special_existence,
                        origanizations,
                        world_rules,
                        raw_summary_json
                    FROM book_chapters
                    WHERE book_id = %s AND plot_id = %s
                    ORDER BY chapter_index ASC, id ASC
                    """,
                    (book_id, plot_id),
                )
                return list(cursor.fetchall() or [])

    def _build_plot_group(self, book_id: int, plot_id: int, rows: list[dict[str, Any]]) -> PlotSourceGroup | None:
        chapters: list[PlotChapterItem] = [
            PlotChapterItem(
                chapter_index=int(row.get("chapter_index") or 0),
                title=str(row.get("title") or "").strip(),
                chapter_summary=str(row.get("chapter_summary") or "").strip(),
                character_text=str(row.get("character") or "").strip(),
                special_existence_text=str(row.get("special_existence") or "").strip(),
                organizations_text=str(row.get("origanizations") or "").strip(),
                world_rules_text=str(row.get("world_rules") or "").strip(),
                raw_summary_json_text=(
                    json.dumps(row.get("raw_summary_json"), ensure_ascii=False)
                    if isinstance(row.get("raw_summary_json"), (dict, list))
                    else str(row.get("raw_summary_json") or "").strip()
                ),
                raw_summary_json_value=row.get("raw_summary_json"),
            )
            for row in rows
            if int(row.get("chapter_index") or 0) > 0
        ]
        if not chapters:
            return None

        chapter_data = "\n".join(
            (
                f'【Ch {chapter.chapter_index} | 摘要: "{chapter.chapter_summary.replace(chr(10), " / ")}"】'
            )
            for chapter in chapters
        )
        return PlotSourceGroup(
            book_id=book_id,
            plot_id=plot_id,
            start_chapter=chapters[0].chapter_index,
            end_chapter=chapters[-1].chapter_index,
            chapters=chapters,
            chapter_data=chapter_data,
        )

    def _prepare_structured_groups(
        self, groups: list[PlotSourceGroup]
    ) -> tuple[list[PlotSourceGroup], list[tuple[PlotSourceGroup, str]]]:
        if not groups:
            return [], []

        total = len(groups)
        done = 0
        logger.warning(
            "[分析进度][情节结构聚合] %s %d/%d",
            _progress_bar(0, total),
            0,
            total,
        )
        prepared: list[PlotSourceGroup] = []
        failures: list[tuple[PlotSourceGroup, str]] = []
        for group in groups:
            try:
                self._prepare_group_structured_fields(group)
                prepared.append(group)
            except Exception as exc:
                failures.append((group, str(exc)))
            done += 1
            logger.warning(
                "[分析进度][情节结构聚合] %s %d/%d (failed=%d)",
                _progress_bar(done, total),
                done,
                total,
                len(failures),
            )
        return prepared, failures

    def _prepare_group_structured_fields(self, group: PlotSourceGroup) -> None:
        for chapter in group.chapters:
            chapter.parsed_summary_payload = self._parse_chapter_summary_payload(
                chapter.raw_summary_json_value if chapter.raw_summary_json_value is not None else chapter.raw_summary_json_text,
                chapter_index=chapter.chapter_index,
                plot_id=group.plot_id,
            )
        group.character = self._merge_plot_named_entries(group.chapters, "character")
        group.special_existence = self._merge_plot_named_entries(group.chapters, "special_existence")
        group.origanizations = self._merge_plot_named_entries(group.chapters, "organizations")
        group.world_rules = self._merge_plot_named_entries(group.chapters, "world_rules")

    def _upsert_structured_groups(self, book_id: int, groups: list[PlotSourceGroup]) -> None:
        for group in groups:
            self._upsert_plot_structured_fields(book_id, group)

    async def _analyze_groups_concurrently(self, groups: list[PlotSourceGroup]) -> list[PlotAnalysisResult]:
        if not groups:
            return []
        tasks = [self._analyze_single_group(group) for group in groups]
        total = len(tasks)
        done = 0
        logger.warning(
            "[分析进度][情节摘要LLM] %s %d/%d",
            _progress_bar(0, total),
            0,
            total,
        )
        results: list[PlotAnalysisResult] = []
        for task in asyncio.as_completed(tasks):
            result = await task
            results.append(result)
            done += 1
            logger.warning(
                "[分析进度][情节摘要LLM] %s %d/%d",
                _progress_bar(done, total),
                done,
                total,
            )
        return sorted(results, key=lambda item: item.plot_id)

    async def _analyze_single_group(self, group: PlotSourceGroup) -> PlotAnalysisResult:
        self._mark_plot_pending(
            group.book_id,
            plot_id=group.plot_id,
            start_chapter=group.start_chapter,
            end_chapter=group.end_chapter,
            character=group.character,
            special_existence=group.special_existence,
            origanizations=group.origanizations,
            world_rules=group.world_rules,
        )

        plot_prompt = PLOT_ANALYSIS_PROMPT.format(
            chapter_count=len(group.chapters),
            start_chapter=group.start_chapter,
            end_chapter=group.end_chapter,
            chapter_data=group.chapter_data,
        )
        parsed, error = await self._invoke_and_parse_with_model_fallback(
            prompt=plot_prompt,
            plot_id=group.plot_id,
            step_label="Plot",
        )
        if parsed is None:
            result = PlotAnalysisResult(
                plot_id=group.plot_id,
                start_chapter=group.start_chapter,
                end_chapter=group.end_chapter,
                title="",
                plot_summary="",
                status="error",
                character=group.character,
                special_existence=group.special_existence,
                origanizations=group.origanizations,
                world_rules=group.world_rules,
                step1_last_error=error,
            )
            self._upsert_plot_result(group.book_id, result)
            return result

        title = str(parsed.get("plot_title") or f"情节{group.plot_id}").strip()
        plot_summary = str(parsed.get("summary") or "").strip()
        result = PlotAnalysisResult(
            plot_id=group.plot_id,
            start_chapter=group.start_chapter,
            end_chapter=group.end_chapter,
            title=title,
            plot_summary=plot_summary,
            status="success",
            character=group.character,
            special_existence=group.special_existence,
            origanizations=group.origanizations,
            world_rules=group.world_rules,
        )
        self._upsert_plot_result(group.book_id, result)
        return result

    async def _invoke_llm(self, prompt: str) -> str:
        ainvoke = getattr(self.llm_client, "ainvoke", None)
        if callable(ainvoke):
            response = await ainvoke(prompt)
        else:
            response = await asyncio.to_thread(self.llm_client.invoke, prompt)
        content = getattr(response, "content", response)
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item.strip())
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
            return "\n".join(part for part in parts if part)
        return str(content).strip()

    async def _invoke_llm_with_client(self, client: Any, prompt: str) -> str:
        ainvoke = getattr(client, "ainvoke", None)
        if callable(ainvoke):
            response = await ainvoke(prompt)
        else:
            response = await asyncio.to_thread(client.invoke, prompt)
        content = getattr(response, "content", response)
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item.strip())
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
            return "\n".join(part for part in parts if part)
        return str(content).strip()

    async def _invoke_and_parse_with_model_fallback(
        self,
        *,
        prompt: str,
        plot_id: int,
        step_label: str,
    ) -> tuple[dict[str, Any] | None, str]:
        attempts = (
            [("primary", self.llm_client)] * PLOT_JSON_PARSE_PRIMARY_ATTEMPTS
            + [("fallback", self._get_fallback_llm_client())] * PLOT_JSON_PARSE_FALLBACK_ATTEMPTS
        )
        last_error = ""
        total_attempts = len(attempts)
        for attempt_index, (model_label, client) in enumerate(attempts, start=1):
            try:
                async with self._api_semaphore:
                    raw_text = await self._invoke_llm_with_client(client, prompt)
                parsed = self._extract_json(raw_text)
                if isinstance(parsed, dict):
                    if step_label == "Plot":
                        self._validate_plot_prompt_output(parsed)
                    return parsed, ""
                raise ValueError(f"{step_label} JSON is not an object.")
            except Exception as exc:
                last_error = str(exc)
                raw_preview = ""
                if isinstance(locals().get("raw_text"), str):
                    raw_preview = str(locals()["raw_text"]).replace("\n", " ").strip()[:300]
                logger.error(
                    "[事故][情节摘要LLM-%s][plot_id=%s] 响应格式异常，attempt=%d/%d，model=%s，error=%s，raw_preview=%s",
                    step_label,
                    plot_id,
                    attempt_index,
                    total_attempts,
                    model_label,
                    exc,
                    raw_preview,
                )
                if attempt_index < total_attempts:
                    logger.warning(
                        "[分析进度][情节摘要LLM-%s][plot_id=%s] 针对异常响应重试本次API调用... model=%s",
                        step_label,
                        plot_id,
                        _plot_json_parse_fallback_model()
                        if attempt_index == PLOT_JSON_PARSE_PRIMARY_ATTEMPTS
                        else model_label,
                    )
                    continue
        return None, last_error

    async def _invoke_and_parse_manual_error_retry(
        self,
        *,
        prompt: str,
        plot_id: int,
        step_label: str,
    ) -> tuple[dict[str, Any] | None, str]:
        client = self.llm_client

        async def _single_attempt(batch_label: str, batch_index: int) -> tuple[dict[str, Any] | None, str]:
            try:
                async with self._api_semaphore:
                    raw_text = await self._invoke_llm_with_client(client, prompt)
                parsed = self._extract_json(raw_text)
                if isinstance(parsed, dict):
                    return parsed, ""
                raise ValueError(f"{step_label} JSON is not an object.")
            except Exception as exc:
                raw_preview = ""
                if isinstance(locals().get("raw_text"), str):
                    raw_preview = str(locals()["raw_text"]).replace("\n", " ").strip()[:300]
                logger.error(
                    "[事故][情节摘要LLM-%s重跑][plot_id=%s] 批次=%s index=%d，model=%s，error=%s，raw_preview=%s",
                    step_label,
                    plot_id,
                    batch_label,
                    batch_index,
                    _plot_manual_error_retry_model(),
                    exc,
                    raw_preview,
                )
                return None, str(exc)

        async def _run_batch(batch_size: int, batch_label: str) -> tuple[dict[str, Any] | None, str]:
            tasks = [
                asyncio.create_task(_single_attempt(batch_label, idx))
                for idx in range(1, batch_size + 1)
            ]
            last_error = ""
            try:
                for task in asyncio.as_completed(tasks):
                    parsed, error = await task
                    if parsed is not None:
                        for other in tasks:
                            if not other.done():
                                other.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        return parsed, ""
                    if error:
                        last_error = error
                return None, last_error
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        logger.warning(
            "[分析进度][情节摘要LLM-%s重跑][plot_id=%s] 使用 %s 并发 3 次重试。",
            step_label,
            plot_id,
            _plot_manual_error_retry_model(),
        )
        parsed, error = await _run_batch(3, "batch3")
        if parsed is not None:
            return parsed, ""

        logger.warning(
            "[分析进度][情节摘要LLM-%s重跑][plot_id=%s] 并发 3 次全部失败，继续并发 5 次重试。",
            step_label,
            plot_id,
        )
        parsed, error = await _run_batch(5, "batch5")
        return parsed, error

    async def _invoke_and_parse_step1_with_retry(self, prompt: str, plot_id: int) -> tuple[dict[str, Any] | None, str]:
        return await self._invoke_and_parse_with_model_fallback(
            prompt=prompt,
            plot_id=plot_id,
            step_label="Step1",
        )

    async def _invoke_and_parse_step2_with_retry(
        self,
        group: PlotSourceGroup,
        step1_parsed: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        prompt = PLOT_ANALYSIS_STEP2_PROMPT.format(
            step1_data=json.dumps(step1_parsed, ensure_ascii=False),
            chapter_data=group.chapter_data,
        )
        parsed, last_error = await self._invoke_and_parse_with_model_fallback(
            prompt=prompt,
            plot_id=group.plot_id,
            step_label="Step2",
        )
        if parsed is None:
            logger.error(
                "[事故][情节摘要LLM-Step2][plot_id=%s] 重试耗尽，保留 Step1 结果并等待补跑。",
                group.plot_id,
            )
        return parsed, last_error

    def _extract_json(self, text: str) -> dict[str, Any]:
        payload = text.strip()
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", payload, flags=re.IGNORECASE)
        if fenced:
            payload = fenced.group(1).strip()
        matched = re.search(r"\{[\s\S]*\}", payload)
        candidate = matched.group(0).strip() if matched else payload
        parsed = json.loads(candidate)
        if not isinstance(parsed, dict):
            raise ValueError("Invalid LLM JSON response for plot analysis.")
        return parsed

    @staticmethod
    def _validate_plot_prompt_output(parsed: dict[str, Any]) -> None:
        title = str(parsed.get("plot_title") or "").strip()
        summary = str(parsed.get("summary") or "").strip()
        if not title:
            raise ValueError("Missing plot_title in plot analysis response.")
        if not summary:
            raise ValueError("Missing summary in plot analysis response.")

    def _mark_plot_pending(
        self,
        book_id: int,
        *,
        plot_id: int,
        start_chapter: int,
        end_chapter: int,
        character: list[dict[str, Any]],
        special_existence: list[dict[str, Any]],
        origanizations: list[dict[str, Any]],
        world_rules: list[dict[str, Any]],
    ) -> None:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id
                    FROM book_plots
                    WHERE book_id = %s AND plot_id = %s
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (book_id, plot_id),
                )
                row = cursor.fetchone() or {}
                plot_row_id = int(row.get("id") or 0)
                if plot_row_id > 0:
                    cursor.execute(
                        """
                        UPDATE book_plots
                        SET volume_id = %s,
                            start_chapter_index = %s,
                            end_chapter_index = %s,
                            status = %s,
                            `character` = %s,
                            special_existence = %s,
                            origanizations = %s,
                            world_rules = %s
                        WHERE id = %s
                        """,
                        (
                            0,
                            start_chapter,
                            end_chapter,
                            "pending",
                            json.dumps(character, ensure_ascii=False),
                            json.dumps(special_existence, ensure_ascii=False),
                            json.dumps(origanizations, ensure_ascii=False),
                            json.dumps(world_rules, ensure_ascii=False),
                            plot_row_id,
                        ),
                    )
                    return

                cursor.execute(
                    """
                    INSERT INTO book_plots
                    (
                        book_id,
                        volume_id,
                        plot_id,
                        start_chapter_index,
                        end_chapter_index,
                        title,
                        status,
                        plot_summary,
                        `character`,
                        special_existence,
                        origanizations,
                        world_rules
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        book_id,
                        0,
                        plot_id,
                        start_chapter,
                        end_chapter,
                        "",
                        "pending",
                        "",
                        json.dumps(character, ensure_ascii=False),
                        json.dumps(special_existence, ensure_ascii=False),
                        json.dumps(origanizations, ensure_ascii=False),
                        json.dumps(world_rules, ensure_ascii=False),
                    ),
                )

    def _upsert_plot_structured_fields(self, book_id: int, group: PlotSourceGroup) -> None:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id
                    FROM book_plots
                    WHERE book_id = %s AND plot_id = %s
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (book_id, group.plot_id),
                )
                row = cursor.fetchone() or {}
                plot_row_id = int(row.get("id") or 0)
                if plot_row_id > 0:
                    cursor.execute(
                        """
                        UPDATE book_plots
                        SET volume_id = %s,
                            start_chapter_index = %s,
                            end_chapter_index = %s,
                            `character` = %s,
                            special_existence = %s,
                            origanizations = %s,
                            world_rules = %s
                        WHERE id = %s
                        """,
                        (
                            0,
                            group.start_chapter,
                            group.end_chapter,
                            json.dumps(group.character, ensure_ascii=False),
                            json.dumps(group.special_existence, ensure_ascii=False),
                            json.dumps(group.origanizations, ensure_ascii=False),
                            json.dumps(group.world_rules, ensure_ascii=False),
                            plot_row_id,
                        ),
                    )
                    return

                cursor.execute(
                    """
                    INSERT INTO book_plots
                    (
                        book_id,
                        volume_id,
                        plot_id,
                        start_chapter_index,
                        end_chapter_index,
                        title,
                        status,
                        plot_summary,
                        `character`,
                        special_existence,
                        origanizations,
                        world_rules
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        book_id,
                        0,
                        group.plot_id,
                        group.start_chapter,
                        group.end_chapter,
                        "",
                        None,
                        "",
                        json.dumps(group.character, ensure_ascii=False),
                        json.dumps(group.special_existence, ensure_ascii=False),
                        json.dumps(group.origanizations, ensure_ascii=False),
                        json.dumps(group.world_rules, ensure_ascii=False),
                    ),
                )

    def _mark_plot_preflight_error(
        self,
        book_id: int,
        *,
        plot_id: int,
        start_chapter: int,
        end_chapter: int,
        error_message: str,
    ) -> None:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id
                    FROM book_plots
                    WHERE book_id = %s AND plot_id = %s
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (book_id, plot_id),
                )
                row = cursor.fetchone() or {}
                plot_row_id = int(row.get("id") or 0)
                if plot_row_id > 0:
                    cursor.execute(
                        """
                        UPDATE book_plots
                        SET volume_id = %s,
                            start_chapter_index = %s,
                            end_chapter_index = %s,
                            title = %s,
                            status = %s,
                            plot_summary = %s,
                            `character` = %s,
                            special_existence = %s,
                            origanizations = %s,
                            world_rules = %s
                        WHERE id = %s
                        """,
                        (
                            0,
                            start_chapter,
                            end_chapter,
                            "",
                            "error",
                            error_message,
                            json.dumps([], ensure_ascii=False),
                            json.dumps([], ensure_ascii=False),
                            json.dumps([], ensure_ascii=False),
                            json.dumps([], ensure_ascii=False),
                            plot_row_id,
                        ),
                    )
                    return

                cursor.execute(
                    """
                    INSERT INTO book_plots
                    (
                        book_id,
                        volume_id,
                        plot_id,
                        start_chapter_index,
                        end_chapter_index,
                        title,
                        status,
                        plot_summary,
                        `character`,
                        special_existence,
                        origanizations,
                        world_rules
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        book_id,
                        0,
                        plot_id,
                        start_chapter,
                        end_chapter,
                        "",
                        "error",
                        error_message,
                        json.dumps([], ensure_ascii=False),
                        json.dumps([], ensure_ascii=False),
                        json.dumps([], ensure_ascii=False),
                        json.dumps([], ensure_ascii=False),
                    ),
                )

    def _build_search_content(
        self,
        title: str,
        plot_summary: str,
        metadata_obj: dict[str, Any],
        chapter_data: str,
        start_chapter: int,
        end_chapter: int,
    ) -> str:
        key_entities = metadata_obj.get("key_entities", {})
        return (
            f"plot_title: {title}\n"
            f"chapter_range: {start_chapter}-{end_chapter}\n"
            f"summary: {plot_summary}\n"
            f"key_entities: {json.dumps(metadata_obj.get('key_entities', {}), ensure_ascii=False)}\n"
            f"all_characters: {json.dumps(key_entities.get('characters_who_appear', []), ensure_ascii=False)}\n"
            f"all_factions: {json.dumps(key_entities.get('factions_who_appear', []), ensure_ascii=False)}\n"
            f"character_presence: {json.dumps(metadata_obj.get('character_presence', []), ensure_ascii=False)}\n"
            f"faction_presence: {json.dumps(metadata_obj.get('faction_presence', []), ensure_ascii=False)}\n"
            f"interaction_highlights: {json.dumps(metadata_obj.get('interaction_highlights', []), ensure_ascii=False)}\n"
            f"reader_sensitive_moments: {json.dumps(metadata_obj.get('reader_sensitive_moments', []), ensure_ascii=False)}\n"
            f"plot_progression: {json.dumps(metadata_obj.get('plot_progression', []), ensure_ascii=False)}\n"
            f"plot_facts: {json.dumps(metadata_obj.get('plot_facts', {}), ensure_ascii=False)}\n"
            f"portrait_deltas: {json.dumps(metadata_obj.get('portrait_deltas', {}), ensure_ascii=False)}"
        )

    @classmethod
    def _normalize_named_description_entries(cls, raw_value: Any) -> list[dict[str, str]]:
        source = raw_value
        if isinstance(source, dict):
            source = [{"name": key, "description": item} for key, item in source.items()]
        if not isinstance(source, list):
            return []

        normalized: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in source:
            name, description = cls._coerce_named_description_pair(item)
            if not name and not description:
                continue
            signature = (name, description)
            if signature in seen:
                continue
            normalized.append({"name": name, "description": description})
            seen.add(signature)
        return normalized

    @classmethod
    def _coerce_named_description_pair(cls, item: Any) -> tuple[str, str]:
        if isinstance(item, dict):
            name = str(
                item.get("name")
                or item.get("title")
                or item.get("entity")
                or item.get("item")
                or item.get("organization")
                or item.get("organization_name")
                or item.get("rule")
                or item.get("label")
                or ""
            ).strip()
            description = str(
                item.get("description")
                or item.get("summary")
                or item.get("info")
                or item.get("content")
                or item.get("detail")
                or item.get("note")
                or item.get("text")
                or ""
            ).strip()
            return name, description
        return str(item or "").strip(), ""

    @classmethod
    def _parse_chapter_summary_payload(
        cls,
        raw_value: Any,
        *,
        chapter_index: int,
        plot_id: int,
    ) -> dict[str, Any]:
        payload = cls._parse_json_dict(raw_value)
        if not payload:
            raise ValueError(f"plot_id={plot_id} chapter_index={chapter_index} raw_summary_json is not a valid JSON object")

        chapter_summary = str(payload.get("chapter_summary") or "").strip()
        if not chapter_summary:
            raise ValueError(f"plot_id={plot_id} chapter_index={chapter_index} missing chapter_summary")

        return {
            "chapter_summary": chapter_summary,
            "character": cls._strict_named_description_list(
                payload.get("character"),
                field_name="character",
                chapter_index=chapter_index,
                plot_id=plot_id,
            ),
            "special_existence": cls._strict_named_description_list(
                payload.get("special_existence"),
                field_name="special_existence",
                chapter_index=chapter_index,
                plot_id=plot_id,
            ),
            "organizations": cls._strict_named_description_list(
                payload.get("organizations") if payload.get("organizations") is not None else payload.get("origanizations"),
                field_name="organizations",
                chapter_index=chapter_index,
                plot_id=plot_id,
            ),
            "world_rules": cls._strict_named_description_list(
                payload.get("world_rules"),
                field_name="world_rules",
                chapter_index=chapter_index,
                plot_id=plot_id,
            ),
        }

    @classmethod
    def _strict_named_description_list(
        cls,
        raw_value: Any,
        *,
        field_name: str,
        chapter_index: int,
        plot_id: int,
    ) -> list[dict[str, str]]:
        if not isinstance(raw_value, list):
            raise ValueError(
                f"plot_id={plot_id} chapter_index={chapter_index} field={field_name} is not a list"
            )

        normalized: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for index, item in enumerate(raw_value):
            if not isinstance(item, dict):
                raise ValueError(
                    f"plot_id={plot_id} chapter_index={chapter_index} field={field_name}[{index}] is not an object"
                )
            name, description = cls._coerce_named_description_pair(item)
            if not name:
                raise ValueError(
                    f"plot_id={plot_id} chapter_index={chapter_index} field={field_name}[{index}] missing name"
                )
            if not description:
                raise ValueError(
                    f"plot_id={plot_id} chapter_index={chapter_index} field={field_name}[{index}] missing description"
                )
            signature = (name, description)
            if signature in seen:
                continue
            normalized.append({"name": name, "description": description})
            seen.add(signature)
        return normalized

    @classmethod
    def _normalize_name_token(cls, value: str) -> str:
        return re.sub(r"\s+", "", str(value or "").strip()).lower()

    @classmethod
    def _split_name_and_aliases(cls, raw_name: str) -> tuple[str, list[str]]:
        text = str(raw_name or "").strip()
        if not text:
            return "", []
        match = re.match(r"^(.*?)[（(]([^（）()]*)[）)]\s*$", text)
        if not match:
            return text, []
        primary = str(match.group(1) or "").strip() or text
        alias_block = str(match.group(2) or "").strip()
        aliases = [
            item.strip()
            for item in re.split(r"[、/／,，|｜]+", alias_block)
            if str(item or "").strip()
        ]
        deduped: list[str] = []
        seen: set[str] = set()
        for item in aliases:
            key = cls._normalize_name_token(item)
            if not key or key in seen or key == cls._normalize_name_token(primary):
                continue
            deduped.append(item)
            seen.add(key)
        return primary, deduped

    @classmethod
    def _format_display_name(cls, primary_name: str, aliases: list[str]) -> str:
        clean_primary = str(primary_name or "").strip()
        clean_aliases = [
            item
            for item in aliases
            if cls._normalize_name_token(item) and cls._normalize_name_token(item) != cls._normalize_name_token(clean_primary)
        ]
        if clean_primary and clean_aliases:
            return f"{clean_primary}（{'/'.join(clean_aliases)}）"
        return clean_primary or (clean_aliases[0] if clean_aliases else "")

    @classmethod
    def _merge_plot_named_entries(cls, chapters: list[PlotChapterItem], field_name: str) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        for chapter in sorted(chapters, key=lambda item: item.chapter_index):
            payload = chapter.parsed_summary_payload or {}
            raw_value = payload.get(field_name)
            if raw_value is None and field_name == "origanizations":
                raw_value = payload.get("organizations")
            elif raw_value is None and field_name == "organizations":
                raw_value = payload.get("origanizations")
            entries = cls._normalize_named_description_entries(raw_value)
            for entry in entries:
                raw_name = str(entry.get("name") or "").strip()
                description = str(entry.get("description") or "").strip()
                primary_name, aliases = cls._split_name_and_aliases(raw_name)
                base_name = primary_name or raw_name
                candidate_names = [base_name, *aliases]
                candidate_tokens = {
                    cls._normalize_name_token(item)
                    for item in candidate_names
                    if cls._normalize_name_token(item)
                }
                matched: dict[str, Any] | None = None
                for item in merged:
                    existing_tokens = set(item.get("_tokens", set()))
                    if existing_tokens & candidate_tokens:
                        matched = item
                        break
                if matched is None:
                    matched = {
                        "primary_name": base_name,
                        "aliases": [],
                        "records": [],
                        "_tokens": set(),
                    }
                    merged.append(matched)
                existing_aliases = list(matched.get("aliases", []))
                for candidate in [matched.get("primary_name")] + candidate_names:
                    text = str(candidate or "").strip()
                    token = cls._normalize_name_token(text)
                    if not token:
                        continue
                    if not any(cls._normalize_name_token(alias) == token for alias in existing_aliases):
                        existing_aliases.append(text)
                    matched["_tokens"].add(token)
                matched["aliases"] = existing_aliases
                record = [int(chapter.chapter_index), description]
                if record not in matched["records"]:
                    matched["records"].append(record)

        final_rows: list[dict[str, Any]] = []
        for item in merged:
            aliases = list(item.get("aliases", []))
            primary_name = str(item.get("primary_name") or "").strip()
            records = sorted(
                [[int(record[0]), str(record[1] or "").strip()] for record in item.get("records", [])],
                key=lambda record: (int(record[0]), str(record[1])),
            )
            final_rows.append(
                {
                    "name": cls._format_display_name(primary_name, aliases),
                    "aliases": aliases,
                    "records": records,
                }
            )
        return final_rows

    @classmethod
    def _extract_named_entry_names(cls, raw_value: Any) -> list[str]:
        return [item["name"] for item in cls._normalize_named_description_entries(raw_value) if item.get("name")]

    @classmethod
    def _extract_world_rules_from_chapter_summary_payload(cls, raw_value: Any) -> list[str]:
        source = raw_value
        if isinstance(source, dict):
            source = [{"name": key, "description": item} for key, item in source.items()]
        elif isinstance(source, str):
            source = [source]
        if not isinstance(source, list):
            return []

        normalized: list[str] = []
        seen: set[str] = set()
        for item in source:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("title") or item.get("rule") or item.get("label") or "").strip()
                description = str(
                    item.get("description")
                    or item.get("summary")
                    or item.get("info")
                    or item.get("content")
                    or item.get("detail")
                    or item.get("note")
                    or item.get("text")
                    or ""
                ).strip()
                text = f"{name}：{description}" if name and description else name or description
            else:
                text = str(item or "").strip()
            if not text or text in seen:
                continue
            normalized.append(text)
            seen.add(text)
        return normalized

    def _build_step1_fallback(self, group: PlotSourceGroup) -> dict[str, Any]:
        all_characters: list[str] = []
        all_factions: list[str] = []
        world_rules: list[str] = []
        for chapter in group.chapters:
            chapter_summary_payload = self._parse_json_dict(chapter.raw_summary_json_text)
            if chapter_summary_payload:
                all_characters.extend(self._extract_named_entry_names(chapter_summary_payload.get("character")))
                all_factions.extend(
                    self._extract_named_entry_names(
                        chapter_summary_payload.get("organizations")
                        if chapter_summary_payload.get("organizations") is not None
                        else chapter_summary_payload.get("origanizations")
                    )
                )
                world_rules.extend(
                    self._extract_world_rules_from_chapter_summary_payload(chapter_summary_payload.get("world_rules"))
                )
            else:
                all_characters.extend(
                    [line.split("：", 1)[0].strip() for line in str(chapter.character_text or "").splitlines() if line.strip()]
                )
                all_factions.extend(
                    [
                        line.split("：", 1)[0].strip()
                        for line in str(chapter.organizations_text or "").splitlines()
                        if line.strip()
                    ]
                )
                world_rules.extend([line.strip() for line in str(chapter.world_rules_text or "").splitlines() if line.strip()])

        key_entities = {
            "protagonists": [],
            "important_characters": self._normalize_str_list(all_characters),
            "organizations": self._normalize_str_list(all_factions),
            "characters_who_appear": self._normalize_str_list(all_characters),
            "factions_who_appear": self._normalize_str_list(all_factions),
        }
        metadata = {
            "key_entities": key_entities,
            "character_presence": [
                {
                    "name": name,
                    "aliases": [],
                    "dialogue_presence": "",
                    "importance": "important",
                }
                for name in self._normalize_str_list(all_characters)
            ],
            "faction_presence": [
                {
                    "name": name,
                    "aliases": [],
                    "importance": "important",
                }
                for name in self._normalize_str_list(all_factions)
            ],
            "interaction_highlights": [],
            "reader_sensitive_moments": [],
            "plot_progression": [],
            "plot_facts": {
                "foreshadowing": [],
                "world_rules": self._normalize_str_list(world_rules),
                "major_rewards": [],
                "major_losses": [],
            },
            "portrait_deltas": {
                "characters": [],
                "factions": [],
            },
        }
        return {
            "plot_title": f"情节{group.plot_id}",
            "summary": f"该情节的 Step1 摘要生成失败，待补跑。章节范围：{group.start_chapter}-{group.end_chapter}。",
            "metadata": metadata,
            "raw_step1_json": {
                "plot_title": f"情节{group.plot_id}",
                "summary": "",
                "plot_progression": [],
                "key_entities": key_entities,
                "character_presence": metadata["character_presence"],
                "faction_presence": metadata["faction_presence"],
            },
        }

    def _build_step1_metadata(self, parsed: dict[str, Any]) -> dict[str, Any]:
        return {
            "key_entities": self._normalize_key_entities(parsed.get("key_entities")),
            "character_presence": self._normalize_character_presence(parsed.get("character_presence")),
            "faction_presence": self._normalize_faction_presence(parsed.get("faction_presence")),
            "interaction_highlights": [],
            "reader_sensitive_moments": [],
            "plot_progression": self._normalize_plot_progression(parsed.get("plot_progression")),
            "plot_facts": {
                "foreshadowing": [],
                "world_rules": [],
                "major_rewards": [],
                "major_losses": [],
            },
            "portrait_deltas": {
                "characters": [],
                "factions": [],
            },
        }

    def _merge_step2_metadata(self, step1_metadata: dict[str, Any], step2_parsed: dict[str, Any]) -> dict[str, Any]:
        merged = dict(step1_metadata)
        merged["character_presence"] = self._merge_presence_entries(
            base_entries=step1_metadata.get("character_presence", []),
            detail_entries=self._normalize_character_presence(step2_parsed.get("character_presence")),
        )
        merged["faction_presence"] = self._merge_presence_entries(
            base_entries=step1_metadata.get("faction_presence", []),
            detail_entries=self._normalize_faction_presence(step2_parsed.get("faction_presence")),
        )
        merged["interaction_highlights"] = self._normalize_interaction_highlights(step2_parsed.get("interaction_highlights"))
        merged["reader_sensitive_moments"] = self._normalize_reader_sensitive_moments(
            step2_parsed.get("reader_sensitive_moments")
        )
        merged["plot_facts"] = self._normalize_plot_facts(
            step2_parsed.get("plot_facts"),
            step2_parsed.get("crucial_information"),
        )
        merged["portrait_deltas"] = self._normalize_portrait_deltas(step2_parsed.get("portrait_deltas"))
        return merged

    def retry_step1_for_plot(self, book_id: int, plot_id: int, *, background: bool) -> bool:
        row = self._load_plot_row(book_id, plot_id)
        if not row:
            return False
        if str(row.get("step1_status") or "") != "error":
            return False
        if background and _normalize_retry_count(row.get("step1_retry_count")) >= 10:
            return False
        group = self._load_single_group_sync(book_id, plot_id)
        if group is None:
            return False
        step1_prompt = PLOT_ANALYSIS_STEP1_PROMPT.format(
            chapter_count=len(group.chapters),
            start_chapter=group.start_chapter,
            end_chapter=group.end_chapter,
            chapter_data=group.chapter_data,
        )
        if background:
            step1_parsed, step1_error = asyncio.run(self._invoke_and_parse_step1_with_retry(step1_prompt, plot_id))
        else:
            step1_parsed, step1_error = asyncio.run(
                self._invoke_and_parse_manual_error_retry(
                    prompt=step1_prompt,
                    plot_id=plot_id,
                    step_label="Step1",
                )
            )
        if step1_parsed is None:
            retry_count = _normalize_retry_count(row.get("step1_retry_count"))
            if background:
                retry_count += 1
            next_retry_at = None
            if background and retry_count < 10:
                next_retry_at = _utcnow() + dt.timedelta(seconds=_retry_delay_seconds(retry_count))
            fallback = self._build_step1_fallback(group)
            result = PlotAnalysisResult(
                plot_id=group.plot_id,
                start_chapter=group.start_chapter,
                end_chapter=group.end_chapter,
                title=fallback["plot_title"],
                plot_summary=fallback["summary"],
                metadata=fallback["metadata"],
                search_content=self._build_search_content(
                    title=fallback["plot_title"],
                    plot_summary=fallback["summary"],
                    metadata_obj=fallback["metadata"],
                    chapter_data=group.chapter_data,
                    start_chapter=group.start_chapter,
                    end_chapter=group.end_chapter,
                ),
                raw_plot_json={"step1": None, "step2": None},
                raw_plot_step1_json=fallback["raw_step1_json"],
                raw_plot_step2_json=None,
                step1_status="error",
                step1_retry_count=retry_count,
                step1_last_error=step1_error,
                step1_next_retry_at=next_retry_at,
                step2_status=str(row.get("step2_status") or "pending"),
                step2_retry_count=_normalize_retry_count(row.get("step2_retry_count")),
                step2_last_error=str(row.get("step2_last_error") or ""),
                step2_next_retry_at=_parse_datetime_value(row.get("step2_next_retry_at")),
            )
            self._upsert_plot_result(book_id, result)
            if background and retry_count < 10:
                self._schedule_background_retry(
                    step="step1",
                    book_id=book_id,
                    plot_id=plot_id,
                    retry_count=retry_count,
                    next_retry_at=next_retry_at,
                )
            return False

        metadata_obj = self._build_step1_metadata(step1_parsed)
        title = str(step1_parsed.get("plot_title") or f"情节{plot_id}").strip()
        plot_summary = str(step1_parsed.get("summary") or "").strip()
        result = PlotAnalysisResult(
            plot_id=group.plot_id,
            start_chapter=group.start_chapter,
            end_chapter=group.end_chapter,
            title=title,
            plot_summary=plot_summary,
            metadata=metadata_obj,
            search_content=self._build_search_content(
                title=title,
                plot_summary=plot_summary,
                metadata_obj=metadata_obj,
                chapter_data=group.chapter_data,
                start_chapter=group.start_chapter,
                end_chapter=group.end_chapter,
            ),
            raw_plot_json={"step1": step1_parsed, "step2": None},
            raw_plot_step1_json=step1_parsed,
            raw_plot_step2_json=None,
            step1_status="success",
            step1_retry_count=0,
            step1_last_error="",
            step1_next_retry_at=None,
            step2_status="pending",
            step2_retry_count=0,
            step2_last_error="",
            step2_next_retry_at=None,
        )
        self._upsert_plot_result(book_id, result)
        return self.retry_step2_for_plot(book_id, plot_id, background=background)

    def retry_step2_for_plot(self, book_id: int, plot_id: int, *, background: bool) -> bool:
        row = self._load_plot_row(book_id, plot_id)
        if not row:
            return False
        if str(row.get("step1_status") or "") != "success":
            return False
        if str(row.get("step2_status") or "") == "success":
            return True
        if background and _normalize_retry_count(row.get("step2_retry_count")) >= 10:
            return False
        group = self._load_single_group_sync(book_id, plot_id)
        if group is None:
            return False
        step1_raw = self._parse_json_dict(row.get("raw_plot_step1_json"))
        if not step1_raw:
            step1_raw = self._rebuild_step1_json_from_row(row)
        if not step1_raw:
            return False
        if background:
            step2_parsed, step2_error = asyncio.run(self._invoke_and_parse_step2_with_retry(group, step1_raw))
        else:
            step2_prompt = PLOT_ANALYSIS_STEP2_PROMPT.format(
                step1_data=json.dumps(step1_raw, ensure_ascii=False),
                chapter_data=group.chapter_data,
            )
            step2_parsed, step2_error = asyncio.run(
                self._invoke_and_parse_manual_error_retry(
                    prompt=step2_prompt,
                    plot_id=plot_id,
                    step_label="Step2",
                )
            )
        if step2_parsed is None:
            retry_count = _normalize_retry_count(row.get("step2_retry_count"))
            if background:
                retry_count += 1
            next_retry_at = None
            if background and retry_count < 10:
                next_retry_at = _utcnow() + dt.timedelta(seconds=_retry_delay_seconds(retry_count))
            result = PlotAnalysisResult(
                plot_id=plot_id,
                start_chapter=group.start_chapter,
                end_chapter=group.end_chapter,
                title=str(row.get("title") or step1_raw.get("plot_title") or f"情节{plot_id}").strip(),
                plot_summary=str(row.get("plot_summary") or step1_raw.get("summary") or "").strip(),
                metadata=self._parse_json_dict(row.get("metadata")) or self._build_step1_metadata(step1_raw),
                search_content=str(row.get("search_content") or ""),
                raw_plot_json=self._parse_json_dict(row.get("raw_plot_json")) or {"step1": step1_raw, "step2": None},
                raw_plot_step1_json=step1_raw,
                raw_plot_step2_json=self._parse_json_dict(row.get("raw_plot_step2_json")) or None,
                step1_status="success",
                step1_retry_count=0,
                step1_last_error="",
                step1_next_retry_at=None,
                step2_status="error",
                step2_retry_count=retry_count,
                step2_last_error=step2_error,
                step2_next_retry_at=next_retry_at,
            )
            self._upsert_plot_result(book_id, result)
            if background and retry_count < 10:
                self._schedule_background_retry(
                    step="step2",
                    book_id=book_id,
                    plot_id=plot_id,
                    retry_count=retry_count,
                    next_retry_at=next_retry_at,
                )
            return False

        metadata = self._parse_json_dict(row.get("metadata"))
        if not metadata:
            metadata = self._build_step1_metadata(step1_raw)
        merged_metadata = self._merge_step2_metadata(metadata, step2_parsed)
        title = str(row.get("title") or step1_raw.get("plot_title") or f"情节{plot_id}").strip()
        plot_summary = str(row.get("plot_summary") or step1_raw.get("summary") or "").strip()
        search_content = self._build_search_content(
            title=title,
            plot_summary=plot_summary,
            metadata_obj=merged_metadata,
            chapter_data=group.chapter_data,
            start_chapter=group.start_chapter,
            end_chapter=group.end_chapter,
        )
        raw_plot_json = {
            "step1": step1_raw,
            "step2": step2_parsed,
        }
        self._update_plot_step2_success(
            plot_row_id=int(row.get("id") or 0),
            metadata=merged_metadata,
            search_content=search_content,
            raw_plot_json=raw_plot_json,
            raw_plot_step2_json=step2_parsed,
        )
        if background:
            VolumeSegmentationEngine(
                if_exists_mode="overwrite",
                volume_record_if_exists_mode="overwrite",
            ).run(book_id)
        return True

    def _load_single_group_sync(self, book_id: int, plot_id: int) -> PlotSourceGroup | None:
        rows = self._load_plot_group_rows(book_id, plot_id)
        return self._build_plot_group(book_id, plot_id, rows)

    @staticmethod
    def _parse_json_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        text = str(value or "").strip()
        if not text:
            return {}
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
        if fenced:
            text = fenced.group(1).strip()
        if text.startswith("{") and text.endswith("}"):
            candidate = text
        else:
            matched = re.search(r"\{[\s\S]*\}", text)
            candidate = matched.group(0).strip() if matched else text
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _rebuild_step1_json_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        metadata = self._parse_json_dict(row.get("metadata"))
        if not metadata:
            return {}
        return {
            "plot_title": str(row.get("title") or "").strip(),
            "summary": str(row.get("plot_summary") or "").strip(),
            "plot_progression": metadata.get("plot_progression", []),
            "key_entities": metadata.get("key_entities", {}),
            "character_presence": metadata.get("character_presence", []),
            "faction_presence": metadata.get("faction_presence", []),
        }

    def _update_plot_step2_success(
        self,
        *,
        plot_row_id: int,
        metadata: dict[str, Any],
        search_content: str,
        raw_plot_json: dict[str, Any],
        raw_plot_step2_json: dict[str, Any],
    ) -> None:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE book_plots
                    SET metadata = %s,
                        search_content = %s,
                        raw_plot_json = %s,
                        raw_plot_step2_json = %s,
                        step2_status = 'success',
                        step2_retry_count = 0,
                        step2_last_error = NULL,
                        step2_next_retry_at = NULL
                    WHERE id = %s
                    """,
                    (
                        json.dumps(metadata, ensure_ascii=False),
                        search_content,
                        json.dumps(raw_plot_json, ensure_ascii=False),
                        json.dumps(raw_plot_step2_json, ensure_ascii=False),
                        plot_row_id,
                    ),
                )

    def _update_plot_step2_failure(
        self,
        *,
        plot_row_id: int,
        book_id: int,
        retry_count: int,
        error_message: str,
        next_retry_at: dt.datetime | None,
    ) -> None:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE book_plots
                    SET step2_status = 'error',
                        step2_retry_count = %s,
                        step2_last_error = %s,
                        step2_next_retry_at = %s
                    WHERE id = %s AND book_id = %s
                    """,
                    (
                        retry_count,
                        error_message or None,
                        next_retry_at.strftime("%Y-%m-%d %H:%M:%S") if next_retry_at else None,
                        plot_row_id,
                        book_id,
                    ),
                )

    def _upsert_plot_result(self, book_id: int, result: PlotAnalysisResult) -> int:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id
                    FROM book_plots
                    WHERE book_id = %s AND plot_id = %s
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (book_id, result.plot_id),
                )
                existing = cursor.fetchone() or {}
                plot_row_id = int(existing.get("id") or 0)
                if plot_row_id > 0:
                    cursor.execute(
                        """
                        UPDATE book_plots
                        SET volume_id = %s,
                            start_chapter_index = %s,
                            end_chapter_index = %s,
                            title = %s,
                            status = %s,
                            plot_summary = %s,
                            `character` = %s,
                            special_existence = %s,
                            origanizations = %s,
                            world_rules = %s
                        WHERE id = %s
                        """,
                        (
                            0,
                            result.start_chapter,
                            result.end_chapter,
                            result.title,
                            result.status,
                            result.plot_summary,
                            json.dumps(result.character, ensure_ascii=False),
                            json.dumps(result.special_existence, ensure_ascii=False),
                            json.dumps(result.origanizations, ensure_ascii=False),
                            json.dumps(result.world_rules, ensure_ascii=False),
                            plot_row_id,
                        ),
                    )
                    return plot_row_id

                cursor.execute(
                    """
                    INSERT INTO book_plots
                    (
                        book_id,
                        volume_id,
                        plot_id,
                        start_chapter_index,
                        end_chapter_index,
                        title,
                        status,
                        plot_summary,
                        `character`,
                        special_existence,
                        origanizations,
                        world_rules
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        book_id,
                        0,
                        result.plot_id,
                        result.start_chapter,
                        result.end_chapter,
                        result.title,
                        result.status,
                        result.plot_summary,
                        json.dumps(result.character, ensure_ascii=False),
                        json.dumps(result.special_existence, ensure_ascii=False),
                        json.dumps(result.origanizations, ensure_ascii=False),
                        json.dumps(result.world_rules, ensure_ascii=False),
                    ),
                )
                return int(cursor.lastrowid or 0)

    @staticmethod
    def _normalize_str_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = str(item).strip()
            if not text or text in seen:
                continue
            normalized.append(text)
            seen.add(text)
        return normalized

    @classmethod
    def _normalize_key_entities(cls, raw_key_entities: Any) -> dict[str, list[str]]:
        source = raw_key_entities if isinstance(raw_key_entities, dict) else {}
        protagonists = cls._normalize_str_list(source.get("protagonists"))
        important_characters = cls._normalize_str_list(source.get("important_characters")) or cls._normalize_str_list(
            source.get("new_characters")
        )
        organizations = cls._normalize_str_list(source.get("organizations")) or cls._normalize_str_list(
            source.get("factions")
        )
        characters_who_appear = cls._normalize_str_list(
            source.get("characters_who_appear")
        ) or cls._normalize_str_list(source.get("all_characters"))
        factions_who_appear = cls._normalize_str_list(
            source.get("factions_who_appear")
        ) or cls._normalize_str_list(source.get("all_factions")) or organizations
        return {
            "protagonists": protagonists,
            "important_characters": important_characters,
            "organizations": organizations,
            "characters_who_appear": characters_who_appear,
            "factions_who_appear": factions_who_appear,
        }

    @classmethod
    def _normalize_character_presence(cls, raw_value: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_value, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in raw_value:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("character_name") or "").strip()
            if not name:
                continue
            normalized.append(
                {
                    "name": name,
                    "aliases": cls._normalize_str_list(item.get("aliases")),
                    "role_in_plot": str(item.get("role_in_plot") or "").strip(),
                    "major_actions": cls._normalize_str_list(item.get("major_actions")),
                    "dialogue_presence": str(item.get("dialogue_presence") or "").strip(),
                    "importance": str(item.get("importance") or "").strip(),
                }
            )
        return normalized

    @classmethod
    def _normalize_faction_presence(cls, raw_value: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_value, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in raw_value:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("faction_name") or "").strip()
            if not name:
                continue
            normalized.append(
                {
                    "name": name,
                    "aliases": cls._normalize_str_list(item.get("aliases")),
                    "role_in_plot": str(item.get("role_in_plot") or "").strip(),
                    "members_involved": cls._normalize_str_list(item.get("members_involved")),
                    "major_actions": cls._normalize_str_list(item.get("major_actions")),
                    "status_change": cls._normalize_str_list(item.get("status_change")),
                    "importance": str(item.get("importance") or "").strip(),
                }
            )
        return normalized

    @classmethod
    def _merge_presence_entries(
        cls,
        *,
        base_entries: Any,
        detail_entries: Any,
    ) -> list[dict[str, Any]]:
        merged_by_name: dict[str, dict[str, Any]] = {}
        for source in (base_entries or [], detail_entries or []):
            if not isinstance(source, list):
                continue
            for item in source:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                entry = merged_by_name.setdefault(name, {"name": name})
                for key, value in item.items():
                    if key == "name":
                        continue
                    if isinstance(value, list):
                        existing = entry.get(key, [])
                        if not isinstance(existing, list):
                            existing = []
                        entry[key] = cls._normalize_str_list(existing + value)
                    elif str(value or "").strip():
                        entry[key] = value
        return list(merged_by_name.values())

    @classmethod
    def _normalize_interaction_highlights(cls, raw_value: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_value, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in raw_value:
            if not isinstance(item, dict):
                continue
            summary = str(item.get("summary") or item.get("desc") or "").strip()
            participants = cls._normalize_str_list(
                item.get("participants") if item.get("participants") is not None else item.get("characters")
            )
            if not summary and not participants:
                continue
            normalized.append(
                {
                    "participants": participants,
                    "type": str(item.get("type") or item.get("interaction_type") or "").strip(),
                    "chapter_range": str(item.get("chapter_range") or "").strip(),
                    "summary": summary,
                    "importance": str(item.get("importance") or "").strip(),
                }
            )
        return normalized

    @classmethod
    def _normalize_reader_sensitive_moments(cls, raw_value: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_value, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in raw_value:
            if not isinstance(item, dict):
                continue
            summary = str(item.get("summary") or item.get("desc") or "").strip()
            participants = cls._normalize_str_list(item.get("participants"))
            if not summary and not participants:
                continue
            normalized.append(
                {
                    "chapter_range": str(item.get("chapter_range") or "").strip(),
                    "participants": participants,
                    "tag": str(item.get("tag") or item.get("type") or "").strip(),
                    "summary": summary,
                }
            )
        return normalized

    @classmethod
    def _normalize_plot_progression(cls, raw_value: Any) -> list[dict[str, str]]:
        if not isinstance(raw_value, list):
            return []
        normalized: list[dict[str, str]] = []
        for item in raw_value:
            if not isinstance(item, dict):
                continue
            chapter_range = str(item.get("chapter_range") or "").strip()
            event = str(item.get("event") or "").strip()
            if not chapter_range and not event:
                continue
            normalized.append(
                {
                    "chapter_range": chapter_range,
                    "event": event,
                }
            )
        return normalized

    @classmethod
    def _normalize_plot_facts(
        cls,
        raw_plot_facts: Any,
        raw_crucial_information: Any,
    ) -> dict[str, list[str]]:
        normalized = {
            "foreshadowing": [],
            "world_rules": [],
            "major_rewards": [],
            "major_losses": [],
        }
        source = raw_plot_facts if isinstance(raw_plot_facts, dict) else {}
        normalized["foreshadowing"] = cls._normalize_str_list(source.get("foreshadowing"))
        normalized["world_rules"] = cls._normalize_str_list(source.get("world_rules"))
        normalized["major_rewards"] = cls._normalize_str_list(source.get("major_rewards"))
        normalized["major_losses"] = cls._normalize_str_list(source.get("major_losses"))

        legacy = raw_crucial_information if isinstance(raw_crucial_information, dict) else {}
        if not normalized["foreshadowing"]:
            normalized["foreshadowing"] = cls._normalize_str_list(legacy.get("foreshadowing"))
        if not normalized["major_rewards"]:
            rewards = cls._normalize_str_list(legacy.get("items_obtained"))
            rewards.extend(
                f"技能成长: {item}"
                for item in cls._normalize_str_list(legacy.get("skills_learned"))
            )
            normalized["major_rewards"] = cls._normalize_str_list(rewards)
        if not normalized["major_losses"]:
            normalized["major_losses"] = [
                f"状态变化: {item}"
                for item in cls._normalize_str_list(legacy.get("status_change"))
            ]
        return normalized

    @classmethod
    def _normalize_portrait_deltas(cls, raw_value: Any) -> dict[str, list[dict[str, Any]]]:
        normalized = {
            "characters": [],
            "factions": [],
        }
        if not isinstance(raw_value, dict):
            return normalized

        characters = raw_value.get("characters")
        if isinstance(characters, list):
            for item in characters:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("character_name") or "").strip()
                if not name:
                    continue
                normalized["characters"].append(
                    {
                        "name": name,
                        "status_change": cls._normalize_str_list(item.get("status_change")),
                        "items": cls._normalize_str_list(item.get("items")),
                        "skills": cls._normalize_str_list(item.get("skills")),
                        "relationship_updates": cls._normalize_str_list(item.get("relationship_updates")),
                        "speech_style_notes": cls._normalize_str_list(item.get("speech_style_notes")),
                    }
                )

        factions = raw_value.get("factions")
        if isinstance(factions, list):
            for item in factions:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("faction_name") or "").strip()
                if not name:
                    continue
                normalized["factions"].append(
                    {
                        "name": name,
                        "status_change": cls._normalize_str_list(item.get("status_change")),
                        "leadership_or_membership_updates": cls._normalize_str_list(
                            item.get("leadership_or_membership_updates")
                        ),
                        "alliances_or_hostilities": cls._normalize_str_list(
                            item.get("alliances_or_hostilities")
                        ),
                        "resources_or_territory_changes": cls._normalize_str_list(
                            item.get("resources_or_territory_changes")
                        ),
                    }
                )

        return normalized

    def _rewrite_book_plots(self, book_id: int, results: list[PlotAnalysisResult]) -> None:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM book_plots WHERE book_id = %s", (book_id,))
                next_id = _next_id(cursor, "book_plots")
                total = len(results)
                done = 0
                if total:
                    logger.warning(
                        "[分析进度][book_plots写入] %s %d/%d",
                        _progress_bar(0, total),
                        0,
                        total,
                    )
                for result in results:
                    cursor.execute(
                        """
                        INSERT INTO book_plots
                        (
                            id,
                            book_id,
                            volume_id,
                            plot_id,
                            start_chapter_index,
                            end_chapter_index,
                            title,
                            plot_summary,
                            search_content,
                            metadata,
                            step1_status,
                            step2_status,
                            step2_retry_count,
                            step2_last_error,
                            step2_next_retry_at,
                            raw_plot_step1_json,
                            raw_plot_step2_json,
                            raw_plot_json,
                            vector_id
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            next_id,
                            book_id,
                            0,
                            result.plot_id,
                            result.start_chapter,
                            result.end_chapter,
                            result.title,
                            result.plot_summary,
                            result.search_content,
                            json.dumps(result.metadata, ensure_ascii=False),
                            result.step1_status,
                            result.step2_status,
                            result.step2_retry_count,
                            result.step2_last_error or None,
                            result.step2_next_retry_at.strftime("%Y-%m-%d %H:%M:%S")
                            if result.step2_next_retry_at
                            else None,
                            json.dumps(result.raw_plot_step1_json, ensure_ascii=False),
                            json.dumps(result.raw_plot_step2_json, ensure_ascii=False)
                            if result.raw_plot_step2_json is not None
                            else None,
                            json.dumps(result.raw_plot_json, ensure_ascii=False),
                            None,
                        ),
                    )
                    next_id += 1
                    done += 1
                    logger.warning(
                        "[分析进度][book_plots写入] %s %d/%d",
                        _progress_bar(done, total),
                        done,
                        total,
                    )

    def _count_existing_book_plots(self, book_id: int) -> int:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS cnt FROM book_plots WHERE book_id = %s", (book_id,))
                row = cursor.fetchone() or {}
        return int(row.get("cnt") or 0)

    def _resolve_if_exists_mode(self) -> str:
        raw_mode = str(os.getenv(BOOK_PLOTS_IF_EXISTS_ENV_VAR, "overwrite")).strip().lower()
        if raw_mode in {"skip", "keep", "preserve"}:
            return "skip"
        return "overwrite"
