from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from agent.graph import build_llm
from dotenv import load_dotenv
import pydantic
import pymysql

from rag.prompt import VOLUME_GENERATION_PROMPT


ROOT_DIR = Path(__file__).resolve().parents[1]
logger = logging.getLogger(__name__)
BOOK_VOLUME_RECORDS_IF_EXISTS_ENV_VAR = "BOOK_VOLUME_RECORDS_IF_EXISTS"


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
    if table_name != "book_volumes":
        raise ValueError(f"Unsupported table name: {table_name}")
    cursor.execute(f"SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM `{table_name}`")
    row = cursor.fetchone() or {}
    return int(row.get("next_id") or 1)


class PlotVolumeItem(pydantic.BaseModel):
    volume_id: int
    plot_id: int
    title: str = ""
    plot_summary: str = ""
    start_chapter_index: int
    end_chapter_index: int


class VolumeSourceGroup(pydantic.BaseModel):
    volume_id: int
    plots: list[PlotVolumeItem]
    plot_summaries_text: str
    start_plot_id: int
    end_plot_id: int
    start_chapter_index: int
    end_chapter_index: int
    plot_count: int


class VolumeRecord(pydantic.BaseModel):
    volume_index: int
    title: str
    start_plot_index: int
    end_plot_index: int
    start_chapter_index: int
    end_chapter_index: int
    volume_summary: str
    time_span: str
    plot_count: int
    raw_volume_json: str


class VolumeRecordBuilder:
    def __init__(
        self,
        max_concurrency: int = 25,
        max_parse_retries: int = 10,
        if_exists_mode: str | None = None,
    ) -> None:
        self.max_concurrency = min(25, max(1, int(max_concurrency)))
        self.max_parse_retries = max(1, int(max_parse_retries))
        _load_runtime_env()
        self.llm_client = build_llm()
        self._api_semaphore = asyncio.Semaphore(self.max_concurrency)
        self.if_exists = if_exists_mode or self._resolve_if_exists_mode()

    def run(self, book_id: int) -> dict[str, Any]:
        existing_count = self._count_existing_book_volumes(book_id)
        logger.warning(
            "[分析进度][卷级摘要入库][book=%s] 预检查：book_volumes已有 %d 条，策略=%s。",
            book_id,
            existing_count,
            self.if_exists,
        )
        if existing_count > 0 and self.if_exists == "skip":
            logger.warning(
                "[分析进度][卷级摘要入库][book=%s] 跳过生成，保留既有 book_volumes。",
                book_id,
            )
            return {"book_id": book_id, "volume_count": existing_count, "skipped": True}

        groups = self._load_volume_groups(book_id)
        if not groups:
            logger.warning("[分析进度][卷级摘要入库][book=%s] 未检测到有效 volume_id，跳过。", book_id)
            if self.if_exists == "overwrite":
                self._rewrite_book_volumes(book_id, [])
            return {"book_id": book_id, "volume_count": 0}

        logger.warning(
            "[分析进度][卷级摘要入库][book=%s] 准备处理 %d 个卷分组。",
            book_id,
            len(groups),
        )
        records = asyncio.run(self._analyze_groups_concurrently(groups))
        self._rewrite_book_volumes(book_id, records)
        return {"book_id": book_id, "volume_count": len(records)}

    def _resolve_if_exists_mode(self) -> str:
        raw_mode = str(os.getenv(BOOK_VOLUME_RECORDS_IF_EXISTS_ENV_VAR, "skip")).strip().lower()
        if raw_mode in {"overwrite", "rebuild", "regen"}:
            return "overwrite"
        return "skip"

    def _count_existing_book_volumes(self, book_id: int) -> int:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS cnt FROM book_volumes WHERE book_id = %s", (book_id,))
                row = cursor.fetchone() or {}
        return int(row.get("cnt") or 0)

    def _load_volume_groups(self, book_id: int) -> list[VolumeSourceGroup]:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT volume_id, plot_id, title, plot_summary, start_chapter_index, end_chapter_index
                    FROM book_plots
                    WHERE book_id = %s AND COALESCE(volume_id, 0) > 0 AND status = 'success'
                    ORDER BY volume_id ASC, plot_id ASC, id ASC
                    """,
                    (book_id,),
                )
                rows = list(cursor.fetchall() or [])

        grouped: dict[int, list[PlotVolumeItem]] = defaultdict(list)
        for row in rows:
            plot = PlotVolumeItem(
                volume_id=int(row.get("volume_id") or 0),
                plot_id=int(row.get("plot_id") or 0),
                title=str(row.get("title") or "").strip(),
                plot_summary=str(row.get("plot_summary") or "").strip(),
                start_chapter_index=int(row.get("start_chapter_index") or 0),
                end_chapter_index=int(row.get("end_chapter_index") or 0),
            )
            if plot.volume_id > 0 and plot.plot_id > 0:
                grouped[plot.volume_id].append(plot)

        groups: list[VolumeSourceGroup] = []
        for volume_id in sorted(grouped.keys()):
            plots = sorted(grouped[volume_id], key=lambda item: item.plot_id)
            if not plots:
                continue
            plot_summaries_text = "\n".join(
                f"Plot {plot.plot_id}: {plot.title}\n摘要: {plot.plot_summary}" for plot in plots
            )
            groups.append(
                VolumeSourceGroup(
                    volume_id=volume_id,
                    plots=plots,
                    plot_summaries_text=plot_summaries_text,
                    start_plot_id=plots[0].plot_id,
                    end_plot_id=plots[-1].plot_id,
                    start_chapter_index=plots[0].start_chapter_index,
                    end_chapter_index=plots[-1].end_chapter_index,
                    plot_count=len(plots),
                )
            )
        return groups

    async def _analyze_groups_concurrently(self, groups: list[VolumeSourceGroup]) -> list[VolumeRecord]:
        tasks = [self._analyze_single_group(group) for group in groups]
        total = len(tasks)
        done = 0
        logger.warning(
            "[分析进度][卷级摘要LLM] %s %d/%d",
            _progress_bar(0, total),
            0,
            total,
        )
        results: list[VolumeRecord] = []
        for task in asyncio.as_completed(tasks):
            record = await task
            results.append(record)
            done += 1
            logger.warning(
                "[分析进度][卷级摘要LLM] %s %d/%d",
                _progress_bar(done, total),
                done,
                total,
            )
        return sorted(results, key=lambda item: item.volume_index)

    async def _analyze_single_group(self, group: VolumeSourceGroup) -> VolumeRecord:
        prompt = VOLUME_GENERATION_PROMPT.format(
            start_plot_id=group.start_plot_id,
            end_plot_id=group.end_plot_id,
            plot_summaries_text=group.plot_summaries_text,
        )
        parsed, error_info = await self._invoke_and_parse_with_retry(prompt, group)
        return self._map_llm_response_to_record(parsed, group, error_info)

    async def _invoke_and_parse_with_retry(
        self, prompt: str, group: VolumeSourceGroup
    ) -> tuple[dict[str, Any], str]:
        for attempt in range(1, self.max_parse_retries + 1):
            try:
                async with self._api_semaphore:
                    raw_text = await self._invoke_llm(prompt)
                parsed = self._extract_json(raw_text)
                if isinstance(parsed, dict):
                    return parsed, ""
                raise ValueError("LLM JSON is not an object.")
            except Exception as exc:
                logger.error(
                    "[事故][卷级摘要LLM][book_volume=%s] 响应格式异常，attempt=%d/%d，error=%s",
                    group.volume_id,
                    attempt,
                    self.max_parse_retries,
                    exc,
                )
        error_info = f"LLM响应异常，重试{self.max_parse_retries}次后失败"
        return {}, error_info

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

    def _extract_json(self, text: str) -> dict[str, Any]:
        payload = text.strip()
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", payload, flags=re.IGNORECASE)
        if fenced:
            payload = fenced.group(1).strip()
        matched = re.search(r"\{[\s\S]*\}", payload)
        candidate = matched.group(0).strip() if matched else payload
        parsed = json.loads(candidate)
        if not isinstance(parsed, dict):
            raise ValueError("Invalid LLM JSON response for volume analysis.")
        return parsed

    def _map_llm_response_to_record(
        self,
        llm_json: dict[str, Any],
        group: VolumeSourceGroup,
        error_info: str,
    ) -> VolumeRecord:
        volume_title = str(llm_json.get("volume_title") or f"第{group.volume_id}卷").strip()
        volume_summary = str(llm_json.get("volume_summary") or "").strip()
        time_span = str(llm_json.get("time_span") or "").strip()

        if error_info:
            volume_title = f"第{group.volume_id}卷（异常）"
            volume_summary = f"卷级摘要生成异常：{error_info}"
            time_span = "异常"

        return VolumeRecord(
            volume_index=group.volume_id,
            title=volume_title,
            start_plot_index=group.start_plot_id,
            end_plot_index=group.end_plot_id,
            start_chapter_index=group.start_chapter_index,
            end_chapter_index=group.end_chapter_index,
            volume_summary=volume_summary,
            time_span=time_span,
            plot_count=group.plot_count,
            raw_volume_json=json.dumps(llm_json, ensure_ascii=False),
        )

    def _rewrite_book_volumes(self, book_id: int, records: list[VolumeRecord]) -> None:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM book_volumes WHERE book_id = %s", (book_id,))
                next_id = _next_id(cursor, "book_volumes")
                total = len(records)
                done = 0
                if total:
                    logger.warning(
                        "[分析进度][book_volumes写入] %s %d/%d",
                        _progress_bar(0, total),
                        0,
                        total,
                    )
                for record in records:
                    cursor.execute(
                        """
                        INSERT INTO book_volumes
                        (
                            id,
                            book_id,
                            volume_index,
                            title,
                            start_plot_index,
                            end_plot_index,
                            start_chapter_index,
                            end_chapter_index,
                            volume_summary,
                            time_span,
                            plot_count,
                            raw_volume_json
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            next_id,
                            book_id,
                            record.volume_index,
                            record.title,
                            record.start_plot_index,
                            record.end_plot_index,
                            record.start_chapter_index,
                            record.end_chapter_index,
                            record.volume_summary,
                            record.time_span,
                            record.plot_count,
                            record.raw_volume_json,
                        ),
                    )
                    next_id += 1
                    done += 1
                    logger.warning(
                        "[分析进度][book_volumes写入] %s %d/%d",
                        _progress_bar(done, total),
                        done,
                        total,
                    )
