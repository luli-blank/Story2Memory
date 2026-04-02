from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from agent.graph import build_llm
from dotenv import load_dotenv
import pydantic
import pymysql

from rag.createVolumes import VolumeRecordBuilder
from rag.prompt import PROMPT_B_VOLUME_STITCHING, VOLUME_SEGMENTATION_PROMPT


ROOT_DIR = Path(__file__).resolve().parents[1]
logger = logging.getLogger(__name__)
BOOK_VOLUMES_IF_EXISTS_ENV_VAR = "BOOK_VOLUMES_IF_EXISTS"


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


class PlotItem(pydantic.BaseModel):
    id: int
    title: str = ""
    plot_summary: str = ""
    special_existence: list[dict[str, Any]] = []
    origanizations: list[dict[str, Any]] = []
    world_rules: list[dict[str, Any]] = []


class VolumeChunk(pydantic.BaseModel):
    start_plot_id: int
    end_plot_id: int
    reason: str = ""


class MergeDecision(pydantic.BaseModel):
    should_merge: bool
    reason: str = ""


class VolumeSegmentationEngine:
    def __init__(
        self,
        batch_size: int = 10,
        max_iterations: int = 8,
        max_concurrency: int = 25,
        max_parse_retries: int = 10,
        if_exists_mode: str | None = None,
        volume_record_if_exists_mode: str | None = None,
    ) -> None:
        self.batch_size = max(1, int(batch_size))
        self.max_iterations = max(1, int(max_iterations))
        self.max_concurrency = min(25, max(1, int(max_concurrency)))
        self.max_parse_retries = max(1, int(max_parse_retries))
        self._api_semaphore: asyncio.Semaphore | None = None
        self._active_book_id = 0
        _load_runtime_env()
        self.llm_client = build_llm()
        self.if_exists = if_exists_mode or self._resolve_if_exists_mode()
        self.volume_record_if_exists_mode = volume_record_if_exists_mode

    def run(self, book_id: int) -> dict[str, Any]:
        logger.warning("[分析进度][book=%s] 开始卷级聚类...", book_id)
        total_plots, grouped_plots, skip_segmentation = self._volume_precheck(book_id)
        logger.warning(
            "[分析进度][卷级聚类][book=%s] 预检查：%s，策略=%s",
            book_id,
            "已完成，跳过聚类" if (skip_segmentation and self.if_exists == "skip") else "将执行聚类",
            self.if_exists,
        )
        if skip_segmentation and self.if_exists == "skip":
            logger.warning(
                "[分析进度][卷级聚类][book=%s] %s %d/%d",
                book_id,
                _progress_bar(grouped_plots, total_plots),
                grouped_plots,
                total_plots,
            )
            volume_record_stats = VolumeRecordBuilder(if_exists_mode=self.volume_record_if_exists_mode).run(book_id)
            return {
                "book_id": book_id,
                "volume_count": len(self._load_distinct_volumes(book_id)),
                "plot_count": total_plots,
                "skipped": True,
                "volume_records": volume_record_stats,
            }

        plots = self._load_plots(book_id)
        if not plots:
            self._clear_volume_ids(book_id)
            logger.warning("[分析进度][分卷][book=%s] 未检测到情节数据，已清空 volume_id。", book_id)
            return {"book_id": book_id, "volume_count": 0, "plot_count": 0}

        self._active_book_id = book_id
        logger.warning("[分析进度][分卷][book=%s] 准备处理 %d 个情节。", book_id, len(plots))
        chunks = asyncio.run(self._segment_plots(plots))
        self._write_volume_ids(book_id, chunks)
        volume_record_stats = VolumeRecordBuilder(if_exists_mode=self.volume_record_if_exists_mode).run(book_id)
        return {
            "book_id": book_id,
            "volume_count": len(chunks),
            "plot_count": len(plots),
            "volume_records": volume_record_stats,
        }

    def _resolve_if_exists_mode(self) -> str:
        raw_mode = str(os.getenv(BOOK_VOLUMES_IF_EXISTS_ENV_VAR, "skip")).strip().lower()
        if raw_mode in {"overwrite", "rebuild", "regen"}:
            return "overwrite"
        return "skip"

    def _volume_precheck(self, book_id: int) -> tuple[int, int, bool]:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        COUNT(*) AS total_count,
                        SUM(CASE WHEN COALESCE(volume_id, 0) <> 0 THEN 1 ELSE 0 END) AS grouped_count
                    FROM book_plots
                    WHERE book_id = %s AND COALESCE(plot_id, 0) > 0 AND status = 'success'
                    """,
                    (book_id,),
                )
                row = cursor.fetchone() or {}
        total_count = int(row.get("total_count") or 0)
        grouped_count = int(row.get("grouped_count") or 0)
        return total_count, grouped_count, total_count > 0 and grouped_count == total_count

    def _load_distinct_volumes(self, book_id: int) -> list[int]:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT volume_id
                    FROM book_plots
                    WHERE book_id = %s AND COALESCE(volume_id, 0) > 0 AND status = 'success'
                    ORDER BY volume_id ASC
                    """,
                    (book_id,),
                )
                rows = list(cursor.fetchall() or [])
        return [int(row.get("volume_id") or 0) for row in rows if int(row.get("volume_id") or 0) > 0]

    def _load_plots(self, book_id: int) -> list[PlotItem]:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT plot_id AS id, title, plot_summary, special_existence, origanizations, world_rules
                    FROM book_plots
                    WHERE book_id = %s AND COALESCE(plot_id, 0) > 0 AND status = 'success'
                    ORDER BY plot_id ASC, id ASC
                    """,
                    (book_id,),
                )
                rows = list(cursor.fetchall() or [])
        return [
            PlotItem(
                id=int(row.get("id") or 0),
                title=str(row.get("title") or "").strip(),
                plot_summary=str(row.get("plot_summary") or "").strip(),
                special_existence=self._parse_json_list(row.get("special_existence")),
                origanizations=self._parse_json_list(row.get("origanizations")),
                world_rules=self._parse_json_list(row.get("world_rules")),
            )
            for row in rows
            if int(row.get("id") or 0) > 0
        ]

    async def _segment_plots(self, plots: list[PlotItem]) -> list[VolumeChunk]:
        self._api_semaphore = asyncio.Semaphore(self.max_concurrency)
        stage_one_chunks = await self._stage_one_parallel(plots)
        if not stage_one_chunks:
            stage_one_chunks = [
                VolumeChunk(start_plot_id=plots[0].id, end_plot_id=plots[-1].id, reason="fallback")
            ]

        plot_map = {plot.id: plot for plot in plots}
        current_chunks = sorted(stage_one_chunks, key=lambda item: item.start_plot_id)
        iteration = 1
        while iteration <= self.max_iterations:
            reduced_chunks, merged_count = await self._stage_two_reduce(
                current_chunks,
                plot_map,
                iteration,
            )
            current_chunks = reduced_chunks
            if merged_count == 0:
                break
            iteration += 1
        return current_chunks

    async def _stage_one_parallel(self, plots: list[PlotItem]) -> list[VolumeChunk]:
        batches = [
            plots[start : start + self.batch_size]
            for start in range(0, len(plots), self.batch_size)
        ]
        tasks = [self._process_single_batch(batch) for batch in batches if batch]
        total = len(tasks)
        if total:
            logger.warning(
                "[分析进度][分卷-阶段1][book=%s] %s %d/%d",
                self._active_book_id,
                _progress_bar(0, total),
                0,
                total,
            )
        results = await asyncio.gather(*tasks) if tasks else []
        flat_chunks = [item for group in results for item in group]
        if total:
            logger.warning(
                "[分析进度][分卷-阶段1][book=%s] %s %d/%d",
                self._active_book_id,
                _progress_bar(total, total),
                total,
                total,
            )
        return sorted(flat_chunks, key=lambda item: item.start_plot_id)

    async def _process_single_batch(self, batch: list[PlotItem]) -> list[VolumeChunk]:
        plot_data_list = "\n".join(
            self._build_plot_volume_card(item) for item in batch
        )
        prompt = VOLUME_SEGMENTATION_PROMPT.format(plot_data_list=plot_data_list)
        raw_ranges = await self._stage_one_llm(prompt, batch)

        batch_start = batch[0].id
        batch_end = batch[-1].id
        chunks: list[VolumeChunk] = []
        for item in raw_ranges:
            start = int(item.get("start_plot_id") or item.get("start") or batch_start)
            end = int(item.get("end_plot_id") or item.get("end") or batch_end)
            start = max(batch_start, start)
            end = min(batch_end, end)
            if start > end:
                continue
            chunks.append(
                VolumeChunk(
                    start_plot_id=start,
                    end_plot_id=end,
                    reason=str(item.get("reason") or "").strip(),
                )
            )

        if not chunks:
            chunks = [VolumeChunk(start_plot_id=batch_start, end_plot_id=batch_end, reason="fallback")]
        return sorted(chunks, key=lambda item: item.start_plot_id)

    async def _stage_one_llm(self, prompt: str, batch: list[PlotItem]) -> list[dict[str, Any]]:
        if not batch:
            return []
        fallback = [
            {
                "start_plot_id": batch[0].id,
                "end_plot_id": batch[-1].id,
                "reason": "fallback",
            }
        ]

        for attempt in range(1, self.max_parse_retries + 1):
            try:
                response_text = await self._invoke_llm_with_limit(prompt)
                parsed = self._extract_json(response_text, expect_array=True)
                if isinstance(parsed, list):
                    normalized = [item for item in parsed if isinstance(item, dict)]
                    if normalized:
                        return normalized
            except Exception as exc:
                logger.error(
                    "[事故][分卷-阶段1][book=%s] 批次解析异常 attempt=%d/%d error=%s",
                    self._active_book_id,
                    attempt,
                    self.max_parse_retries,
                    exc,
                )
        return fallback

    async def _stage_two_reduce(
        self,
        chunks: list[VolumeChunk],
        plot_map: dict[int, PlotItem],
        iteration: int,
    ) -> tuple[list[VolumeChunk], int]:
        if len(chunks) <= 1:
            return chunks, 0

        pair_indices = list(range(0, len(chunks) - 1, 2))
        pair_tasks = [
            self._check_adjacent_pair(chunks[index], chunks[index + 1], plot_map)
            for index in pair_indices
        ]
        total = len(pair_tasks)
        logger.warning(
            "[分析进度][分卷-阶段2-R%d][book=%s] %s %d/%d",
            iteration,
            self._active_book_id,
            _progress_bar(0, total),
            0,
            total,
        )
        decisions = await asyncio.gather(*pair_tasks) if pair_tasks else []
        decisions_by_index = {pair_indices[idx]: decision for idx, decision in enumerate(decisions)}
        logger.warning(
            "[分析进度][分卷-阶段2-R%d][book=%s] %s %d/%d",
            iteration,
            self._active_book_id,
            _progress_bar(total, total),
            total,
            total,
        )

        merged_chunks: list[VolumeChunk] = []
        merged_count = 0
        index = 0
        while index < len(chunks):
            decision = decisions_by_index.get(index)
            if decision is not None and decision.should_merge:
                merged_chunks.append(
                    VolumeChunk(
                        start_plot_id=chunks[index].start_plot_id,
                        end_plot_id=chunks[index + 1].end_plot_id,
                        reason=decision.reason,
                    )
                )
                merged_count += 1
                index += 2
                continue
            merged_chunks.append(chunks[index])
            index += 1
        return merged_chunks, merged_count

    async def _check_adjacent_pair(
        self,
        current_chunk: VolumeChunk,
        next_chunk: VolumeChunk,
        plot_map: dict[int, PlotItem],
    ) -> MergeDecision:
        tail_item = plot_map.get(current_chunk.end_plot_id, PlotItem(id=current_chunk.end_plot_id))
        head_item = plot_map.get(next_chunk.start_plot_id, PlotItem(id=next_chunk.start_plot_id))
        prompt = PROMPT_B_VOLUME_STITCHING.format(
            end_id_a=current_chunk.end_plot_id,
            title_a=tail_item.title,
            summary_of_last_plot_of_A=f"上一块结尾：{tail_item.plot_summary}",
            start_id_b=next_chunk.start_plot_id,
            title_b=head_item.title,
            summary_of_first_plot_of_B=f"下一块开头：{head_item.plot_summary}",
        )
        return await self._stage_two_llm(prompt)

    async def _stage_two_llm(self, prompt: str) -> MergeDecision:
        fallback = MergeDecision(should_merge=False, reason="fallback")
        for attempt in range(1, self.max_parse_retries + 1):
            try:
                response_text = await self._invoke_llm_with_limit(prompt)
                parsed = self._extract_json(response_text, expect_array=False)
                if isinstance(parsed, dict):
                    return MergeDecision(
                        should_merge=self._to_bool(parsed.get("should_merge")),
                        reason=str(parsed.get("reason") or "").strip(),
                    )
            except Exception as exc:
                logger.error(
                    "[事故][分卷-阶段2][book=%s] 缝合判定异常 attempt=%d/%d error=%s",
                    self._active_book_id,
                    attempt,
                    self.max_parse_retries,
                    exc,
                )
        return fallback

    async def _invoke_llm_with_limit(self, prompt: str) -> str:
        semaphore = self._api_semaphore
        if semaphore is None:
            return await self._invoke_llm(prompt)
        async with semaphore:
            return await self._invoke_llm(prompt)

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

    def _extract_json(self, text: str, expect_array: bool) -> Any:
        payload = text.strip()
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", payload, flags=re.IGNORECASE)
        if fenced:
            payload = fenced.group(1).strip()
        pattern = r"\[[\s\S]*\]" if expect_array else r"\{[\s\S]*\}"
        matched = re.search(pattern, payload)
        candidate = matched.group(0).strip() if matched else payload
        return json.loads(candidate)

    @staticmethod
    def _parse_json_list(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return []
            return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []
        return []

    @staticmethod
    def _string_list(value: Any) -> list[str]:
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
    def _brief_join(cls, value: Any, limit: int = 6) -> str:
        items = cls._string_list(value)
        if not items:
            return "无"
        return "；".join(items[:limit])

    @classmethod
    def _summarize_character_deltas(cls, value: Any, limit: int = 4) -> str:
        if not isinstance(value, list):
            return "无"
        parts: list[str] = []
        for item in value[:limit]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            status = cls._brief_join(item.get("status_change"), limit=3)
            relations = cls._brief_join(item.get("relationship_updates"), limit=3)
            skills = cls._brief_join(item.get("skills"), limit=3)
            notes = []
            if status != "无":
                notes.append(f"状态={status}")
            if relations != "无":
                notes.append(f"关系={relations}")
            if skills != "无":
                notes.append(f"能力={skills}")
            parts.append(f"{name}: {' | '.join(notes) if notes else '无明显变化'}")
        return "；".join(parts) if parts else "无"

    @classmethod
    def _summarize_faction_deltas(cls, value: Any, limit: int = 4) -> str:
        if not isinstance(value, list):
            return "无"
        parts: list[str] = []
        for item in value[:limit]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            status = cls._brief_join(item.get("status_change"), limit=3)
            alliances = cls._brief_join(item.get("alliances_or_hostilities"), limit=3)
            structure = cls._brief_join(item.get("leadership_or_membership_updates"), limit=3)
            notes = []
            if status != "无":
                notes.append(f"状态={status}")
            if alliances != "无":
                notes.append(f"关系={alliances}")
            if structure != "无":
                notes.append(f"成员={structure}")
            parts.append(f"{name}: {' | '.join(notes) if notes else '无明显变化'}")
        return "；".join(parts) if parts else "无"

    @classmethod
    def _summarize_relationship_signals(cls, metadata: dict[str, Any], limit: int = 4) -> str:
        highlights = metadata.get("interaction_highlights")
        moments = metadata.get("reader_sensitive_moments")
        parts: list[str] = []
        if isinstance(highlights, list):
            for item in highlights[:limit]:
                if not isinstance(item, dict):
                    continue
                participants = cls._brief_join(item.get("participants"), limit=3)
                interaction_type = str(item.get("type") or "").strip() or "互动"
                importance = str(item.get("importance") or "").strip()
                summary = str(item.get("summary") or "").strip()
                parts.append(
                    f"{participants} | {interaction_type}"
                    + (f" | {importance}" if importance else "")
                    + (f" | {summary}" if summary else "")
                )
        if isinstance(moments, list):
            for item in moments[:limit]:
                if not isinstance(item, dict):
                    continue
                participants = cls._brief_join(item.get("participants"), limit=3)
                tag = str(item.get("tag") or "").strip()
                summary = str(item.get("summary") or "").strip()
                parts.append(
                    f"{participants}"
                    + (f" | {tag}" if tag else "")
                    + (f" | {summary}" if summary else "")
                )
        deduped: list[str] = []
        seen: set[str] = set()
        for item in parts:
            if not item or item in seen:
                continue
            deduped.append(item)
            seen.add(item)
        return "；".join(deduped[:limit]) if deduped else "无"

    @classmethod
    def _build_plot_volume_card(cls, item: PlotItem) -> str:
        def _summarize(value: list[dict[str, Any]], limit: int = 4) -> str:
            parts: list[str] = []
            for item in value[:limit]:
                name = str(item.get("name") or "").strip()
                records = item.get("records") if isinstance(item.get("records"), list) else []
                snippets = [str(record[1]).strip() for record in records[:2] if isinstance(record, list) and len(record) >= 2]
                text = name
                if snippets:
                    text += f": {'；'.join(snippets)}"
                if text.strip():
                    parts.append(text.strip())
            return "；".join(parts) if parts else "无"

        return "\n".join(
            [
                f"Plot {item.id}",
                f"Title: {item.title}",
                f"Summary: {item.plot_summary}",
                f"SpecialExistence: {_summarize(item.special_existence)}",
                f"Organizations: {_summarize(item.origanizations)}",
                f"WorldRules: {_summarize(item.world_rules)}",
            ]
        )

    def _to_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        normalized = str(value or "").strip().lower()
        return normalized in {"true", "1", "yes", "y", "是"}

    def _clear_volume_ids(self, book_id: int) -> None:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE book_plots SET volume_id = 0 WHERE book_id = %s", (book_id,))

    def _write_volume_ids(self, book_id: int, chunks: list[VolumeChunk]) -> None:
        ordered_chunks = sorted(chunks, key=lambda item: item.start_plot_id)
        total = len(ordered_chunks)
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE book_plots SET volume_id = 0 WHERE book_id = %s", (book_id,))
                if total:
                    logger.warning(
                        "[分析进度][分卷写回][book=%s] %s %d/%d",
                        book_id,
                        _progress_bar(0, total),
                        0,
                        total,
                    )
                for volume_index, chunk in enumerate(ordered_chunks, start=1):
                    cursor.execute(
                        """
                        UPDATE book_plots
                        SET volume_id = %s
                        WHERE book_id = %s
                          AND plot_id >= %s
                          AND plot_id <= %s
                        """,
                        (volume_index, book_id, chunk.start_plot_id, chunk.end_plot_id),
                    )
                    logger.warning(
                        "[分析进度][分卷写回][book=%s] %s %d/%d",
                        book_id,
                        _progress_bar(volume_index, total),
                        volume_index,
                        total,
                    )
