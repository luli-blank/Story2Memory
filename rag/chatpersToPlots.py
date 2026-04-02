from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from agent.graph import build_llm
from dotenv import load_dotenv
import pymysql
import pydantic
from rag.prompt import PROMPT_A_TEMPLATE, PROMPT_B_TEMPLATE


ROOT_DIR = Path(__file__).resolve().parents[1]
logger = logging.getLogger(__name__)
BOOK_CHAPTER_PLOT_IF_EXISTS_ENV_VAR = "BOOK_CHAPTER_PLOT_IF_EXISTS"


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


class ChapterSummaryItem(pydantic.BaseModel):
    id: int
    chapter_index: int
    plot_id: int = 0
    chapter_summary: str = ""


class PlotChunk(pydantic.BaseModel):
    start: int
    end: int
    summary: str = ""


class MergeDecision(pydantic.BaseModel):
    should_merge: bool
    reason: str = ""


class PlotSegmentationEngine:
    def __init__(
        self,
        batch_size: int = 10,
        max_iterations: int = 5,
        max_concurrency: int = 25,
        max_parse_retries: int = 10,
    ) -> None:
        self.batch_size = max(1, int(batch_size))
        self.max_iterations = max(1, int(max_iterations))
        self.max_concurrency = min(25, max(1, int(max_concurrency)))
        self.max_parse_retries = max(1, int(max_parse_retries))
        self._api_semaphore: asyncio.Semaphore | None = None
        self._active_book_id = 0
        self._stage_one_total = 0
        self._stage_one_done = 0
        self._negative_merge_cache: set[tuple[Any, ...]] = set()
        self._prompt_b_version = hashlib.sha1(PROMPT_B_TEMPLATE.encode("utf-8")).hexdigest()[:12]
        _load_runtime_env()
        self.llm_client = build_llm()
        self.if_exists = self._resolve_if_exists_mode()

    def run_for_title(self, title: str) -> dict[str, Any]:
        book_title = title.strip()
        if not book_title:
            raise ValueError("Book title is required.")

        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM books WHERE title = %s ORDER BY id DESC LIMIT 1",
                    (book_title,),
                )
                row = cursor.fetchone()
                if not row:
                    raise ValueError(f"Book not found: {book_title}")
                book_id = int(row["id"])
        return self.run(book_id)

    def run(self, book_id: int) -> dict[str, Any]:
        chapters = self._load_chapters(book_id)
        if not chapters:
            self._clear_plot_ids(book_id)
            return {"book_id": book_id, "plot_count": 0, "chapter_count": 0}

        total_chapters, grouped_chapters, skip_segmentation = self._plot_precheck(chapters)
        logger.warning(
            "[分析进度][情节聚类][book=%s] 预检查：%s，策略=%s",
            book_id,
            "已完成，跳过聚类" if (skip_segmentation and self.if_exists == "skip") else "将执行聚类",
            self.if_exists,
        )
        if skip_segmentation and self.if_exists == "skip":
            logger.warning(
                "[分析进度][情节聚类][book=%s] %s %d/%d",
                book_id,
                _progress_bar(grouped_chapters, total_chapters),
                grouped_chapters,
                total_chapters,
            )
            return {"book_id": book_id, "plot_count": 0, "chapter_count": len(chapters)}

        self._active_book_id = book_id
        self._negative_merge_cache.clear()
        final_chunks = asyncio.run(self._segment_chapters(chapters))
        self._write_plot_ids(book_id, final_chunks)
        logger.warning(
            "[分析进度][情节聚类][book=%s] %s %d/%d",
            book_id,
            _progress_bar(len(chapters), len(chapters)),
            len(chapters),
            len(chapters),
        )
        return {"book_id": book_id, "plot_count": len(final_chunks), "chapter_count": len(chapters)}

    def _load_chapters(self, book_id: int) -> list[ChapterSummaryItem]:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, chapter_index, plot_id, chapter_summary
                    FROM book_chapters
                    WHERE book_id = %s
                    ORDER BY chapter_index ASC, id ASC
                    """,
                    (book_id,),
                )
                rows = list(cursor.fetchall() or [])
        chapters: list[ChapterSummaryItem] = [
            ChapterSummaryItem(
                id=int(row["id"]),
                chapter_index=int(row["chapter_index"]),
                plot_id=int(row.get("plot_id") or 0),
                chapter_summary=str(row.get("chapter_summary") or ""),
            )
            for row in rows
        ]
        return chapters

    async def _segment_chapters(self, chapters: list[ChapterSummaryItem]) -> list[PlotChunk]:
        self._api_semaphore = asyncio.Semaphore(self.max_concurrency)
        stage_one = await self._stage_one_parallel(chapters)
        if not stage_one:
            first = chapters[0].chapter_index
            last = chapters[-1].chapter_index
            stage_one = [PlotChunk(start=first, end=last, summary="default")]

        chapter_map = {chapter.chapter_index: chapter for chapter in chapters}
        current_chunks = sorted(stage_one, key=lambda item: item.start)
        iterations = 0
        while iterations < self.max_iterations:
            reduced_chunks, merged_count = await self._stage_two_reduce(
                current_chunks,
                chapter_map,
                round_index=iterations + 1,
            )
            current_chunks = reduced_chunks
            if merged_count == 0:
                break
            iterations += 1

        return current_chunks

    async def _stage_one_parallel(self, chapters: list[ChapterSummaryItem]) -> list[PlotChunk]:
        batches = [
            chapters[start : start + self.batch_size]
            for start in range(0, len(chapters), self.batch_size)
        ]
        tasks = [self._process_single_batch(batch) for batch in batches if batch]
        self._stage_one_total = len(tasks)
        self._stage_one_done = 0
        if self._stage_one_total:
            logger.warning(
                "[分析进度][情节聚类-阶段1][book=%s] %s %d/%d",
                self._active_book_id,
                _progress_bar(0, self._stage_one_total),
                0,
                self._stage_one_total,
            )
        grouped_ranges = await asyncio.gather(*tasks) if tasks else []
        flat_ranges = [item for group in grouped_ranges for item in group]
        return sorted(flat_ranges, key=lambda item: item.start)

    async def _process_single_batch(self, batch: list[ChapterSummaryItem]) -> list[PlotChunk]:
        chapter_data_list = "\n".join(
            f"Ch {chapter.chapter_index}: {chapter.chapter_summary}" for chapter in batch
        )
        prompt = PROMPT_A_TEMPLATE.format(
            batch_size=len(batch),
            chapter_data_list=chapter_data_list,
            first_chapter=batch[0].chapter_index,
            last_chapter=batch[-1].chapter_index,
        )
        raw_ranges = await self._stage_one_llm(prompt, batch)
        if self._stage_one_total:
            self._stage_one_done += 1
            logger.warning(
                "[分析进度][情节聚类-阶段1][book=%s] %s %d/%d",
                self._active_book_id,
                _progress_bar(self._stage_one_done, self._stage_one_total),
                self._stage_one_done,
                self._stage_one_total,
            )
        ranges: list[PlotChunk] = []
        batch_start = batch[0].chapter_index
        batch_end = batch[-1].chapter_index
        for item in raw_ranges:
            start = max(batch_start, int(item.get("start", batch_start)))
            end = min(batch_end, int(item.get("end", batch_end)))
            if start > end:
                continue
            ranges.append(
                PlotChunk(
                    start=start,
                    end=end,
                    summary=str(item.get("summary") or "").strip(),
                )
            )
        if not ranges:
            ranges = [PlotChunk(start=batch_start, end=batch_end, summary="local")]
        return sorted(ranges, key=lambda item: item.start)

    async def _stage_one_llm(
        self, prompt: str, batch: list[ChapterSummaryItem]
    ) -> list[dict[str, Any]]:
        if not batch:
            return []
        fallback = self._fallback_prompt_a_ranges(batch)
        semaphore = self._api_semaphore
        if semaphore is None:
            return fallback

        for attempt in range(1, self.max_parse_retries + 1):
            try:
                async with semaphore:
                    response_text = await self._invoke_llm(prompt)
                parsed_json = self._extract_json(response_text, expect_array=True)
                if not isinstance(parsed_json, list):
                    raise ValueError("Stage1 LLM JSON is not a list.")

                ranges: list[dict[str, Any]] = []
                for item in parsed_json:
                    if not isinstance(item, dict):
                        continue
                    if "start" not in item or "end" not in item:
                        continue
                    ranges.append(
                        {
                            "start": int(item["start"]),
                            "end": int(item["end"]),
                            "summary": str(item.get("summary") or "").strip(),
                        }
                    )
                if ranges:
                    return ranges
                raise ValueError("Stage1 LLM JSON has no valid ranges.")
            except Exception as exc:
                logger.error(
                    "[事故][情节聚类-阶段1][book=%s] 响应格式异常，attempt=%d/%d，error=%s",
                    self._active_book_id,
                    attempt,
                    self.max_parse_retries,
                    exc,
                )
        return fallback

    async def _stage_two_reduce(
        self,
        chunks: list[PlotChunk],
        chapter_map: dict[int, ChapterSummaryItem],
        round_index: int,
    ) -> tuple[list[PlotChunk], int]:
        if not chunks:
            return [], 0

        merged: list[PlotChunk] = []
        merged_count = 0
        index = 0
        total_pairs = max(len(chunks) - 1, 0)
        done_pairs = 0
        if total_pairs:
            logger.warning(
                "[分析进度][情节聚类-阶段2-R%d][book=%s] %s %d/%d",
                round_index,
                self._active_book_id,
                _progress_bar(0, total_pairs),
                0,
                total_pairs,
            )

        decisions_by_index = await self._prefetch_stage_two_decisions(chunks, chapter_map)

        while index < len(chunks):
            current_chunk = chunks[index]
            if index + 1 >= len(chunks):
                merged.append(current_chunk)
                break

            next_chunk = chunks[index + 1]
            decision = decisions_by_index.get(index, MergeDecision(should_merge=False, reason="missing_decision"))
            done_pairs += 1
            logger.warning(
                "[分析进度][情节聚类-阶段2-R%d][book=%s] %s %d/%d",
                round_index,
                self._active_book_id,
                _progress_bar(done_pairs, total_pairs),
                done_pairs,
                total_pairs,
            )
            if decision.should_merge:
                merged.append(
                    PlotChunk(
                        start=current_chunk.start,
                        end=next_chunk.end,
                        summary=current_chunk.summary or next_chunk.summary,
                    )
                )
                merged_count += 1
                index += 2
                continue

            merged.append(current_chunk)
            index += 1

        return merged, merged_count

    async def _prefetch_stage_two_decisions(
        self,
        chunks: list[PlotChunk],
        chapter_map: dict[int, ChapterSummaryItem],
    ) -> dict[int, MergeDecision]:
        decisions_by_index: dict[int, MergeDecision] = {}
        pending_indices: list[int] = []
        pending_signatures: list[tuple[Any, ...]] = []
        pending_tasks: list[asyncio.Future[MergeDecision] | asyncio.Task[MergeDecision] | Any] = []

        for index in range(len(chunks) - 1):
            current_chunk = chunks[index]
            next_chunk = chunks[index + 1]
            tail_summary = chapter_map.get(
                current_chunk.end,
                ChapterSummaryItem(id=0, chapter_index=0),
            ).chapter_summary
            head_summary = chapter_map.get(
                next_chunk.start,
                ChapterSummaryItem(id=0, chapter_index=0),
            ).chapter_summary
            boundary_signature = self._build_boundary_signature(
                current_chunk=current_chunk,
                next_chunk=next_chunk,
                tail_summary=tail_summary,
                head_summary=head_summary,
            )
            if boundary_signature in self._negative_merge_cache:
                decisions_by_index[index] = MergeDecision(should_merge=False, reason="cached_no_merge")
                continue

            prompt = PROMPT_B_TEMPLATE.format(
                end_index_a=current_chunk.end,
                summary_of_last_chapter_of_A=tail_summary,
                start_index_b=next_chunk.start,
                summary_of_first_chapter_of_B=head_summary,
            )
            pending_indices.append(index)
            pending_signatures.append(boundary_signature)
            pending_tasks.append(self._stage_two_llm(prompt, tail_summary, head_summary))

        if pending_tasks:
            pending_results = await asyncio.gather(*pending_tasks)
            for index, boundary_signature, decision in zip(
                pending_indices,
                pending_signatures,
                pending_results,
            ):
                decisions_by_index[index] = decision
                if not decision.should_merge:
                    self._negative_merge_cache.add(boundary_signature)

        return decisions_by_index

    async def _stage_two_llm(self, prompt: str, tail_summary: str, head_summary: str) -> MergeDecision:
        fallback = self._fallback_prompt_b_decision(tail_summary, head_summary)
        semaphore = self._api_semaphore
        if semaphore is None:
            return fallback

        for attempt in range(1, self.max_parse_retries + 1):
            try:
                async with semaphore:
                    response_text = await self._invoke_llm(prompt)
                parsed_json = self._extract_json(response_text, expect_array=False)
                if not isinstance(parsed_json, dict):
                    raise ValueError("Stage2 LLM JSON is not an object.")
                return MergeDecision(
                    should_merge=bool(parsed_json.get("should_merge", False)),
                    reason=str(parsed_json.get("reason") or ""),
                )
            except Exception as exc:
                logger.error(
                    "[事故][情节聚类-阶段2][book=%s] 响应格式异常，attempt=%d/%d，error=%s",
                    self._active_book_id,
                    attempt,
                    self.max_parse_retries,
                    exc,
                )
        return fallback

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return {token.lower() for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9_]+", text) if token.strip()}

    def _build_boundary_signature(
        self,
        current_chunk: PlotChunk,
        next_chunk: PlotChunk,
        tail_summary: str,
        head_summary: str,
    ) -> tuple[Any, ...]:
        tail_hash = hashlib.sha1((tail_summary or "").strip().encode("utf-8")).hexdigest()[:16]
        head_hash = hashlib.sha1((head_summary or "").strip().encode("utf-8")).hexdigest()[:16]
        return (
            current_chunk.start,
            current_chunk.end,
            next_chunk.start,
            next_chunk.end,
            tail_hash,
            head_hash,
            self._prompt_b_version,
        )

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

    def _fallback_prompt_a_ranges(self, batch: list[ChapterSummaryItem]) -> list[dict[str, Any]]:
        if not batch:
            return []
        ranges: list[dict[str, Any]] = []
        start_index = batch[0].chapter_index
        previous = batch[0]

        for current in batch[1:]:
            if self._should_split(previous.chapter_summary, current.chapter_summary):
                ranges.append({"start": start_index, "end": previous.chapter_index, "summary": "local"})
                start_index = current.chapter_index
            previous = current

        ranges.append({"start": start_index, "end": batch[-1].chapter_index, "summary": "local"})
        return ranges

    def _fallback_prompt_b_decision(self, tail_summary: str, head_summary: str) -> MergeDecision:
        tail_tokens = self._tokenize(tail_summary)
        head_tokens = self._tokenize(head_summary)
        should_merge = bool(tail_tokens and head_tokens and (tail_tokens & head_tokens))
        return MergeDecision(
            should_merge=should_merge,
            reason="token_overlap" if should_merge else "no_overlap",
        )

    def _should_split(self, previous_summary: str, current_summary: str) -> bool:
        previous_tokens = self._tokenize(previous_summary)
        current_tokens = self._tokenize(current_summary)
        if not previous_tokens or not current_tokens:
            return False
        overlap = previous_tokens & current_tokens
        return len(overlap) == 0 and len(previous_tokens) > 3 and len(current_tokens) > 3

    def _plot_precheck(self, chapters: list[ChapterSummaryItem]) -> tuple[int, int, bool]:
        total_count = len(chapters)
        grouped_count = sum(1 for chapter in chapters if int(chapter.plot_id) != 0)
        return total_count, grouped_count, total_count > 0 and grouped_count == total_count

    def _resolve_if_exists_mode(self) -> str:
        raw_mode = str(os.getenv(BOOK_CHAPTER_PLOT_IF_EXISTS_ENV_VAR, "skip")).strip().lower()
        if raw_mode in {"overwrite", "rebuild", "regen"}:
            return "overwrite"
        return "skip"

    def _clear_plot_ids(self, book_id: int) -> None:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE book_chapters SET plot_id = 0 WHERE book_id = %s", (book_id,))

    def _write_plot_ids(self, book_id: int, chunks: list[PlotChunk]) -> None:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE book_chapters SET plot_id = 0 WHERE book_id = %s", (book_id,))
                for plot_order, chunk in enumerate(chunks, start=1):
                    cursor.execute(
                        """
                        UPDATE book_chapters
                        SET plot_id = %s
                        WHERE book_id = %s
                          AND chapter_index >= %s
                          AND chapter_index <= %s
                        """,
                        (plot_order, book_id, chunk.start, chunk.end),
                    )
