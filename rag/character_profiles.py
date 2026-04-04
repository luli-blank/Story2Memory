from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage

from agent.graph import build_llm
from agent.prompt import ROLEPLAY_STYLE_SAMPLE_BATCH_PROMPT, ROLEPLAY_STYLE_SAMPLE_SUMMARY_PROMPT
from rag.prompt import (
    CHARACTER_PROFILE_APPEARANCE_PROMPT,
    CHARACTER_PROFILE_CRITICAL_CHUNK_PROMPT,
    CHARACTER_PROFILE_CURRENT_STATE_PROMPT,
    CHARACTER_PROFILE_IDENTITY_ROLE_PROMPT,
    CHARACTER_PROFILE_VOLUME_GROUP_PROMPT,
    CHARACTER_PROFILE_MECHANISM_PROMPT,
    CHARACTER_PROFILE_PERSONALITY_PROMPT,
    CHARACTER_PROFILE_SLICE_PROMPT,
    CHARACTER_RELATION_CRITICAL_CHUNK_PROMPT,
    CHARACTER_RELATION_DYNAMICS_PROMPT,
    CHARACTER_RELATION_HISTORY_SEGMENT_PROMPT,
    CHARACTER_RELATION_OVERVIEW_PROMPT,
    CHARACTER_RELATION_STRUCTURE_PROMPT,
    CHARACTER_RELATION_VOLUME_GROUP_PROMPT,
    CHARACTER_PROFILE_VOLUME_ARC_PROMPT,
    CHARACTER_ROLEPLAY_RELATION_BATCH_PROMPT,
    CHARACTER_ROLEPLAY_RELATION_SUMMARY_PROMPT,
)
from rag.uploadBook import _connect as _connect_mysql
from rag.uploadBook import _ensure_schema as _ensure_mysql_schema

logger = logging.getLogger(__name__)

MAX_WINDOW_CHAPTERS = 18
MAX_WINDOW_CHARS = 4000
MAX_CONCURRENT_WINDOW_TASKS = 15
MAX_CONCURRENT_RELATION_TASKS = 25
INVALID_JSON_REMINDER = "你上一次输出不是合法 JSON，请只返回合法 JSON，不要解释。"
INVALID_JSON_RETRY_DELAY_SECONDS = 2.0
PROFILE_CRITICAL_HIGH_SCORE = 12
PROFILE_CRITICAL_MEDIUM_SCORE = 8
RELATION_CRITICAL_HIGH_SCORE = 10
RELATION_CRITICAL_MEDIUM_SCORE = 6
CONTENT_CONTEXT_EXPANSION = 1
MAX_CRITICAL_CHAPTERS_PER_VOLUME = 6
MAX_RELATION_CHAPTERS_PER_VOLUME = 5
MAX_CHAPTER_CONTENT_EXCERPT_CHARS = 1800
MAX_CRITICAL_CHAPTERS_PER_CHUNK = 10
MAX_CONCURRENT_PROFILE_CHUNK_TASKS = 12
MAX_CONCURRENT_PROFILE_GROUP_TASKS = 8
MAX_CONCURRENT_RELATION_CHUNK_TASKS = 12
MAX_CONCURRENT_RELATION_GROUP_TASKS = 8
MAX_CONCURRENT_FINAL_PROFILE_MODULE_TASKS = 5
MAX_CONCURRENT_FINAL_RELATION_MODULE_TASKS = 4
MAX_ROLEPLAY_WINDOW_CHAPTERS = 10
MAX_ROLEPLAY_WINDOW_CHARS = 5200
MAX_CONCURRENT_ROLEPLAY_BATCH_TASKS = 6
MAX_CONCURRENT_ROLEPLAY_RELATION_SUMMARIES = 6
MAX_ROLEPLAY_RELATION_TARGETS = 8
CHARACTER_ARCHIVE_SCHEMA_VERSION = 4
JSON_RETRY_FALLBACK_MODEL = "Doubao-Seed-2.0-pro"
PROFILE_CHANGE_KEYWORDS = ("成为", "加入", "背叛", "暴露", "恢复", "叛逃", "接任", "身份", "立场")
PROFILE_LIFE_STATE_KEYWORDS = ("死亡", "复活", "重伤", "濒死", "失控", "复苏", "觉醒", "苏醒")
PROFILE_ABILITY_RESOURCE_KEYWORDS = ("获得", "失去", "驾驭", "压制", "拿到", "夺得", "使用", "掌握", "能力", "鬼域", "灵异", "物品")
PROFILE_DECISION_KEYWORDS = ("决定", "打算", "选择", "拒绝", "答应", "计划", "命令", "要求")
PROFILE_RELATION_KEYWORDS = ("救下", "保护", "联手", "翻脸", "怀疑", "依赖", "信任", "决裂", "合作", "背叛")
PROFILE_EMOTION_KEYWORDS = ("恐惧", "愤怒", "绝望", "迟疑", "动摇", "后悔", "坚定", "紧张", "惊讶", "痛苦")
RELATION_INTERACTION_KEYWORDS = ("救", "杀", "打", "护", "骗", "威胁", "命令", "联手", "交易", "安慰", "表白", "利用", "怀疑", "保护")
RELATION_CHANGE_KEYWORDS = ("合作", "翻脸", "决裂", "和解", "确认关系", "互相怀疑", "建立信任", "依赖", "疏远", "亲近")
RELATION_EMOTION_KEYWORDS = ("信任", "依赖", "厌恶", "恐惧", "爱慕", "警惕", "尊敬", "嫉妒", "愧疚", "关心")
RELATION_STRUCTURAL_KEYWORDS = ("父", "母", "兄", "姐", "弟", "妹", "师", "徒", "同学", "队友", "上司", "下属", "夫妻", "恋人", "朋友")


@dataclass(frozen=True)
class ChapterEntry:
    chapter_index: int
    record_description: str
    chapter_summary: str
    volume_index: int
    volume_title: str


@dataclass(frozen=True)
class VolumeWindow:
    volume_index: int
    volume_title: str
    chapter_start: int
    chapter_end: int
    entries: tuple[ChapterEntry, ...]


def _progress_bar(current: int, total: int, width: int = 24) -> str:
    safe_total = max(1, int(total))
    safe_current = max(0, min(int(current), safe_total))
    filled = int(width * safe_current / safe_total)
    return f"[{'#' * filled}{'-' * (width - filled)}]"


def _connect():
    conn = _connect_mysql()
    _ensure_mysql_schema(conn)
    _ensure_character_profile_schema(conn)
    return conn


def _ensure_character_profile_schema(conn: Any) -> None:
    with conn.cursor() as cursor:
        required_columns = {
            "character_relations": {
                "relation_model_json": "ALTER TABLE `character_relations` ADD COLUMN `relation_model_json` JSON NULL AFTER `summary`"
            },
        }
        required_tables = {
            "character_profile_chunks",
            "character_profile_volume_groups",
            "character_relation_chunks",
            "character_relation_volume_groups",
        }

        for table_name in required_tables:
            cursor.execute(
                """
                SELECT TABLE_NAME
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = %s
                LIMIT 1
                """,
                (table_name,),
            )
            if cursor.fetchone():
                continue
            raise RuntimeError(f"Required table is missing after schema bootstrap: {table_name}")

        for table_name, columns in required_columns.items():
            for column_name, statement in columns.items():
                cursor.execute(
                    """
                    SELECT COLUMN_NAME
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = %s
                      AND COLUMN_NAME = %s
                    LIMIT 1
                    """,
                    (table_name, column_name),
                )
                if cursor.fetchone():
                    continue
                cursor.execute(statement)


def _parse_json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _parse_json_list(value: Any) -> list[Any]:
    parsed = _parse_json_value(value)
    return parsed if isinstance(parsed, list) else []


def _parse_json_dict(value: Any) -> dict[str, Any]:
    parsed = _parse_json_value(value)
    return parsed if isinstance(parsed, dict) else {}


def _stringify_content(content: Any) -> str:
    if isinstance(content, AIMessage):
        content = content.content
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                stripped = item.strip()
                if stripped:
                    parts.append(stripped)
                continue
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
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

    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", payload, flags=re.IGNORECASE)
    if fence:
        payload = fence.group(1).strip()
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


def _normalize_text_key(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip())


def _build_name_resolution_candidates(raw_name: str) -> list[str]:
    text = str(raw_name or "").strip()
    if not text:
        return []

    candidates: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        normalized = str(candidate or "").strip()
        if not normalized or normalized in seen:
            return
        candidates.append(normalized)
        seen.add(normalized)

    add(text)

    stripped_parenthetical = re.sub(r"[（(][^（）()]*?[）)]", "", text).strip()
    add(stripped_parenthetical)

    parenthetical_parts = re.findall(r"[（(]([^（）()]*)[）)]", text)
    for part in parenthetical_parts:
        add(part)
        for token in re.split(r"\s*[/／、，,；;|]\s*", str(part or "").strip()):
            add(token)

    return candidates


def _dedupe_str_list(items: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        result.append(text)
        seen.add(text)
    return result


def _normalize_int_list(items: Any) -> list[int]:
    if not isinstance(items, list):
        return []
    values: list[int] = []
    seen: set[int] = set()
    for item in items:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value <= 0 or value in seen:
            continue
        values.append(value)
        seen.add(value)
    return values


def _hash_payload(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _select_chapter_summary(row: dict[str, Any]) -> str:
    summary = str(row.get("chapter_summary") or "").strip()
    if summary:
        return summary
    raw_summary = _parse_json_dict(row.get("raw_summary_json"))
    return str(raw_summary.get("chapter_summary") or "").strip()


def _normalize_character_row(row: dict[str, Any]) -> dict[str, Any]:
    records = _parse_json_list(row.get("records"))
    aliases = _dedupe_str_list(_parse_json_list(row.get("aliases")))
    normalized_records: list[tuple[int, str]] = []
    for item in records:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            chapter_index = int(item[0])
        except (TypeError, ValueError):
            continue
        description = str(item[1] or "").strip()
        if chapter_index <= 0 or not description:
            continue
        normalized_records.append((chapter_index, description))
    return {
        "id": int(row.get("id") or 0),
        "book_id": int(row.get("book_id") or 0),
        "name": str(row.get("name") or "").strip(),
        "aliases": aliases,
        "records": normalized_records,
        "need_delete": str(row.get("NEED_DELETE") or "").strip().lower(),
    }


def _load_character_row(cursor: Any, book_id: int, character_id: int) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT id, book_id, name, aliases, records, NEED_DELETE
        FROM characters
        WHERE book_id = %s AND id = %s
        LIMIT 1
        """,
        (int(book_id), int(character_id)),
    )
    row = cursor.fetchone() or {}
    if not row:
        raise ValueError(f"Character not found: book_id={book_id} character_id={character_id}")
    normalized = _normalize_character_row(row)
    if normalized["id"] <= 0 or not normalized["name"]:
        raise ValueError(f"Character not found: book_id={book_id} character_id={character_id}")
    return normalized


def _load_book_characters(cursor: Any, book_id: int) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT id, book_id, name, aliases, records, NEED_DELETE
        FROM characters
        WHERE book_id = %s
        ORDER BY id ASC
        """,
        (int(book_id),),
    )
    return [_normalize_character_row(row) for row in cursor.fetchall() or []]


def _load_book_chapters(cursor: Any, book_id: int) -> dict[int, dict[str, Any]]:
    cursor.execute(
        """
        SELECT chapter_index, chapter_summary, raw_summary_json
        FROM book_chapters
        WHERE book_id = %s
        ORDER BY chapter_index ASC
        """,
        (int(book_id),),
    )
    rows = cursor.fetchall() or []
    return {
        int(row.get("chapter_index") or 0): {
            "chapter_index": int(row.get("chapter_index") or 0),
            "chapter_summary": _select_chapter_summary(row),
        }
        for row in rows
        if int(row.get("chapter_index") or 0) > 0
    }


def _load_chapter_contents(cursor: Any, book_id: int, chapter_indexes: list[int]) -> dict[int, str]:
    normalized_indexes = sorted({int(item) for item in chapter_indexes if int(item) > 0})
    if not normalized_indexes:
        return {}
    placeholders = ", ".join(["%s"] * len(normalized_indexes))
    cursor.execute(
        f"""
        SELECT chapter_index, content
        FROM book_chapters
        WHERE book_id = %s AND chapter_index IN ({placeholders})
        ORDER BY chapter_index ASC
        """,
        [int(book_id), *normalized_indexes],
    )
    rows = cursor.fetchall() or []
    return {
        int(row.get("chapter_index") or 0): str(row.get("content") or "")
        for row in rows
        if int(row.get("chapter_index") or 0) > 0
    }


def _load_book_volumes(cursor: Any, book_id: int) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT volume_index, title, start_chapter_index, end_chapter_index
        FROM book_volumes
        WHERE book_id = %s
        ORDER BY volume_index ASC
        """,
        (int(book_id),),
    )
    rows = cursor.fetchall() or []
    normalized: list[dict[str, Any]] = []
    for row in rows:
        volume_index = int(row.get("volume_index") or 0)
        if volume_index <= 0:
            continue
        normalized.append(
            {
                "volume_index": volume_index,
                "title": str(row.get("title") or f"第{volume_index}卷").strip() or f"第{volume_index}卷",
                "start_chapter_index": int(row.get("start_chapter_index") or 0),
                "end_chapter_index": int(row.get("end_chapter_index") or 0),
            }
        )
    return normalized


def _resolve_volume(chapter_index: int, volume_rows: list[dict[str, Any]]) -> tuple[int, str]:
    for row in volume_rows:
        start = int(row.get("start_chapter_index") or 0)
        end = int(row.get("end_chapter_index") or 0)
        if start <= chapter_index <= end:
            return int(row["volume_index"]), str(row["title"])
    return 0, "未分卷"


def _build_chapter_entries(
    character_row: dict[str, Any],
    chapter_rows: dict[int, dict[str, Any]],
    volume_rows: list[dict[str, Any]],
) -> list[ChapterEntry]:
    descriptions_by_chapter: dict[int, list[str]] = defaultdict(list)
    for chapter_index, description in character_row["records"]:
        bucket = descriptions_by_chapter[int(chapter_index)]
        if description not in bucket:
            bucket.append(description)

    entries: list[ChapterEntry] = []
    for chapter_index in sorted(descriptions_by_chapter.keys()):
        chapter_row = chapter_rows.get(chapter_index, {})
        chapter_summary = str(chapter_row.get("chapter_summary") or "").strip()
        volume_index, volume_title = _resolve_volume(chapter_index, volume_rows)
        entries.append(
            ChapterEntry(
                chapter_index=chapter_index,
                record_description="；".join(descriptions_by_chapter[chapter_index]),
                chapter_summary=chapter_summary,
                volume_index=volume_index,
                volume_title=volume_title,
            )
        )
    return entries


def _window_char_count(entries: list[ChapterEntry] | tuple[ChapterEntry, ...]) -> int:
    return sum(len(entry.record_description) + len(entry.chapter_summary) for entry in entries)


def _split_windows(entries: list[ChapterEntry]) -> list[VolumeWindow]:
    grouped: dict[tuple[int, str], list[ChapterEntry]] = defaultdict(list)
    for entry in entries:
        grouped[(entry.volume_index, entry.volume_title)].append(entry)

    windows: list[VolumeWindow] = []
    for (volume_index, volume_title), group in sorted(grouped.items(), key=lambda item: item[0][0]):
        ordered = sorted(group, key=lambda item: item.chapter_index)
        batch: list[ChapterEntry] = []
        current_chars = 0
        for entry in ordered:
            entry_chars = len(entry.record_description) + len(entry.chapter_summary)
            if batch and (len(batch) >= MAX_WINDOW_CHAPTERS or current_chars + entry_chars > MAX_WINDOW_CHARS):
                windows.append(
                    VolumeWindow(
                        volume_index=volume_index,
                        volume_title=volume_title,
                        chapter_start=batch[0].chapter_index,
                        chapter_end=batch[-1].chapter_index,
                        entries=tuple(batch),
                    )
                )
                batch = []
                current_chars = 0
            batch.append(entry)
            current_chars += entry_chars
        if batch:
            windows.append(
                VolumeWindow(
                    volume_index=volume_index,
                    volume_title=volume_title,
                    chapter_start=batch[0].chapter_index,
                    chapter_end=batch[-1].chapter_index,
                    entries=tuple(batch),
                )
            )
    return windows


def _split_roleplay_windows(entries: list[ChapterEntry]) -> list[VolumeWindow]:
    grouped: dict[tuple[int, str], list[ChapterEntry]] = defaultdict(list)
    for entry in entries:
        grouped[(entry.volume_index, entry.volume_title)].append(entry)

    windows: list[VolumeWindow] = []
    for (volume_index, volume_title), group in sorted(grouped.items(), key=lambda item: item[0][0]):
        ordered = sorted(group, key=lambda item: item.chapter_index)
        batch: list[ChapterEntry] = []
        current_chars = 0
        for entry in ordered:
            entry_chars = len(entry.record_description) + len(entry.chapter_summary)
            if batch and (
                len(batch) >= MAX_ROLEPLAY_WINDOW_CHAPTERS or current_chars + entry_chars > MAX_ROLEPLAY_WINDOW_CHARS
            ):
                windows.append(
                    VolumeWindow(
                        volume_index=volume_index,
                        volume_title=volume_title,
                        chapter_start=batch[0].chapter_index,
                        chapter_end=batch[-1].chapter_index,
                        entries=tuple(batch),
                    )
                )
                batch = []
                current_chars = 0
            batch.append(entry)
            current_chars += entry_chars
        if batch:
            windows.append(
                VolumeWindow(
                    volume_index=volume_index,
                    volume_title=volume_title,
                    chapter_start=batch[0].chapter_index,
                    chapter_end=batch[-1].chapter_index,
                    entries=tuple(batch),
                )
            )
    return windows


def _build_version_hash(
    character_row: dict[str, Any],
    entries: list[ChapterEntry],
    volume_rows: list[dict[str, Any]],
) -> str:
    payload = {
        "schema_version": CHARACTER_ARCHIVE_SCHEMA_VERSION,
        "character_id": character_row["id"],
        "character_name": character_row["name"],
        "aliases": character_row["aliases"],
        "records": [
            {
                "chapter_index": entry.chapter_index,
                "record_description": entry.record_description,
                "chapter_summary": entry.chapter_summary,
                "volume_index": entry.volume_index,
            }
            for entry in entries
        ],
        "volumes": volume_rows,
    }
    return _hash_payload(payload)


def _load_character_context(cursor: Any, book_id: int, character_id: int) -> dict[str, Any]:
    character_row = _load_character_row(cursor, book_id, character_id)
    chapter_rows = _load_book_chapters(cursor, book_id)
    volume_rows = _load_book_volumes(cursor, book_id)
    entries = _build_chapter_entries(character_row, chapter_rows, volume_rows)
    windows = _split_windows(entries)
    return {
        "character_row": character_row,
        "chapter_rows": chapter_rows,
        "entries": entries,
        "windows": windows,
        "volume_rows": volume_rows,
        "version_hash": _build_version_hash(character_row, entries, volume_rows),
    }


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword for keyword in keywords if keyword and keyword in text)


def _count_mentions(text: str, aliases: list[str]) -> int:
    normalized_text = str(text or "")
    total = 0
    for alias in aliases:
        alias_text = str(alias or "").strip()
        if not alias_text:
            continue
        total += normalized_text.count(alias_text)
    return total


def _entry_lookup(entries: list[ChapterEntry]) -> dict[int, ChapterEntry]:
    return {entry.chapter_index: entry for entry in entries}


def _expand_chapter_indexes(
    chapter_indexes: set[int],
    available_chapters: set[int],
    expansion: int = CONTENT_CONTEXT_EXPANSION,
) -> list[int]:
    expanded: set[int] = set()
    for chapter_index in chapter_indexes:
        for offset in range(-int(expansion), int(expansion) + 1):
            candidate = int(chapter_index) + offset
            if candidate in available_chapters:
                expanded.add(candidate)
    return sorted(expanded)


def _merge_reason_list(reasons: list[str]) -> list[str]:
    return _dedupe_str_list(reasons)


def _score_profile_entry(
    entry: ChapterEntry,
    *,
    entry_index: int,
    entries: list[ChapterEntry],
    volume_entries: list[ChapterEntry],
    aliases: list[str],
) -> tuple[int, list[str]]:
    text = f"{entry.record_description}\n{entry.chapter_summary}"
    score = 0
    reasons: list[str] = []
    if entry_index == 0:
        score += 8
        reasons.append("全书首次登场")
    if volume_entries and entry.chapter_index == volume_entries[0].chapter_index:
        score += 4
        reasons.append("本卷首次登场")
    if volume_entries and entry.chapter_index == volume_entries[-1].chapter_index:
        score += 3
        reasons.append("本卷最后出现")
    if entry_index > 0 and entry.chapter_index - entries[entry_index - 1].chapter_index >= 30:
        score += 4
        reasons.append("长间隔后重新出现")
    if _contains_any(text, PROFILE_CHANGE_KEYWORDS):
        score += 6
        reasons.append("身份或立场变化")
    if _contains_any(text, PROFILE_LIFE_STATE_KEYWORDS):
        score += 5
        reasons.append("生死或状态剧变")
    if _contains_any(text, PROFILE_ABILITY_RESOURCE_KEYWORDS):
        score += 4
        reasons.append("能力或资源变化")
    if _contains_any(text, PROFILE_DECISION_KEYWORDS):
        score += 3
        reasons.append("重大决策")
    if _contains_any(text, PROFILE_RELATION_KEYWORDS):
        score += 3
        reasons.append("关系变化")
    if _contains_any(text, PROFILE_EMOTION_KEYWORDS):
        score += 2
        reasons.append("情绪或心理转折")
    mention_count = _count_mentions(text, aliases)
    if mention_count >= 2:
        score += 2
        reasons.append("章节中心度较高")
    return score, _merge_reason_list(reasons)


def _select_profile_critical_chapters(
    entries: list[ChapterEntry],
    aliases: list[str],
    chapter_rows: dict[int, dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    if not entries:
        return {}
    selected: dict[int, dict[str, Any]] = {}
    by_volume: dict[int, list[tuple[int, ChapterEntry]]] = defaultdict(list)
    for index, entry in enumerate(entries):
        by_volume[int(entry.volume_index)].append((index, entry))

    for _, volume_items in sorted(by_volume.items(), key=lambda item: item[0]):
        volume_entries = [entry for _, entry in volume_items]
        scored: list[tuple[int, int, list[str], ChapterEntry]] = []
        for position, (entry_index, entry) in enumerate(volume_items):
            score, reasons = _score_profile_entry(
                entry,
                entry_index=entry_index,
                entries=entries,
                volume_entries=volume_entries,
                aliases=aliases,
            )
            scored.append((score, position, reasons, entry))

        chosen_chapters: set[int] = set()
        if volume_entries:
            chosen_chapters.add(volume_entries[0].chapter_index)
            chosen_chapters.add(volume_entries[-1].chapter_index)

        for score, _, _, entry in scored:
            if score >= PROFILE_CRITICAL_HIGH_SCORE:
                chosen_chapters.add(entry.chapter_index)

        medium_candidates = [
            item for item in scored if PROFILE_CRITICAL_MEDIUM_SCORE <= item[0] < PROFILE_CRITICAL_HIGH_SCORE
        ]
        medium_candidates.sort(key=lambda item: (-item[0], item[3].chapter_index))
        for score, _, _, entry in medium_candidates[:2]:
            _ = score
            chosen_chapters.add(entry.chapter_index)

        per_volume_selected: list[tuple[int, list[str], ChapterEntry]] = []
        for score, _, reasons, entry in scored:
            if entry.chapter_index in chosen_chapters:
                per_volume_selected.append((score, reasons, entry))
        per_volume_selected.sort(key=lambda item: (-item[0], item[2].chapter_index))
        for score, reasons, entry in per_volume_selected[:MAX_CRITICAL_CHAPTERS_PER_VOLUME]:
            selected[entry.chapter_index] = {
                "score": score,
                "reasons": reasons,
                "record_description": entry.record_description,
                "chapter_summary": str(chapter_rows.get(entry.chapter_index, {}).get("chapter_summary") or entry.chapter_summary).strip(),
                "volume_index": entry.volume_index,
                "volume_title": entry.volume_title,
                "is_context": False,
            }

    expanded_indexes = _expand_chapter_indexes(set(selected.keys()), set(chapter_rows.keys()))
    for chapter_index in expanded_indexes:
        selected.setdefault(
            chapter_index,
            {
                "score": 0,
                "reasons": ["邻接上下文"],
                "record_description": "",
                "chapter_summary": str(chapter_rows.get(chapter_index, {}).get("chapter_summary") or "").strip(),
                "volume_index": 0,
                "volume_title": "",
                "is_context": True,
            },
        )
    return dict(sorted(selected.items(), key=lambda item: item[0]))


def _score_relation_chapter(
    *,
    chapter_index: int,
    target_aliases: list[str],
    text: str,
    is_volume_first: bool,
    is_volume_last: bool,
    evidence_hit: bool,
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    mention_count = _count_mentions(text, target_aliases)
    if evidence_hit:
        score += 6
        reasons.append("已识别关系证据")
    if mention_count > 0 and _contains_any(text, RELATION_INTERACTION_KEYWORDS):
        score += 6
        reasons.append("存在明确互动")
    if mention_count > 0 and _contains_any(text, RELATION_CHANGE_KEYWORDS):
        score += 5
        reasons.append("关系变化信号")
    if mention_count > 0 and _contains_any(text, RELATION_EMOTION_KEYWORDS):
        score += 4
        reasons.append("情感指向明确")
    if mention_count > 0 and _contains_any(text, RELATION_STRUCTURAL_KEYWORDS):
        score += 4
        reasons.append("结构关系线索")
    if mention_count >= 2:
        score += 3
        reasons.append("双向提及概率较高")
    if is_volume_first:
        score += 2
        reasons.append("本卷关系起点")
    if is_volume_last:
        score += 2
        reasons.append("本卷关系终点")
    return score, _merge_reason_list(reasons)


def _alias_lookup_by_name(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    lookup: dict[str, list[str]] = {}
    for row in rows:
        canonical_name = str(row.get("name") or "").strip()
        if not canonical_name:
            continue
        lookup[canonical_name] = _dedupe_str_list([canonical_name, *(row.get("aliases") or [])])
    return lookup


def _select_relation_critical_chapters(
    grouped_relations: list[dict[str, Any]],
    entry_map: dict[int, ChapterEntry],
    chapter_rows: dict[int, dict[str, Any]],
    alias_lookup: dict[str, list[str]],
) -> dict[str, list[dict[str, Any]]]:
    selected_by_target: dict[str, list[dict[str, Any]]] = {}
    available_chapters = set(chapter_rows.keys())
    for row in grouped_relations:
        target_name = str(row.get("target_character_name") or "").strip()
        if not target_name:
            continue
        target_aliases = alias_lookup.get(target_name, [target_name])
        evidence_by_chapter: dict[int, set[int]] = defaultdict(set)
        for item in row.get("history_json") or []:
            if not isinstance(item, dict):
                continue
            start = int(item.get("chapter_start") or 0)
            end = int(item.get("chapter_end") or 0) or start
            for chapter_index in range(min(start, end), max(start, end) + 1):
                evidence_by_chapter[chapter_index].update(_normalize_int_list(item.get("evidence_chapters")))

        if not evidence_by_chapter:
            continue

        by_volume: dict[int, list[int]] = defaultdict(list)
        for chapter_index in evidence_by_chapter.keys():
            entry = entry_map.get(chapter_index)
            if entry is None:
                continue
            by_volume[int(entry.volume_index)].append(chapter_index)

        selected: dict[int, dict[str, Any]] = {}
        for _, chapter_indexes in sorted(by_volume.items(), key=lambda item: item[0]):
            ordered = sorted(set(chapter_indexes))
            scored_rows: list[tuple[int, list[str], int]] = []
            for idx, chapter_index in enumerate(ordered):
                entry = entry_map.get(chapter_index)
                if entry is None:
                    continue
                text = f"{entry.record_description}\n{entry.chapter_summary}"
                score, reasons = _score_relation_chapter(
                    chapter_index=chapter_index,
                    target_aliases=target_aliases,
                    text=text,
                    is_volume_first=idx == 0,
                    is_volume_last=idx == len(ordered) - 1,
                    evidence_hit=chapter_index in evidence_by_chapter,
                )
                scored_rows.append((score, reasons, chapter_index))

            chosen_chapters: set[int] = set()
            if ordered:
                chosen_chapters.add(ordered[0])
                chosen_chapters.add(ordered[-1])
            for score, _, chapter_index in scored_rows:
                if score >= RELATION_CRITICAL_HIGH_SCORE:
                    chosen_chapters.add(chapter_index)
            medium_candidates = [item for item in scored_rows if RELATION_CRITICAL_MEDIUM_SCORE <= item[0] < RELATION_CRITICAL_HIGH_SCORE]
            medium_candidates.sort(key=lambda item: (-item[0], item[2]))
            for score, _, chapter_index in medium_candidates[:2]:
                _ = score
                chosen_chapters.add(chapter_index)

            final_rows = [item for item in scored_rows if item[2] in chosen_chapters]
            final_rows.sort(key=lambda item: (-item[0], item[2]))
            for score, reasons, chapter_index in final_rows[:MAX_RELATION_CHAPTERS_PER_VOLUME]:
                entry = entry_map.get(chapter_index)
                if entry is None:
                    continue
                selected[chapter_index] = {
                    "score": score,
                    "reasons": reasons,
                    "record_description": entry.record_description,
                    "chapter_summary": str(chapter_rows.get(chapter_index, {}).get("chapter_summary") or entry.chapter_summary).strip(),
                    "is_context": False,
                }

        expanded_indexes = _expand_chapter_indexes(set(selected.keys()), available_chapters)
        packets: list[dict[str, Any]] = []
        for chapter_index in expanded_indexes:
            base = selected.get(
                chapter_index,
                {
                    "score": 0,
                    "reasons": ["邻接上下文"],
                    "record_description": "",
                    "chapter_summary": str(chapter_rows.get(chapter_index, {}).get("chapter_summary") or "").strip(),
                    "is_context": True,
                },
            )
            packet = {"chapter_index": chapter_index, **base}
            packets.append(packet)
        selected_by_target[target_name] = packets
    return selected_by_target


def _extract_relevant_content_excerpt(content: str, keywords: list[str], max_chars: int = MAX_CHAPTER_CONTENT_EXCERPT_CHARS) -> str:
    text = str(content or "").strip()
    if not text:
        return ""
    spans: list[tuple[int, int]] = []
    for keyword in keywords:
        kw = str(keyword or "").strip()
        if not kw:
            continue
        position = text.find(kw)
        if position < 0:
            continue
        start = max(0, position - max_chars // 3)
        end = min(len(text), position + max_chars // 2)
        spans.append((start, end))
    if not spans:
        return text[:max_chars]
    spans.sort()
    parts: list[str] = []
    consumed = 0
    for start, end in spans:
        excerpt = text[start:end].strip()
        if not excerpt:
            continue
        remaining = max_chars - consumed
        if remaining <= 0:
            break
        excerpt = excerpt[:remaining]
        parts.append(excerpt)
        consumed += len(excerpt)
    merged = "\n...\n".join(parts).strip()
    return merged[:max_chars]


def _build_profile_critical_packets(
    chapter_rows: dict[int, dict[str, Any]],
    chapter_contents: dict[int, str],
    selected: dict[int, dict[str, Any]],
    aliases: list[str],
    volume_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    packets: list[dict[str, Any]] = []
    for chapter_index, info in selected.items():
        volume_index, volume_title = _resolve_volume(int(chapter_index), volume_rows)
        packets.append(
            {
                "chapter_index": int(chapter_index),
                "volume_index": volume_index,
                "volume_title": volume_title,
                "score": int(info.get("score") or 0),
                "reasons": _merge_reason_list(info.get("reasons") or []),
                "record_description": str(info.get("record_description") or "").strip(),
                "chapter_summary": str(info.get("chapter_summary") or chapter_rows.get(chapter_index, {}).get("chapter_summary") or "").strip(),
                "chapter_content_excerpt": _extract_relevant_content_excerpt(
                    chapter_contents.get(chapter_index, ""),
                    aliases,
                ),
                "is_context": bool(info.get("is_context")),
            }
        )
    packets.sort(key=lambda item: item["chapter_index"])
    return packets


def _build_relation_critical_packets(
    selected_by_target: dict[str, list[dict[str, Any]]],
    chapter_contents: dict[int, str],
    source_aliases: list[str],
    alias_lookup: dict[str, list[str]],
    volume_rows: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    packets_by_target: dict[str, list[dict[str, Any]]] = {}
    for target_name, items in selected_by_target.items():
        target_aliases = alias_lookup.get(target_name, [target_name])
        keywords = _dedupe_str_list(source_aliases + target_aliases)
        packets: list[dict[str, Any]] = []
        for item in items:
            chapter_index = int(item.get("chapter_index") or 0)
            volume_index, volume_title = _resolve_volume(chapter_index, volume_rows)
            packets.append(
                {
                    "chapter_index": chapter_index,
                    "volume_index": volume_index,
                    "volume_title": volume_title,
                    "score": int(item.get("score") or 0),
                    "reasons": _merge_reason_list(item.get("reasons") or []),
                    "record_description": str(item.get("record_description") or "").strip(),
                    "chapter_summary": str(item.get("chapter_summary") or "").strip(),
                    "chapter_content_excerpt": _extract_relevant_content_excerpt(
                        chapter_contents.get(chapter_index, ""),
                        keywords,
                    ),
                    "is_context": bool(item.get("is_context")),
                }
            )
        packets.sort(key=lambda item: item["chapter_index"])
        packets_by_target[target_name] = packets
    return packets_by_target


def _build_roleplay_window_payloads(
    windows: list[VolumeWindow],
    chapter_contents: dict[int, str],
    aliases: list[str],
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for window in windows:
        chapters_payload: list[dict[str, Any]] = []
        for entry in window.entries:
            chapters_payload.append(
                {
                    "chapter_index": entry.chapter_index,
                    "record_description": entry.record_description,
                    "chapter_summary": entry.chapter_summary,
                    "chapter_content_excerpt": _extract_relevant_content_excerpt(
                        chapter_contents.get(entry.chapter_index, ""),
                        aliases,
                    ),
                }
            )
        payloads.append(
            {
                "window": window,
                "chapters_payload": chapters_payload,
            }
        )
    return payloads


def _chunk_packets_by_volume(packets: list[dict[str, Any]], *, max_chapters: int = MAX_CRITICAL_CHAPTERS_PER_CHUNK) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for packet in packets:
        grouped[(int(packet.get("volume_index") or 0), str(packet.get("volume_title") or "").strip())].append(packet)
    chunks: list[dict[str, Any]] = []
    for (volume_index, volume_title), rows in sorted(grouped.items(), key=lambda item: item[0][0]):
        ordered = sorted(rows, key=lambda item: int(item.get("chapter_index") or 0))
        chunk_index = 1
        for start in range(0, len(ordered), max_chapters):
            batch = ordered[start : start + max_chapters]
            source_chapters = [int(item.get("chapter_index") or 0) for item in batch if int(item.get("chapter_index") or 0) > 0]
            chunks.append(
                {
                    "volume_index": volume_index,
                    "volume_title": volume_title,
                    "chunk_index": chunk_index,
                    "chapter_start": min(source_chapters) if source_chapters else 0,
                    "chapter_end": max(source_chapters) if source_chapters else 0,
                    "source_chapters": source_chapters,
                    "critical_chapters": batch,
                }
            )
            chunk_index += 1
    return chunks


def _list_valid_characters(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row["need_delete"] != "yes" and row["id"] > 0 and row["name"]]


def list_book_character_cards(book_id: int, limit: int | None = 10) -> list[dict[str, Any]]:
    with _connect() as conn:
        with conn.cursor() as cursor:
            rows = _list_valid_characters(_load_book_characters(cursor, int(book_id)))

    cards: list[dict[str, Any]] = []
    for row in rows:
        chapter_indexes = [chapter_index for chapter_index, _ in row["records"]]
        alias_preview = [alias for alias in row["aliases"] if alias != row["name"]][:3]
        cards.append(
            {
                "id": row["id"],
                "book_id": row["book_id"],
                "name": row["name"],
                "aliases": row["aliases"],
                "alias_preview": alias_preview,
                "record_count": len(row["records"]),
                "first_chapter_index": min(chapter_indexes) if chapter_indexes else 0,
                "last_chapter_index": max(chapter_indexes) if chapter_indexes else 0,
            }
        )
    cards.sort(key=lambda item: (-int(item["record_count"]), int(item["id"])))
    if limit is None:
        return cards
    normalized_limit = int(limit)
    if normalized_limit <= 0:
        return cards
    return cards[: max(1, normalized_limit)]


def _load_cached_profile(cursor: Any, book_id: int, character_id: int, version_hash: str) -> dict[str, Any] | None:
    cursor.execute(
        """
        SELECT character_name, aliases_json, first_chapter_index, last_chapter_index, record_count, profile_json, source_chapters_json, version_hash
        FROM character_profiles
        WHERE book_id = %s AND character_id = %s
        LIMIT 1
        """,
        (int(book_id), int(character_id)),
    )
    row = cursor.fetchone() or {}
    if not row or str(row.get("version_hash") or "") != version_hash:
        return None
    aliases = _dedupe_str_list(_parse_json_list(row.get("aliases_json")))
    first_chapter_index = int(row.get("first_chapter_index") or 0)
    last_chapter_index = int(row.get("last_chapter_index") or 0)
    return {
        "character_name": str(row.get("character_name") or "").strip(),
        "aliases_json": aliases,
        "first_chapter_index": first_chapter_index,
        "last_chapter_index": last_chapter_index,
        "record_count": int(row.get("record_count") or 0),
        "profile_json": _normalize_profile_json(
            _parse_json_dict(row.get("profile_json")),
            {},
            first_chapter_index,
            last_chapter_index,
            aliases,
        ),
        "source_chapters_json": _normalize_int_list(_parse_json_list(row.get("source_chapters_json"))),
        "version_hash": str(row.get("version_hash") or "").strip(),
    }


def _load_cached_relations(cursor: Any, book_id: int, character_id: int, version_hash: str) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT target_character_id, target_character_name, summary, relation_model_json, history_json, first_chapter_index, last_chapter_index
        FROM character_relations
        WHERE book_id = %s AND source_character_id = %s AND version_hash = %s
        ORDER BY first_chapter_index ASC, target_character_name ASC
        """,
        (int(book_id), int(character_id), version_hash),
    )
    rows = cursor.fetchall() or []
    relations: list[dict[str, Any]] = []
    for row in rows:
        relations.append(
            {
                "target_character_id": int(row.get("target_character_id") or 0) or None,
                "target_character_name": str(row.get("target_character_name") or "").strip(),
                "summary": str(row.get("summary") or "").strip(),
                "relation_model_json": _parse_json_dict(row.get("relation_model_json")),
                "history_json": _normalize_relation_history(_parse_json_list(row.get("history_json"))),
                "first_chapter_index": int(row.get("first_chapter_index") or 0),
                "last_chapter_index": int(row.get("last_chapter_index") or 0),
            }
        )
    return relations


def _load_cached_profile_chunks(cursor: Any, book_id: int, character_id: int, version_hash: str) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT id, volume_index, chunk_index, chapter_start, chapter_end, source_chapters_json, chunk_json
        FROM character_profile_chunks
        WHERE book_id = %s AND character_id = %s AND version_hash = %s
        ORDER BY volume_index ASC, chunk_index ASC
        """,
        (int(book_id), int(character_id), version_hash),
    )
    rows = cursor.fetchall() or []
    return [
        {
            "id": int(row.get("id") or 0),
            "volume_index": int(row.get("volume_index") or 0),
            "chunk_index": int(row.get("chunk_index") or 0),
            "chapter_start": int(row.get("chapter_start") or 0),
            "chapter_end": int(row.get("chapter_end") or 0),
            "source_chapters": _normalize_int_list(_parse_json_list(row.get("source_chapters_json"))),
            "chunk_json": _parse_json_dict(row.get("chunk_json")),
        }
        for row in rows
    ]


def _load_cached_profile_volume_groups(cursor: Any, book_id: int, character_id: int, version_hash: str) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT id, volume_index, chunk_ids_json, group_json
        FROM character_profile_volume_groups
        WHERE book_id = %s AND character_id = %s AND version_hash = %s
        ORDER BY volume_index ASC
        """,
        (int(book_id), int(character_id), version_hash),
    )
    rows = cursor.fetchall() or []
    return [
        {
            "id": int(row.get("id") or 0),
            "volume_index": int(row.get("volume_index") or 0),
            "chunk_ids": _normalize_int_list(_parse_json_list(row.get("chunk_ids_json"))),
            "group_json": _parse_json_dict(row.get("group_json")),
        }
        for row in rows
    ]


def _load_cached_relation_chunks(cursor: Any, book_id: int, source_character_id: int, version_hash: str) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT id, target_character_id, target_character_name, volume_index, chunk_index, chapter_start, chapter_end, source_chapters_json, chunk_json
        FROM character_relation_chunks
        WHERE book_id = %s AND source_character_id = %s AND version_hash = %s
        ORDER BY target_character_name ASC, volume_index ASC, chunk_index ASC
        """,
        (int(book_id), int(source_character_id), version_hash),
    )
    rows = cursor.fetchall() or []
    return [
        {
            "id": int(row.get("id") or 0),
            "target_character_id": int(row.get("target_character_id") or 0) or None,
            "target_character_name": str(row.get("target_character_name") or "").strip(),
            "volume_index": int(row.get("volume_index") or 0),
            "chunk_index": int(row.get("chunk_index") or 0),
            "chapter_start": int(row.get("chapter_start") or 0),
            "chapter_end": int(row.get("chapter_end") or 0),
            "source_chapters": _normalize_int_list(_parse_json_list(row.get("source_chapters_json"))),
            "chunk_json": _parse_json_dict(row.get("chunk_json")),
        }
        for row in rows
    ]


def _load_cached_relation_volume_groups(cursor: Any, book_id: int, source_character_id: int, version_hash: str) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT id, target_character_id, target_character_name, volume_index, chunk_ids_json, group_json
        FROM character_relation_volume_groups
        WHERE book_id = %s AND source_character_id = %s AND version_hash = %s
        ORDER BY target_character_name ASC, volume_index ASC
        """,
        (int(book_id), int(source_character_id), version_hash),
    )
    rows = cursor.fetchall() or []
    return [
        {
            "id": int(row.get("id") or 0),
            "target_character_id": int(row.get("target_character_id") or 0) or None,
            "target_character_name": str(row.get("target_character_name") or "").strip(),
            "volume_index": int(row.get("volume_index") or 0),
            "chunk_ids": _normalize_int_list(_parse_json_list(row.get("chunk_ids_json"))),
            "group_json": _parse_json_dict(row.get("group_json")),
        }
        for row in rows
    ]


def get_character_archive_snapshot(book_id: int, character_id: int) -> dict[str, Any]:
    with _connect() as conn:
        with conn.cursor() as cursor:
            context = _load_character_context(cursor, int(book_id), int(character_id))
            character_row = context["character_row"]
            cached_profile = _load_cached_profile(cursor, book_id, character_id, context["version_hash"])
            cached_relations = _load_cached_relations(cursor, book_id, character_id, context["version_hash"])

    chapter_indexes = [entry.chapter_index for entry in context["entries"]]
    alias_preview = [alias for alias in character_row["aliases"] if alias != character_row["name"]][:3]
    return {
        "character": {
            "id": character_row["id"],
            "book_id": character_row["book_id"],
            "name": character_row["name"],
            "aliases": character_row["aliases"],
            "alias_preview": alias_preview,
            "record_count": len(character_row["records"]),
            "first_chapter_index": min(chapter_indexes) if chapter_indexes else 0,
            "last_chapter_index": max(chapter_indexes) if chapter_indexes else 0,
            "source_chapters": chapter_indexes,
        },
        "profile": cached_profile["profile_json"] if cached_profile else None,
        "relations": cached_relations,
        "has_cached_result": cached_profile is not None,
        "version_hash": context["version_hash"],
    }


def _build_alias_maps(rows: list[dict[str, Any]]) -> tuple[dict[str, str | None], dict[str, int]]:
    alias_map: dict[str, str | None] = {}
    id_map: dict[str, int] = {}
    for row in rows:
        canonical_name = str(row.get("name") or "").strip()
        if not canonical_name:
            continue
        canonical_key = _normalize_text_key(canonical_name)
        if canonical_key:
            id_map[canonical_key] = int(row.get("id") or 0)
        for alias in [canonical_name, *row.get("aliases", [])]:
            alias_key = _normalize_text_key(alias)
            if not alias_key:
                continue
            existing = alias_map.get(alias_key)
            if existing is None and alias_key in alias_map:
                continue
            if existing and existing != canonical_name:
                alias_map[alias_key] = None
                continue
            alias_map[alias_key] = canonical_name
    return alias_map, id_map


def _resolve_target_character(
    raw_name: str,
    alias_map: dict[str, str | None],
    id_map: dict[str, int],
) -> tuple[int | None, str]:
    raw_text = str(raw_name or "").strip()
    for candidate in _build_name_resolution_candidates(raw_text):
        normalized_key = _normalize_text_key(candidate)
        if not normalized_key:
            continue
        matched_name = alias_map.get(normalized_key)
        if matched_name is None:
            if normalized_key in alias_map:
                continue
            continue
        return id_map.get(_normalize_text_key(matched_name)), matched_name
    return None, raw_text


def _invoke_json_once(
    prompt: str,
    *,
    remind_invalid_json: bool = False,
    model_name_override: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    llm = build_llm(model_name_override)
    effective_prompt = prompt
    if remind_invalid_json:
        effective_prompt = f"{prompt}\n\n{INVALID_JSON_REMINDER}"
    response = llm.invoke(effective_prompt)
    raw_text = _stringify_content(getattr(response, "content", response))
    parsed = _extract_json_object(raw_text)
    return parsed, raw_text


def _invoke_json_until_valid(
    prompt: str,
    *,
    log_label: str,
    remind_invalid_json: bool = False,
    attempt: int = 1,
) -> dict[str, Any]:
    while True:
        model_name_override = JSON_RETRY_FALLBACK_MODEL if attempt >= 3 else None
        if attempt == 3:
            logger.warning(
                "[character_profiles] switching model after repeated invalid JSON. label=%s model=%s",
                log_label,
                JSON_RETRY_FALLBACK_MODEL,
            )
        parsed, raw_text = _invoke_json_once(
            prompt,
            remind_invalid_json=remind_invalid_json,
            model_name_override=model_name_override,
        )
        if parsed is not None:
            return parsed
        logger.warning(
            "[character_profiles] invalid JSON. label=%s attempt=%d preview=%s",
            log_label,
            attempt,
            raw_text[:600].replace("\n", "\\n"),
        )
        remind_invalid_json = True
        attempt += 1
        time.sleep(INVALID_JSON_RETRY_DELAY_SECONDS)


def _split_window(window: VolumeWindow) -> tuple[VolumeWindow, VolumeWindow] | None:
    entries = list(window.entries)
    if len(entries) <= 1:
        return None
    middle = max(1, len(entries) // 2)
    left_entries = tuple(entries[:middle])
    right_entries = tuple(entries[middle:])
    if not left_entries or not right_entries:
        return None
    return (
        VolumeWindow(
            volume_index=window.volume_index,
            volume_title=window.volume_title,
            chapter_start=left_entries[0].chapter_index,
            chapter_end=left_entries[-1].chapter_index,
            entries=left_entries,
        ),
        VolumeWindow(
            volume_index=window.volume_index,
            volume_title=window.volume_title,
            chapter_start=right_entries[0].chapter_index,
            chapter_end=right_entries[-1].chapter_index,
            entries=right_entries,
        ),
    )


def _window_log_label(character_name: str, window: VolumeWindow) -> str:
    return (
        f"{character_name}|volume={window.volume_index}|chapters={window.chapter_start}-{window.chapter_end}"
        f"|count={len(window.entries)}|chars={_window_char_count(window.entries)}"
    )


def _relation_log_label(character_name: str, target_character_name: str, history_json: list[dict[str, Any]]) -> str:
    if history_json:
        chapter_start = min(item["chapter_start"] for item in history_json)
        chapter_end = max(item["chapter_end"] for item in history_json)
    else:
        chapter_start = 0
        chapter_end = 0
    return f"{character_name}->{target_character_name}|chapters={chapter_start}-{chapter_end}|segments={len(history_json)}"


def _final_profile_log_label(character_name: str, profile_slices: list[dict[str, Any]]) -> str:
    return f"{character_name}|profile_slices={len(profile_slices)}"


def _profile_chunk_log_label(character_name: str, chunk: dict[str, Any]) -> str:
    return (
        f"{character_name}|profile_chunk|volume={int(chunk.get('volume_index') or 0)}"
        f"|index={int(chunk.get('chunk_index') or 0)}|chapters={int(chunk.get('chapter_start') or 0)}-{int(chunk.get('chapter_end') or 0)}"
    )


def _profile_group_log_label(character_name: str, volume_index: int) -> str:
    return f"{character_name}|profile_group|volume={int(volume_index or 0)}"


def _relation_chunk_log_label(character_name: str, target_character_name: str, chunk: dict[str, Any]) -> str:
    return (
        f"{character_name}->{target_character_name}|relation_chunk|volume={int(chunk.get('volume_index') or 0)}"
        f"|index={int(chunk.get('chunk_index') or 0)}|chapters={int(chunk.get('chapter_start') or 0)}-{int(chunk.get('chapter_end') or 0)}"
    )


def _relation_group_log_label(character_name: str, target_character_name: str, volume_index: int) -> str:
    return f"{character_name}->{target_character_name}|relation_group|volume={int(volume_index or 0)}"


def _history_segment_log_label(character_name: str, target_character_name: str, history_segment: dict[str, Any]) -> str:
    return (
        f"{character_name}->{target_character_name}|history_segment"
        f"|chapters={int(history_segment.get('chapter_start') or 0)}-{int(history_segment.get('chapter_end') or 0)}"
    )


def _history_segment_overlaps_group(history_segment: dict[str, Any], relation_group: dict[str, Any]) -> bool:
    segment_start = int(history_segment.get("chapter_start") or 0)
    segment_end = int(history_segment.get("chapter_end") or 0)
    for item in relation_group.get("history_json", []) if isinstance(relation_group.get("history_json"), list) else []:
        if not isinstance(item, dict):
            continue
        group_start = int(item.get("chapter_start") or 0)
        group_end = int(item.get("chapter_end") or 0) or group_start
        if segment_start <= group_end and group_start <= segment_end:
            return True
    return False


def _select_relevant_relation_groups_for_segment(
    history_segment: dict[str, Any],
    relation_volume_groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    relevant = [group for group in relation_volume_groups if _history_segment_overlaps_group(history_segment, group)]
    if relevant:
        return relevant
    if relation_volume_groups:
        return relation_volume_groups[-1:]
    return []


def _normalize_window_outputs(
    parsed: dict[str, Any],
    window: VolumeWindow,
    source_character_name: str,
    alias_map: dict[str, str | None],
    id_map: dict[str, int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return (
        [_normalize_profile_slice(parsed, window)],
        _normalize_relation_events(parsed, window, source_character_name, alias_map, id_map),
    )


def _normalize_profile_chunk_json(raw: dict[str, Any], chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        "volume_index": int(raw.get("volume_index") or chunk.get("volume_index") or 0),
        "volume_title": str(raw.get("volume_title") or chunk.get("volume_title") or "").strip(),
        "chapter_start": int(raw.get("chapter_start") or chunk.get("chapter_start") or 0),
        "chapter_end": int(raw.get("chapter_end") or chunk.get("chapter_end") or 0),
        "summary": str(raw.get("summary") or "").strip(),
        "narrative_role_signals": _dedupe_str_list(raw.get("narrative_role_signals") if isinstance(raw.get("narrative_role_signals"), list) else []),
        "personality_and_style_signals": _dedupe_str_list(raw.get("personality_and_style_signals") if isinstance(raw.get("personality_and_style_signals"), list) else []),
        "appearance_signals": _dedupe_str_list(raw.get("appearance_signals") if isinstance(raw.get("appearance_signals"), list) else []),
        "goals_and_motivation_signals": _dedupe_str_list(raw.get("goals_and_motivation_signals") if isinstance(raw.get("goals_and_motivation_signals"), list) else []),
        "stance_and_alignment_signals": _dedupe_str_list(raw.get("stance_and_alignment_signals") if isinstance(raw.get("stance_and_alignment_signals"), list) else []),
        "abilities_and_resources_signals": _dedupe_str_list(raw.get("abilities_and_resources_signals") if isinstance(raw.get("abilities_and_resources_signals"), list) else []),
        "turning_point_signals": _dedupe_str_list(raw.get("turning_point_signals") if isinstance(raw.get("turning_point_signals"), list) else []),
        "important_relationship_signals": _dedupe_str_list(raw.get("important_relationship_signals") if isinstance(raw.get("important_relationship_signals"), list) else []),
        "evidence_chapters": _normalize_int_list(raw.get("evidence_chapters")),
    }


def _normalize_profile_volume_group_json(raw: dict[str, Any], volume_index: int, volume_title: str) -> dict[str, Any]:
    return {
        "volume_index": int(raw.get("volume_index") or volume_index),
        "volume_title": str(raw.get("volume_title") or volume_title).strip() or volume_title,
        "summary": str(raw.get("summary") or "").strip(),
        "role_in_volume": _dedupe_str_list(raw.get("role_in_volume") if isinstance(raw.get("role_in_volume"), list) else []),
        "goals": _dedupe_str_list(raw.get("goals") if isinstance(raw.get("goals"), list) else []),
        "state_changes": _dedupe_str_list(raw.get("state_changes") if isinstance(raw.get("state_changes"), list) else []),
        "relationship_changes": _dedupe_str_list(raw.get("relationship_changes") if isinstance(raw.get("relationship_changes"), list) else []),
        "narrative_role_signals": _dedupe_str_list(raw.get("narrative_role_signals") if isinstance(raw.get("narrative_role_signals"), list) else []),
        "personality_and_style_signals": _dedupe_str_list(raw.get("personality_and_style_signals") if isinstance(raw.get("personality_and_style_signals"), list) else []),
        "appearance_signals": _dedupe_str_list(raw.get("appearance_signals") if isinstance(raw.get("appearance_signals"), list) else []),
        "goals_and_motivation_signals": _dedupe_str_list(raw.get("goals_and_motivation_signals") if isinstance(raw.get("goals_and_motivation_signals"), list) else []),
        "stance_and_alignment_signals": _dedupe_str_list(raw.get("stance_and_alignment_signals") if isinstance(raw.get("stance_and_alignment_signals"), list) else []),
        "abilities_and_resources_signals": _dedupe_str_list(raw.get("abilities_and_resources_signals") if isinstance(raw.get("abilities_and_resources_signals"), list) else []),
        "turning_point_signals": _dedupe_str_list(raw.get("turning_point_signals") if isinstance(raw.get("turning_point_signals"), list) else []),
    }


def _normalize_relation_chunk_json(raw: dict[str, Any], chunk: dict[str, Any]) -> dict[str, Any]:
    history_candidates = raw.get("history_candidates") if isinstance(raw.get("history_candidates"), list) else []
    normalized_history_candidates: list[dict[str, Any]] = []
    for item in history_candidates:
        if not isinstance(item, dict):
            continue
        normalized_history_candidates.append(
            {
                "chapter_start": int(item.get("chapter_start") or chunk.get("chapter_start") or 0),
                "chapter_end": int(item.get("chapter_end") or chunk.get("chapter_end") or 0),
                "summary": str(item.get("summary") or "").strip(),
            }
        )
    return {
        "volume_index": int(raw.get("volume_index") or chunk.get("volume_index") or 0),
        "volume_title": str(raw.get("volume_title") or chunk.get("volume_title") or "").strip(),
        "chapter_start": int(raw.get("chapter_start") or chunk.get("chapter_start") or 0),
        "chapter_end": int(raw.get("chapter_end") or chunk.get("chapter_end") or 0),
        "summary": str(raw.get("summary") or "").strip(),
        "structural_relation_signals": _dedupe_str_list(raw.get("structural_relation_signals") if isinstance(raw.get("structural_relation_signals"), list) else []),
        "action_relation_signals": _dedupe_str_list(raw.get("action_relation_signals") if isinstance(raw.get("action_relation_signals"), list) else []),
        "emotional_relation_signals": _dedupe_str_list(raw.get("emotional_relation_signals") if isinstance(raw.get("emotional_relation_signals"), list) else []),
        "directionality_signals": _dedupe_str_list(raw.get("directionality_signals") if isinstance(raw.get("directionality_signals"), list) else []),
        "stability_signals": _dedupe_str_list(raw.get("stability_signals") if isinstance(raw.get("stability_signals"), list) else []),
        "current_status_signals": _dedupe_str_list(raw.get("current_status_signals") if isinstance(raw.get("current_status_signals"), list) else []),
        "drivers": _dedupe_str_list(raw.get("drivers") if isinstance(raw.get("drivers"), list) else []),
        "history_candidates": normalized_history_candidates,
        "evidence_chapters": _normalize_int_list(raw.get("evidence_chapters")),
    }


def _normalize_relation_volume_group_json(raw: dict[str, Any], volume_index: int, volume_title: str) -> dict[str, Any]:
    return {
        "volume_index": int(raw.get("volume_index") or volume_index),
        "volume_title": str(raw.get("volume_title") or volume_title).strip() or volume_title,
        "summary": str(raw.get("summary") or "").strip(),
        "structural_relation": _dedupe_str_list(raw.get("structural_relation") if isinstance(raw.get("structural_relation"), list) else []),
        "action_relation": _dedupe_str_list(raw.get("action_relation") if isinstance(raw.get("action_relation"), list) else []),
        "emotional_relation": _dedupe_str_list(raw.get("emotional_relation") if isinstance(raw.get("emotional_relation"), list) else []),
        "directionality": str(raw.get("directionality") or "").strip(),
        "stability": str(raw.get("stability") or "").strip(),
        "current_status": str(raw.get("current_status") or "").strip(),
        "drivers": _dedupe_str_list(raw.get("drivers") if isinstance(raw.get("drivers"), list) else []),
        "history_json": _normalize_relation_history(raw.get("history_json") if isinstance(raw.get("history_json"), list) else []),
    }


def _generate_single_window(
    character_name: str,
    window: VolumeWindow,
    alias_map: dict[str, str | None],
    id_map: dict[str, int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    chapters_payload = [
        {
            "chapter_index": entry.chapter_index,
            "record_description": entry.record_description,
            "chapter_summary": entry.chapter_summary,
        }
        for entry in window.entries
    ]
    prompt = CHARACTER_PROFILE_SLICE_PROMPT.format(
        character_name=character_name,
        volume_index=window.volume_index,
        volume_title=window.volume_title,
        chapter_start=window.chapter_start,
        chapter_end=window.chapter_end,
        chapters_json=json.dumps(chapters_payload, ensure_ascii=False, indent=2),
    )
    log_label = _window_log_label(character_name, window)
    logger.info("[character_profiles] start window. label=%s", log_label)

    parsed, raw_text = _invoke_json_once(prompt, remind_invalid_json=False)
    if parsed is not None:
        logger.info("[character_profiles] window success. label=%s attempt=1", log_label)
        return _normalize_window_outputs(parsed, window, character_name, alias_map, id_map)

    logger.warning(
        "[character_profiles] invalid JSON. label=%s attempt=1 preview=%s",
        log_label,
        raw_text[:600].replace("\n", "\\n"),
    )
    parsed, raw_text = _invoke_json_once(prompt, remind_invalid_json=True)
    if parsed is not None:
        logger.info("[character_profiles] window success. label=%s attempt=2", log_label)
        return _normalize_window_outputs(parsed, window, character_name, alias_map, id_map)

    logger.warning(
        "[character_profiles] invalid JSON. label=%s attempt=2 preview=%s",
        log_label,
        raw_text[:600].replace("\n", "\\n"),
    )
    split_result = _split_window(window)
    if split_result is not None:
        left_window, right_window = split_result
        logger.warning(
            "[character_profiles] split window after invalid JSON. label=%s left=%s-%s right=%s-%s",
            log_label,
            left_window.chapter_start,
            left_window.chapter_end,
            right_window.chapter_start,
            right_window.chapter_end,
        )
        left_slices, left_relations = _generate_single_window(character_name, left_window, alias_map, id_map)
        right_slices, right_relations = _generate_single_window(character_name, right_window, alias_map, id_map)
        return left_slices + right_slices, left_relations + right_relations

    parsed = _invoke_json_until_valid(prompt, log_label=log_label, remind_invalid_json=True, attempt=3)
    logger.info("[character_profiles] window success. label=%s attempt>=3", log_label)
    return _normalize_window_outputs(parsed, window, character_name, alias_map, id_map)


def _normalize_profile_slice(raw: dict[str, Any], window: VolumeWindow) -> dict[str, Any]:
    slice_obj = raw.get("profile_slice", raw) if isinstance(raw, dict) else {}
    if not isinstance(slice_obj, dict):
        slice_obj = {}
    return {
        "volume_index": int(slice_obj.get("volume_index") or window.volume_index),
        "volume_title": str(slice_obj.get("volume_title") or window.volume_title).strip() or window.volume_title,
        "chapter_start": int(slice_obj.get("chapter_start") or window.chapter_start),
        "chapter_end": int(slice_obj.get("chapter_end") or window.chapter_end),
        "summary": str(slice_obj.get("summary") or "").strip(),
        "stable_signals": _dedupe_str_list(slice_obj.get("stable_signals") if isinstance(slice_obj.get("stable_signals"), list) else []),
        "current_state_signals": _dedupe_str_list(
            slice_obj.get("current_state_signals") if isinstance(slice_obj.get("current_state_signals"), list) else []
        ),
        "key_events": _dedupe_str_list(slice_obj.get("key_events") if isinstance(slice_obj.get("key_events"), list) else []),
    }


def _normalize_relation_history(history: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        try:
            chapter_start = int(item.get("chapter_start") or 0)
            chapter_end = int(item.get("chapter_end") or 0)
        except (TypeError, ValueError):
            continue
        if chapter_start <= 0:
            continue
        if chapter_end <= 0:
            chapter_end = chapter_start
        if chapter_end < chapter_start:
            chapter_start, chapter_end = chapter_end, chapter_start
        normalized.append(
            {
                "chapter_start": chapter_start,
                "chapter_end": chapter_end,
                "relation_type": str(item.get("relation_type") or "").strip(),
                "structural_relation": _dedupe_str_list(item.get("structural_relation") if isinstance(item.get("structural_relation"), list) else []),
                "action_relation": _dedupe_str_list(item.get("action_relation") if isinstance(item.get("action_relation"), list) else []),
                "emotional_relation": _dedupe_str_list(item.get("emotional_relation") if isinstance(item.get("emotional_relation"), list) else []),
                "polarity": str(item.get("polarity") or "").strip() or "neutral",
                "strength": str(item.get("strength") or "").strip() or "medium",
                "directionality": str(item.get("directionality") or "").strip(),
                "stability": str(item.get("stability") or "").strip(),
                "current_status": str(item.get("current_status") or "").strip(),
                "drivers": _dedupe_str_list(item.get("drivers") if isinstance(item.get("drivers"), list) else []),
                "summary": str(item.get("summary") or "").strip(),
                "evidence_chapters": _normalize_int_list(item.get("evidence_chapters")),
            }
        )
    normalized.sort(key=lambda item: (item["chapter_start"], item["chapter_end"], item["relation_type"], item["summary"]))
    return normalized


def _normalize_relation_events(
    raw: dict[str, Any],
    window: VolumeWindow,
    source_character_name: str,
    alias_map: dict[str, str | None],
    id_map: dict[str, int],
) -> list[dict[str, Any]]:
    events = raw.get("relation_events", []) if isinstance(raw, dict) else []
    if not isinstance(events, list):
        return []
    normalized: list[dict[str, Any]] = []
    source_key = _normalize_text_key(source_character_name)
    for item in events:
        if not isinstance(item, dict):
            continue
        target_raw = str(item.get("target_character") or item.get("target") or "").strip()
        target_character_id, target_character_name = _resolve_target_character(target_raw, alias_map, id_map)
        if not target_character_name:
            continue
        if _normalize_text_key(target_character_name) == source_key:
            continue
        try:
            chapter_start = int(item.get("chapter_start") or window.chapter_start)
            chapter_end = int(item.get("chapter_end") or chapter_start)
        except (TypeError, ValueError):
            chapter_start = window.chapter_start
            chapter_end = window.chapter_end
        chapter_start = max(window.chapter_start, chapter_start)
        chapter_end = min(window.chapter_end, max(chapter_start, chapter_end))
        evidence = [chapter for chapter in _normalize_int_list(item.get("evidence_chapters")) if chapter_start <= chapter <= chapter_end]
        if not evidence:
            evidence = [chapter_start]
        normalized.append(
            {
                "target_character_id": target_character_id,
                "target_character_name": target_character_name,
                "relation_type": str(item.get("relation_type") or "").strip(),
                "polarity": str(item.get("polarity") or "").strip() or "neutral",
                "strength": str(item.get("strength") or "").strip() or "medium",
                "summary": str(item.get("summary") or "").strip(),
                "chapter_start": chapter_start,
                "chapter_end": chapter_end,
                "evidence_chapters": evidence,
                "volume_index": window.volume_index,
            }
        )
    return normalized


def _normalize_roleplay_style_batch(raw: dict[str, Any]) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    for item in raw.get("style_samples", []) if isinstance(raw.get("style_samples"), list) else []:
        if not isinstance(item, dict):
            continue
        scene = str(item.get("scene") or "").strip()
        quote = str(item.get("quote") or "").strip()
        if not scene or not quote:
            continue
        samples.append(
            {
                "scene": scene,
                "quote": quote,
            }
        )
    return {
        "speech_style_signals": _dedupe_str_list(
            raw.get("speech_style_signals") if isinstance(raw.get("speech_style_signals"), list) else []
        ),
        "style_samples": samples,
    }


def _confidence_rank(value: str) -> int:
    mapping = {"low": 1, "medium": 2, "high": 3}
    return mapping.get(str(value or "").strip().lower(), 0)


def _aggregate_roleplay_style_batches(batch_results: list[dict[str, Any]]) -> dict[str, Any]:
    style_counts: dict[str, dict[str, Any]] = {}
    for batch in batch_results:
        for signal in batch.get("speech_style_signals", []) if isinstance(batch.get("speech_style_signals"), list) else []:
            text = str(signal or "").strip()
            if not text:
                continue
            key = _normalize_text_key(text)
            bucket = style_counts.setdefault(
                key,
                {"text": text, "occurrence_count": 0},
            )
            bucket["occurrence_count"] += 1

    sample_counts: dict[str, dict[str, Any]] = {}
    for batch in batch_results:
        for item in batch.get("style_samples", []) if isinstance(batch.get("style_samples"), list) else []:
            if not isinstance(item, dict):
                continue
            scene = str(item.get("scene") or "").strip()
            quote = str(item.get("quote") or "").strip()
            if not scene or not quote:
                continue
            key = _normalize_text_key(f"{scene}|{quote}")
            bucket = sample_counts.setdefault(
                key,
                {"scene": scene, "quote": quote, "occurrence_count": 0},
            )
            bucket["occurrence_count"] += 1

    speech_style_candidates = sorted(
        style_counts.values(),
        key=lambda item: (-int(item.get("occurrence_count") or 0), str(item.get("text") or "")),
    )
    style_samples = sorted(
        sample_counts.values(),
        key=lambda item: (
            -int(item.get("occurrence_count") or 0),
            str(item.get("scene") or ""),
            str(item.get("quote") or ""),
        ),
    )[:30]
    return {
        "speech_style_candidates": speech_style_candidates,
        "style_samples": style_samples,
        "batch_count": len(batch_results),
    }


def _normalize_roleplay_relation_batch(
    raw: dict[str, Any],
    window: VolumeWindow,
    source_character_name: str,
    alias_map: dict[str, str | None],
    id_map: dict[str, int],
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    source_key = _normalize_text_key(source_character_name)
    items = raw.get("emotional_relation_candidates", []) if isinstance(raw, dict) else []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        target_raw = str(item.get("target_character") or "").strip()
        target_character_id, target_character_name = _resolve_target_character(target_raw, alias_map, id_map)
        if not target_character_name or _normalize_text_key(target_character_name) == source_key:
            continue
        chapter_start = int(item.get("chapter_start") or window.chapter_start)
        chapter_end = int(item.get("chapter_end") or chapter_start)
        chapter_start = max(window.chapter_start, chapter_start)
        chapter_end = min(window.chapter_end, max(chapter_start, chapter_end))
        candidates.append(
            {
                "target_character_id": target_character_id,
                "target_character_name": target_character_name,
                "primary_relation_type": str(item.get("primary_relation_type") or "").strip(),
                "emotional_signals": _dedupe_str_list(
                    item.get("emotional_signals") if isinstance(item.get("emotional_signals"), list) else []
                ),
                "interaction_signals": _dedupe_str_list(
                    item.get("interaction_signals") if isinstance(item.get("interaction_signals"), list) else []
                ),
                "explicitness": str(item.get("explicitness") or "").strip().lower() or "implicit",
                "confidence": str(item.get("confidence") or "").strip().lower() or "low",
                "intensity": str(item.get("intensity") or "").strip().lower() or "medium",
                "chapter_start": chapter_start,
                "chapter_end": chapter_end,
                "evidence_chapters": _normalize_int_list(item.get("evidence_chapters")) or [chapter_start],
                "summary": str(item.get("summary") or "").strip(),
            }
        )
    return {"emotional_relation_candidates": candidates}


def _aggregate_roleplay_relation_candidates(batch_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[int | None, str], list[dict[str, Any]]] = defaultdict(list)
    for batch in batch_results:
        items = batch.get("emotional_relation_candidates", []) if isinstance(batch.get("emotional_relation_candidates"), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            primary_relation_type = str(item.get("primary_relation_type") or "").strip()
            target_name = str(item.get("target_character_name") or item.get("target_character") or "").strip()
            if not target_name or not primary_relation_type:
                continue
            grouped[(item.get("target_character_id"), target_name)].append(item)

    results: list[dict[str, Any]] = []
    for (target_character_id, target_character_name), items in sorted(grouped.items(), key=lambda entry: entry[0][1]):
        score = 0
        for item in items:
            intensity = str(item.get("intensity") or "").strip().lower()
            confidence = str(item.get("confidence") or "").strip().lower()
            score += 3 if intensity == "strong" else 2 if intensity == "medium" else 1
            score += 1 if confidence == "high" else 0 if confidence == "low" else 1
        if score < 3:
            continue
        results.append(
            {
                "target_character_id": target_character_id,
                "target_character_name": target_character_name,
                "score": score,
                "candidates": sorted(
                    items,
                    key=lambda item: (
                        int(item.get("chapter_start") or 0),
                        int(item.get("chapter_end") or 0),
                        str(item.get("primary_relation_type") or ""),
                    ),
                ),
            }
        )
    results.sort(key=lambda item: (-int(item.get("score") or 0), -len(item.get("candidates", [])), str(item.get("target_character_name") or "")))
    return results


def _normalize_roleplay_relation_summary(raw: dict[str, Any], fallback_target: str) -> dict[str, Any]:
    timeline: list[dict[str, Any]] = []
    for item in raw.get("timeline", []) if isinstance(raw.get("timeline"), list) else []:
        if not isinstance(item, dict):
            continue
        chapter_start = int(item.get("chapter_start") or 0)
        chapter_end = int(item.get("chapter_end") or 0) or chapter_start
        summary = str(item.get("summary") or "").strip()
        if chapter_start <= 0 or not summary:
            continue
        timeline.append(
            {
                "chapter_start": chapter_start,
                "chapter_end": max(chapter_start, chapter_end),
                "summary": summary,
            }
        )
    timeline.sort(key=lambda item: (item["chapter_start"], item["chapter_end"]))
    return {
        "target_character": str(raw.get("target_character") or fallback_target).strip() or fallback_target,
        "relation_summary": str(raw.get("relation_summary") or "").strip(),
        "primary_relation_type": str(raw.get("primary_relation_type") or "").strip(),
        "secondary_emotional_tendencies": _dedupe_str_list(
            raw.get("secondary_emotional_tendencies")
            if isinstance(raw.get("secondary_emotional_tendencies"), list)
            else []
        ),
        "intensity": str(raw.get("intensity") or "").strip().lower() or "medium",
        "current_status": str(raw.get("current_status") or "").strip(),
        "timeline": timeline,
    }


def _normalize_profile_json(
    raw: dict[str, Any],
    character_row: dict[str, Any],
    first_chapter_index: int,
    last_chapter_index: int,
    aliases: list[str],
) -> dict[str, Any]:
    identity = raw.get("identity", {}) if isinstance(raw, dict) and isinstance(raw.get("identity"), dict) else {}
    volume_arc_raw = raw.get("volume_arc", []) if isinstance(raw, dict) and isinstance(raw.get("volume_arc"), list) else []
    volume_arc: list[dict[str, Any]] = []
    for item in volume_arc_raw:
        if not isinstance(item, dict):
            continue
        try:
            volume_index = int(item.get("volume_index") or 0)
        except (TypeError, ValueError):
            volume_index = 0
        volume_arc.append(
            {
                "volume_index": volume_index,
                "volume_title": str(item.get("volume_title") or f"第{volume_index}卷").strip() if volume_index else str(item.get("volume_title") or "").strip(),
                "summary": str(item.get("summary") or "").strip(),
                "role_in_volume": _dedupe_str_list(item.get("role_in_volume") if isinstance(item.get("role_in_volume"), list) else []),
                "goals": _dedupe_str_list(item.get("goals") if isinstance(item.get("goals"), list) else []),
                "state_changes": _dedupe_str_list(item.get("state_changes") if isinstance(item.get("state_changes"), list) else []),
                "relationship_changes": _dedupe_str_list(item.get("relationship_changes") if isinstance(item.get("relationship_changes"), list) else []),
            }
        )
    emotional_relations: list[dict[str, Any]] = []
    for item in raw.get("emotional_relations", []) if isinstance(raw.get("emotional_relations"), list) else []:
        if not isinstance(item, dict):
            continue
        emotional_relations.append(
            _normalize_roleplay_relation_summary(item, str(item.get("target_character") or "").strip())
        )
    return {
        "identity": {
            "summary": str(identity.get("summary") or "").strip(),
            "aliases": _dedupe_str_list(identity.get("aliases") if isinstance(identity.get("aliases"), list) else aliases),
            "first_chapter_index": int(identity.get("first_chapter_index") or first_chapter_index),
            "last_chapter_index": int(identity.get("last_chapter_index") or last_chapter_index),
        },
        "narrative_role": _dedupe_str_list(raw.get("narrative_role") if isinstance(raw.get("narrative_role"), list) else []),
        "personality_and_style": _dedupe_str_list(
            raw.get("personality_and_style") if isinstance(raw.get("personality_and_style"), list) else []
        ),
        "appearance": _dedupe_str_list(raw.get("appearance") if isinstance(raw.get("appearance"), list) else []),
        "goals_and_motivation": _dedupe_str_list(
            raw.get("goals_and_motivation") if isinstance(raw.get("goals_and_motivation"), list) else []
        ),
        "stance_and_alignment": _dedupe_str_list(
            raw.get("stance_and_alignment") if isinstance(raw.get("stance_and_alignment"), list) else []
        ),
        "abilities_and_resources": _dedupe_str_list(
            raw.get("abilities_and_resources") if isinstance(raw.get("abilities_and_resources"), list) else []
        ),
        "stable_profile": _dedupe_str_list(raw.get("stable_profile") if isinstance(raw.get("stable_profile"), list) else []),
        "speech_style": _dedupe_str_list(raw.get("speech_style") if isinstance(raw.get("speech_style"), list) else []),
        "style_summary": str(raw.get("style_summary") or "").strip(),
        "style_samples": [
            {
                "scene": str(item.get("scene") or "").strip(),
                "quote": str(item.get("quote") or "").strip(),
            }
            for item in raw.get("style_samples", [])
            if isinstance(item, dict) and str(item.get("scene") or "").strip() and str(item.get("quote") or "").strip()
        ] if isinstance(raw.get("style_samples"), list) else [],
        "emotional_relations": emotional_relations,
        "volume_arc": volume_arc,
        "current_state": _dedupe_str_list(raw.get("current_state") if isinstance(raw.get("current_state"), list) else []),
        "turning_points": _dedupe_str_list(raw.get("turning_points") if isinstance(raw.get("turning_points"), list) else []),
        "key_events": _dedupe_str_list(raw.get("key_events") if isinstance(raw.get("key_events"), list) else []),
        "critical_evidence": [
            {
                "chapter_index": int(item.get("chapter_index") or 0),
                "reasons": _dedupe_str_list(item.get("reasons") if isinstance(item.get("reasons"), list) else []),
            }
            for item in raw.get("critical_evidence", [])
            if isinstance(item, dict) and int(item.get("chapter_index") or 0) > 0
        ] if isinstance(raw.get("critical_evidence"), list) else [],
    }


def _generate_window_outputs(
    character_name: str,
    windows: list[VolumeWindow],
    alias_map: dict[str, str | None],
    id_map: dict[str, int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not windows:
        return [], []
    total = len(windows)
    completed = 0
    profile_slices: list[dict[str, Any]] = []
    relation_events: list[dict[str, Any]] = []
    logger.warning(
        "[character_profiles] window generation started. character=%s total_windows=%d concurrency=%d",
        character_name,
        total,
        min(MAX_CONCURRENT_WINDOW_TASKS, total),
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_WINDOW_TASKS, total)) as executor:
        future_to_window = {
            executor.submit(_generate_single_window, character_name, window, alias_map, id_map): window
            for window in windows
        }
        for future in concurrent.futures.as_completed(future_to_window):
            window = future_to_window[future]
            slices, relations = future.result()
            profile_slices.extend(slices)
            relation_events.extend(relations)
            completed += 1
            logger.warning(
                "[character_profiles] windows %s %d/%d completed. character=%s last_window=%s-%s slices=%d relations=%d",
                _progress_bar(completed, total),
                completed,
                total,
                character_name,
                window.chapter_start,
                window.chapter_end,
                len(slices),
                len(relations),
            )
    profile_slices.sort(key=lambda item: (int(item.get("volume_index") or 0), int(item.get("chapter_start") or 0)))
    relation_events.sort(key=lambda item: (int(item.get("chapter_start") or 0), str(item.get("target_character_name") or "")))
    return profile_slices, relation_events


def _generate_single_profile_chunk(character_name: str, chunk: dict[str, Any]) -> dict[str, Any]:
    log_label = _profile_chunk_log_label(character_name, chunk)
    logger.info("[character_profiles] start profile chunk. label=%s", log_label)
    prompt = CHARACTER_PROFILE_CRITICAL_CHUNK_PROMPT.format(
        character_name=character_name,
        volume_index=int(chunk.get("volume_index") or 0),
        volume_title=str(chunk.get("volume_title") or "").strip(),
        chapter_start=int(chunk.get("chapter_start") or 0),
        chapter_end=int(chunk.get("chapter_end") or 0),
        critical_chapters_json=json.dumps(chunk.get("critical_chapters") or [], ensure_ascii=False, indent=2),
    )
    raw = _invoke_json_until_valid(prompt, log_label=log_label)
    return _normalize_profile_chunk_json(raw, chunk)


def _generate_profile_chunks(
    character_name: str,
    chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not chunks:
        return []
    total = len(chunks)
    completed = 0
    results: list[dict[str, Any]] = []
    logger.warning(
        "[character_profiles] profile chunk generation started. character=%s total_chunks=%d concurrency=%d",
        character_name,
        total,
        min(MAX_CONCURRENT_PROFILE_CHUNK_TASKS, total),
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_PROFILE_CHUNK_TASKS, total)) as executor:
        future_to_chunk = {
            executor.submit(_generate_single_profile_chunk, character_name, chunk): chunk
            for chunk in chunks
        }
        for future in concurrent.futures.as_completed(future_to_chunk):
            chunk = future_to_chunk[future]
            results.append({**chunk, "chunk_json": future.result()})
            completed += 1
            logger.warning(
                "[character_profiles] profile chunks %s %d/%d completed. character=%s volume=%s chunk=%s",
                _progress_bar(completed, total),
                completed,
                total,
                character_name,
                int(chunk.get("volume_index") or 0),
                int(chunk.get("chunk_index") or 0),
            )
    results.sort(key=lambda item: (int(item.get("volume_index") or 0), int(item.get("chunk_index") or 0)))
    return results


def _group_profile_chunks(character_name: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not chunks:
        return []
    by_volume: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for chunk in chunks:
        volume_title = str(chunk.get("volume_title") or chunk.get("chunk_json", {}).get("volume_title") or "").strip()
        by_volume[(int(chunk.get("volume_index") or 0), volume_title)].append(chunk)

    rows = []
    for (volume_index, volume_title), items in sorted(by_volume.items(), key=lambda item: item[0][0]):
        rows.append(
            {
                "volume_index": volume_index,
                "volume_title": volume_title,
                "chunks": sorted(items, key=lambda item: int(item.get("chunk_index") or 0)),
            }
        )

    total = len(rows)
    completed = 0
    groups: list[dict[str, Any]] = []
    logger.warning(
        "[character_profiles] profile volume grouping started. character=%s total_groups=%d concurrency=%d",
        character_name,
        total,
        min(MAX_CONCURRENT_PROFILE_GROUP_TASKS, total),
    )

    def _single_group(row: dict[str, Any]) -> dict[str, Any]:
        volume_index = int(row["volume_index"])
        volume_title = str(row["volume_title"] or "").strip()
        prompt = CHARACTER_PROFILE_VOLUME_GROUP_PROMPT.format(
            character_name=character_name,
            volume_index=volume_index,
            volume_title=volume_title,
            profile_chunks_json=json.dumps(row["chunks"], ensure_ascii=False, indent=2),
        )
        raw = _invoke_json_until_valid(prompt, log_label=_profile_group_log_label(character_name, volume_index))
        return {
            "volume_index": volume_index,
            "volume_title": volume_title,
            "chunk_refs": row["chunks"],
            "group_json": _normalize_profile_volume_group_json(raw, volume_index, volume_title),
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_PROFILE_GROUP_TASKS, total)) as executor:
        future_to_row = {executor.submit(_single_group, row): row for row in rows}
        for future in concurrent.futures.as_completed(future_to_row):
            row = future_to_row[future]
            groups.append(future.result())
            completed += 1
            logger.warning(
                "[character_profiles] profile groups %s %d/%d completed. character=%s volume=%s",
                _progress_bar(completed, total),
                completed,
                total,
                character_name,
                int(row["volume_index"]),
            )
    groups.sort(key=lambda item: int(item.get("volume_index") or 0))
    return groups


def _generate_single_relation_chunk(character_name: str, target_character_name: str, chunk: dict[str, Any]) -> dict[str, Any]:
    log_label = _relation_chunk_log_label(character_name, target_character_name, chunk)
    logger.info("[character_profiles] start relation chunk. label=%s", log_label)
    prompt = CHARACTER_RELATION_CRITICAL_CHUNK_PROMPT.format(
        character_name=character_name,
        target_character_name=target_character_name,
        volume_index=int(chunk.get("volume_index") or 0),
        volume_title=str(chunk.get("volume_title") or "").strip(),
        chapter_start=int(chunk.get("chapter_start") or 0),
        chapter_end=int(chunk.get("chapter_end") or 0),
        critical_chapters_json=json.dumps(chunk.get("critical_chapters") or [], ensure_ascii=False, indent=2),
    )
    raw = _invoke_json_until_valid(prompt, log_label=log_label)
    return _normalize_relation_chunk_json(raw, chunk)


def _generate_relation_chunks(
    character_name: str,
    critical_packets_by_target: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    targets_with_chunks: list[tuple[str, dict[str, Any]]] = []
    for target_character_name, packets in sorted(critical_packets_by_target.items()):
        for chunk in _chunk_packets_by_volume(packets, max_chapters=MAX_CRITICAL_CHAPTERS_PER_CHUNK):
            chunk["target_character_name"] = target_character_name
            targets_with_chunks.append((target_character_name, chunk))
    if not targets_with_chunks:
        return []
    total = len(targets_with_chunks)
    completed = 0
    results: list[dict[str, Any]] = []
    logger.warning(
        "[character_profiles] relation chunk generation started. character=%s total_chunks=%d concurrency=%d",
        character_name,
        total,
        min(MAX_CONCURRENT_RELATION_CHUNK_TASKS, total),
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_RELATION_CHUNK_TASKS, total)) as executor:
        future_to_chunk = {
            executor.submit(_generate_single_relation_chunk, character_name, target_name, chunk): (target_name, chunk)
            for target_name, chunk in targets_with_chunks
        }
        for future in concurrent.futures.as_completed(future_to_chunk):
            target_name, chunk = future_to_chunk[future]
            result = future.result()
            results.append({**chunk, "target_character_name": target_name, "chunk_json": result})
            completed += 1
            logger.warning(
                "[character_profiles] relation chunks %s %d/%d completed. character=%s target=%s volume=%s chunk=%s",
                _progress_bar(completed, total),
                completed,
                total,
                character_name,
                target_name,
                int(chunk.get("volume_index") or 0),
                int(chunk.get("chunk_index") or 0),
            )
    results.sort(key=lambda item: (str(item.get("target_character_name") or ""), int(item.get("volume_index") or 0), int(item.get("chunk_index") or 0)))
    return results


def _group_relation_chunks(character_name: str, relation_chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not relation_chunks:
        return []
    grouped: dict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for chunk in relation_chunks:
        volume_title = str(chunk.get("volume_title") or chunk.get("chunk_json", {}).get("volume_title") or "").strip()
        grouped[(str(chunk.get("target_character_name") or ""), int(chunk.get("volume_index") or 0), volume_title)].append(chunk)

    rows = []
    for (target_character_name, volume_index, volume_title), items in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
        rows.append(
            {
                "target_character_name": target_character_name,
                "volume_index": volume_index,
                "volume_title": volume_title,
                "chunks": sorted(items, key=lambda item: int(item.get("chunk_index") or 0)),
            }
        )

    total = len(rows)
    completed = 0
    groups: list[dict[str, Any]] = []
    logger.warning(
        "[character_profiles] relation volume grouping started. character=%s total_groups=%d concurrency=%d",
        character_name,
        total,
        min(MAX_CONCURRENT_RELATION_GROUP_TASKS, total),
    )

    def _single_group(row: dict[str, Any]) -> dict[str, Any]:
        target_character_name = row["target_character_name"]
        volume_index = int(row["volume_index"])
        volume_title = str(row["volume_title"] or "").strip()
        prompt = CHARACTER_RELATION_VOLUME_GROUP_PROMPT.format(
            character_name=character_name,
            target_character_name=target_character_name,
            volume_index=volume_index,
            volume_title=volume_title,
            relation_chunks_json=json.dumps(row["chunks"], ensure_ascii=False, indent=2),
        )
        raw = _invoke_json_until_valid(prompt, log_label=_relation_group_log_label(character_name, target_character_name, volume_index))
        return {
            "target_character_name": target_character_name,
            "volume_index": volume_index,
            "volume_title": volume_title,
            "chunk_refs": row["chunks"],
            "group_json": _normalize_relation_volume_group_json(raw, volume_index, volume_title),
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_RELATION_GROUP_TASKS, total)) as executor:
        future_to_row = {executor.submit(_single_group, row): row for row in rows}
        for future in concurrent.futures.as_completed(future_to_row):
            row = future_to_row[future]
            groups.append(future.result())
            completed += 1
            logger.warning(
                "[character_profiles] relation groups %s %d/%d completed. character=%s target=%s volume=%s",
                _progress_bar(completed, total),
                completed,
                total,
                character_name,
                row["target_character_name"],
                int(row["volume_index"]),
            )
    groups.sort(key=lambda item: (str(item.get("target_character_name") or ""), int(item.get("volume_index") or 0)))
    return groups


def _merge_relation_histories(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not events:
        return []
    ordered = sorted(
        events,
        key=lambda item: (
            item["chapter_start"],
            item["chapter_end"],
            item["relation_type"],
            item["polarity"],
            item["strength"],
            item["summary"],
        ),
    )
    merged: list[dict[str, Any]] = []
    for item in ordered:
        if not merged:
            merged.append(dict(item))
            continue
        prev = merged[-1]
        should_merge = (
            prev.get("relation_type") == item.get("relation_type")
            and prev.get("polarity") == item.get("polarity")
            and prev.get("strength") == item.get("strength")
            and int(item["chapter_start"]) <= int(prev["chapter_end"]) + 1
        )
        if not should_merge:
            merged.append(dict(item))
            continue
        prev["chapter_end"] = max(int(prev["chapter_end"]), int(item["chapter_end"]))
        prev["evidence_chapters"] = _normalize_int_list(list(prev.get("evidence_chapters", [])) + list(item.get("evidence_chapters", [])))
        summaries = _dedupe_str_list([prev.get("summary", ""), item.get("summary", "")])
        prev["summary"] = "；".join(summaries)
    return merged


def _relation_target_name_key(target_character_name: Any) -> str:
    text = str(target_character_name or "").strip()
    return _normalize_text_key(text) or text


def _pick_preferred_relation_target_name(rows: list[dict[str, Any]]) -> str:
    for row in rows:
        try:
            target_character_id = int(row.get("target_character_id") or 0)
        except (TypeError, ValueError):
            target_character_id = 0
        target_character_name = str(row.get("target_character_name") or "").strip()
        if target_character_id > 0 and target_character_name:
            return target_character_name
    for row in rows:
        target_character_name = str(row.get("target_character_name") or "").strip()
        if target_character_name:
            return target_character_name
    return ""


def _pick_preferred_relation_target_id(rows: list[dict[str, Any]]) -> int | None:
    for row in rows:
        try:
            target_character_id = int(row.get("target_character_id") or 0)
        except (TypeError, ValueError):
            target_character_id = 0
        if target_character_id > 0:
            return target_character_id
    return None


def _pick_first_non_empty_relation_value(rows: list[dict[str, Any]], key: str) -> str:
    for row in rows:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _dedupe_relation_rows_for_storage(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = _relation_target_name_key(row.get("target_character_name"))
        if not key:
            continue
        grouped[key].append(row)

    deduped_rows: list[dict[str, Any]] = []
    for _, target_rows in sorted(grouped.items(), key=lambda item: _pick_preferred_relation_target_name(item[1])):
        history_inputs: list[dict[str, Any]] = []
        summary_parts: list[str] = []
        structural_relation: list[Any] = []
        action_relation: list[Any] = []
        emotional_relation: list[Any] = []
        drivers: list[Any] = []
        for row in target_rows:
            history_inputs.extend(_normalize_relation_history(row.get("history_json") if isinstance(row.get("history_json"), list) else []))
            summary_parts.append(str(row.get("summary") or "").strip())
            structural_relation.extend(row.get("structural_relation") if isinstance(row.get("structural_relation"), list) else [])
            action_relation.extend(row.get("action_relation") if isinstance(row.get("action_relation"), list) else [])
            emotional_relation.extend(row.get("emotional_relation") if isinstance(row.get("emotional_relation"), list) else [])
            drivers.extend(row.get("drivers") if isinstance(row.get("drivers"), list) else [])
        history_json = _merge_relation_histories(history_inputs)
        deduped_rows.append(
            {
                "target_character_id": _pick_preferred_relation_target_id(target_rows),
                "target_character_name": _pick_preferred_relation_target_name(target_rows),
                "summary": "；".join(_dedupe_str_list(summary_parts)),
                "structural_relation": _dedupe_str_list(structural_relation),
                "action_relation": _dedupe_str_list(action_relation),
                "emotional_relation": _dedupe_str_list(emotional_relation),
                "directionality": _pick_first_non_empty_relation_value(target_rows, "directionality"),
                "stability": _pick_first_non_empty_relation_value(target_rows, "stability"),
                "current_status": _pick_first_non_empty_relation_value(target_rows, "current_status"),
                "drivers": _dedupe_str_list(drivers),
                "history_json": history_json,
                "first_chapter_index": min((item["chapter_start"] for item in history_json), default=0),
                "last_chapter_index": max((item["chapter_end"] for item in history_json), default=0),
            }
        )
    deduped_rows.sort(key=lambda item: (int(item.get("first_chapter_index") or 0), str(item.get("target_character_name") or "")))
    return deduped_rows


def _group_relation_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in events:
        key = _relation_target_name_key(item.get("target_character_name"))
        if not key:
            continue
        grouped[key].append(item)

    grouped_rows: list[dict[str, Any]] = []
    for _, rows in sorted(grouped.items(), key=lambda item: _pick_preferred_relation_target_name(item[1])):
        history = _merge_relation_histories(rows)
        grouped_rows.append(
            {
                "target_character_id": _pick_preferred_relation_target_id(rows),
                "target_character_name": _pick_preferred_relation_target_name(rows),
                "history_json": history,
                "first_chapter_index": min(item["chapter_start"] for item in history) if history else 0,
                "last_chapter_index": max(item["chapter_end"] for item in history) if history else 0,
            }
        )
    return grouped_rows


def _build_final_profile(
    character_row: dict[str, Any],
    profile_slices: list[dict[str, Any]],
    profile_volume_groups: list[dict[str, Any]],
) -> dict[str, Any]:
    chapter_indexes = [chapter_index for chapter_index, _ in character_row["records"]]
    logger.warning(
        "[character_profiles] final profile module generation started. label=%s",
        _final_profile_log_label(character_row["name"], profile_slices),
    )
    first_chapter_index = min(chapter_indexes) if chapter_indexes else 0
    last_chapter_index = max(chapter_indexes) if chapter_indexes else 0
    all_groups_json = json.dumps(profile_volume_groups, ensure_ascii=False, indent=2)
    recent_groups = profile_volume_groups[-2:] if len(profile_volume_groups) > 2 else profile_volume_groups
    recent_groups_json = json.dumps(recent_groups, ensure_ascii=False, indent=2)

    module_specs = {
        "identity_role": CHARACTER_PROFILE_IDENTITY_ROLE_PROMPT.format(
            character_name=character_row["name"],
            aliases_json=json.dumps(character_row["aliases"], ensure_ascii=False),
            first_chapter_index=first_chapter_index,
            last_chapter_index=last_chapter_index,
            record_count=len(character_row["records"]),
            profile_slices_json=json.dumps(profile_slices, ensure_ascii=False, indent=2),
            profile_volume_groups_json=all_groups_json,
        ),
        "personality": CHARACTER_PROFILE_PERSONALITY_PROMPT.format(
            character_name=character_row["name"],
            profile_volume_groups_json=all_groups_json,
        ),
        "appearance": CHARACTER_PROFILE_APPEARANCE_PROMPT.format(
            character_name=character_row["name"],
            profile_volume_groups_json=all_groups_json,
        ),
        "mechanism": CHARACTER_PROFILE_MECHANISM_PROMPT.format(
            character_name=character_row["name"],
            profile_volume_groups_json=all_groups_json,
        ),
        "volume_arc": CHARACTER_PROFILE_VOLUME_ARC_PROMPT.format(
            character_name=character_row["name"],
            profile_slices_json=json.dumps(profile_slices, ensure_ascii=False, indent=2),
            profile_volume_groups_json=all_groups_json,
        ),
        "current_state": CHARACTER_PROFILE_CURRENT_STATE_PROMPT.format(
            character_name=character_row["name"],
            recent_profile_volume_groups_json=recent_groups_json,
            all_profile_volume_groups_json=all_groups_json,
        ),
    }

    def _run_module(name: str, prompt: str) -> tuple[str, dict[str, Any]]:
        logger.info("[character_profiles] start final profile module. character=%s module=%s", character_row["name"], name)
        return name, _invoke_json_until_valid(prompt, log_label=f"{_final_profile_log_label(character_row['name'], profile_slices)}|module={name}")

    module_results: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_FINAL_PROFILE_MODULE_TASKS) as executor:
        future_to_name = {
            executor.submit(_run_module, name, prompt): name
            for name, prompt in module_specs.items()
        }
        for future in concurrent.futures.as_completed(future_to_name):
            name = future_to_name[future]
            module_name, payload = future.result()
            module_results[module_name] = payload
            logger.warning(
                "[character_profiles] final profile module completed. character=%s module=%s",
                character_row["name"],
                name,
            )

    raw = {
        **module_results.get("identity_role", {}),
        **module_results.get("personality", {}),
        **module_results.get("appearance", {}),
        **module_results.get("mechanism", {}),
        **module_results.get("volume_arc", {}),
        **module_results.get("current_state", {}),
    }
    return _normalize_profile_json(
        raw,
        character_row,
        first_chapter_index,
        last_chapter_index,
        character_row["aliases"],
    )


def _generate_single_roleplay_style_batch(character_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    window = payload["window"]
    prompt = ROLEPLAY_STYLE_SAMPLE_BATCH_PROMPT.format(
        character_name=character_name,
        chapter_start=window.chapter_start,
        chapter_end=window.chapter_end,
        chapters_json=json.dumps(payload["chapters_payload"], ensure_ascii=False, indent=2),
    )
    parsed = _invoke_json_until_valid(
        prompt,
        log_label=f"{character_name}|roleplay_style|{window.chapter_start}-{window.chapter_end}",
    )
    return _normalize_roleplay_style_batch(parsed)


def _generate_roleplay_style_batches(character_name: str, payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not payloads:
        return []
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_ROLEPLAY_BATCH_TASKS, len(payloads))) as executor:
        future_to_payload = {
            executor.submit(_generate_single_roleplay_style_batch, character_name, payload): payload
            for payload in payloads
        }
        for future in concurrent.futures.as_completed(future_to_payload):
            results.append(future.result())
    return results


def _summarize_roleplay_style(character_name: str, batch_results: list[dict[str, Any]]) -> dict[str, Any]:
    aggregated = _aggregate_roleplay_style_batches(batch_results)
    prompt = ROLEPLAY_STYLE_SAMPLE_SUMMARY_PROMPT.format(
        character_name=character_name,
        style_batches_json=json.dumps(
            {
                "batch_results": batch_results,
                "aggregated_candidates": aggregated,
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    parsed = _invoke_json_until_valid(
        prompt,
        log_label=f"{character_name}|roleplay_style_summary",
    )
    return {
        "style_summary": str(parsed.get("style_summary") or "").strip(),
        "speech_style": _dedupe_str_list(parsed.get("speech_style") if isinstance(parsed.get("speech_style"), list) else []),
        "style_samples": [
            {
                "scene": str(item.get("scene") or "").strip(),
                "quote": str(item.get("quote") or "").strip(),
            }
            for item in parsed.get("style_samples", [])
            if isinstance(item, dict)
            and str(item.get("scene") or "").strip()
            and str(item.get("quote") or "").strip()
        ],
    }


def _generate_single_roleplay_relation_batch(
    character_name: str,
    payload: dict[str, Any],
    alias_map: dict[str, str | None],
    id_map: dict[str, int],
) -> dict[str, Any]:
    window = payload["window"]
    prompt = CHARACTER_ROLEPLAY_RELATION_BATCH_PROMPT.format(
        character_name=character_name,
        chapter_start=window.chapter_start,
        chapter_end=window.chapter_end,
        chapters_json=json.dumps(payload["chapters_payload"], ensure_ascii=False, indent=2),
    )
    parsed = _invoke_json_until_valid(
        prompt,
        log_label=f"{character_name}|roleplay_relation|{window.chapter_start}-{window.chapter_end}",
    )
    return _normalize_roleplay_relation_batch(parsed, window, character_name, alias_map, id_map)


def _generate_roleplay_relation_batches(
    character_name: str,
    payloads: list[dict[str, Any]],
    alias_map: dict[str, str | None],
    id_map: dict[str, int],
) -> list[dict[str, Any]]:
    if not payloads:
        return []
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_ROLEPLAY_BATCH_TASKS, len(payloads))) as executor:
        future_to_payload = {
            executor.submit(_generate_single_roleplay_relation_batch, character_name, payload, alias_map, id_map): payload
            for payload in payloads
        }
        for future in concurrent.futures.as_completed(future_to_payload):
            results.append(future.result())
    return results


def _summarize_single_roleplay_relation(character_name: str, row: dict[str, Any]) -> dict[str, Any]:
    target_character_name = str(row.get("target_character_name") or "").strip()
    prompt = CHARACTER_ROLEPLAY_RELATION_SUMMARY_PROMPT.format(
        character_name=character_name,
        target_character_name=target_character_name,
        relation_candidates_json=json.dumps(row.get("candidates") or [], ensure_ascii=False, indent=2),
    )
    parsed = _invoke_json_until_valid(
        prompt,
        log_label=f"{character_name}|roleplay_relation_summary|{target_character_name}",
    )
    return _normalize_roleplay_relation_summary(parsed, target_character_name)


def _summarize_roleplay_relations(character_name: str, batch_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped = _aggregate_roleplay_relation_candidates(batch_results)
    if not grouped:
        return []
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_ROLEPLAY_RELATION_SUMMARIES, len(grouped))) as executor:
        future_to_row = {
            executor.submit(_summarize_single_roleplay_relation, character_name, row): row
            for row in grouped
        }
        for future in concurrent.futures.as_completed(future_to_row):
            results.append(future.result())
    results.sort(key=lambda item: (str(item.get("target_character") or ""), str(item.get("primary_relation_type") or "")))
    return results


def _summarize_single_relation(
    character_name: str,
    row: dict[str, Any],
    relation_volume_groups: list[dict[str, Any]],
) -> dict[str, Any] | None:
    history_json = _normalize_relation_history(row.get("history_json") or [])
    if not history_json:
        return None
    log_label = _relation_log_label(character_name, row["target_character_name"], history_json)
    logger.info("[character_profiles] start relation module generation. label=%s", log_label)
    relation_groups_json = json.dumps(relation_volume_groups, ensure_ascii=False, indent=2)
    history_json_text = json.dumps(history_json, ensure_ascii=False, indent=2)
    module_specs = {
        "overview": CHARACTER_RELATION_OVERVIEW_PROMPT.format(
            character_name=character_name,
            target_character_name=row["target_character_name"],
            history_json=history_json_text,
            relation_volume_groups_json=relation_groups_json,
        ),
        "structure": CHARACTER_RELATION_STRUCTURE_PROMPT.format(
            character_name=character_name,
            target_character_name=row["target_character_name"],
            relation_volume_groups_json=relation_groups_json,
        ),
        "dynamics": CHARACTER_RELATION_DYNAMICS_PROMPT.format(
            character_name=character_name,
            target_character_name=row["target_character_name"],
            history_json=history_json_text,
            relation_volume_groups_json=relation_groups_json,
        ),
    }

    def _run_module(name: str, prompt: str) -> tuple[str, dict[str, Any]]:
        logger.info("[character_profiles] start final relation module. label=%s module=%s", log_label, name)
        return name, _invoke_json_until_valid(prompt, log_label=f"{log_label}|module={name}")

    module_results: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_FINAL_RELATION_MODULE_TASKS) as executor:
        future_to_name = {
            executor.submit(_run_module, name, prompt): name
            for name, prompt in module_specs.items()
        }
        for future in concurrent.futures.as_completed(future_to_name):
            name = future_to_name[future]
            module_name, payload = future.result()
            module_results[module_name] = payload
            logger.warning(
                "[character_profiles] final relation module completed. label=%s module=%s",
                log_label,
                name,
            )

    raw = {
        **module_results.get("overview", {}),
        **module_results.get("structure", {}),
        **module_results.get("dynamics", {}),
    }

    def _run_history_segment(history_segment: dict[str, Any]) -> dict[str, Any]:
        segment_log_label = _history_segment_log_label(character_name, row["target_character_name"], history_segment)
        relevant_groups = _select_relevant_relation_groups_for_segment(history_segment, relation_volume_groups)
        prompt = CHARACTER_RELATION_HISTORY_SEGMENT_PROMPT.format(
            character_name=character_name,
            target_character_name=row["target_character_name"],
            history_segment_json=json.dumps(history_segment, ensure_ascii=False, indent=2),
            relation_volume_groups_json=json.dumps(relevant_groups, ensure_ascii=False, indent=2),
        )
        logger.info("[character_profiles] start final relation history segment. label=%s", segment_log_label)
        parsed = _invoke_json_until_valid(prompt, log_label=segment_log_label)
        segment_raw = parsed.get("history_segment") if isinstance(parsed.get("history_segment"), dict) else history_segment
        normalized = _normalize_relation_history([segment_raw])
        return normalized[0] if normalized else history_segment

    normalized_history: list[dict[str, Any]] = []
    if history_json:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(history_json), MAX_CONCURRENT_FINAL_RELATION_MODULE_TASKS)) as executor:
            future_to_segment = {
                executor.submit(_run_history_segment, history_segment): history_segment
                for history_segment in history_json
            }
            for future in concurrent.futures.as_completed(future_to_segment):
                normalized_history.append(future.result())
        normalized_history.sort(key=lambda item: (int(item.get("chapter_start") or 0), int(item.get("chapter_end") or 0)))
    else:
        normalized_history = history_json

    return {
        "target_character_id": row.get("target_character_id"),
        "target_character_name": row["target_character_name"],
        "summary": str(raw.get("summary") or "").strip(),
        "structural_relation": _dedupe_str_list(
            raw.get("structural_relation") if isinstance(raw.get("structural_relation"), list) else []
        ),
        "action_relation": _dedupe_str_list(
            raw.get("action_relation") if isinstance(raw.get("action_relation"), list) else []
        ),
        "emotional_relation": _dedupe_str_list(
            raw.get("emotional_relation") if isinstance(raw.get("emotional_relation"), list) else []
        ),
        "directionality": str(raw.get("directionality") or "").strip(),
        "stability": str(raw.get("stability") or "").strip(),
        "current_status": str(raw.get("current_status") or "").strip(),
        "drivers": _dedupe_str_list(raw.get("drivers") if isinstance(raw.get("drivers"), list) else []),
        "history_json": normalized_history or history_json,
        "first_chapter_index": row["first_chapter_index"],
        "last_chapter_index": row["last_chapter_index"],
    }


def _summarize_relations(
    character_name: str,
    grouped_relations: list[dict[str, Any]],
    relation_volume_groups_by_target: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    if not grouped_relations:
        return []
    total = len(grouped_relations)
    completed = 0
    summarized: list[dict[str, Any]] = []
    logger.warning(
        "[character_profiles] relation summarization started. character=%s total_relations=%d concurrency=%d",
        character_name,
        total,
        min(MAX_CONCURRENT_RELATION_TASKS, total),
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_RELATION_TASKS, total)) as executor:
        future_to_row = {
            executor.submit(
                _summarize_single_relation,
                character_name,
                row,
                relation_volume_groups_by_target.get(str(row.get("target_character_name") or "").strip(), []),
            ): row
            for row in grouped_relations
        }
        for future in concurrent.futures.as_completed(future_to_row):
            row = future_to_row[future]
            result = future.result()
            if result is not None:
                summarized.append(result)
            completed += 1
            logger.warning(
                "[character_profiles] relations %s %d/%d completed. character=%s target=%s",
                _progress_bar(completed, total),
                completed,
                total,
                character_name,
                row["target_character_name"],
            )
    summarized.sort(key=lambda item: (int(item.get("first_chapter_index") or 0), str(item.get("target_character_name") or "")))
    return summarized


def _create_job(cursor: Any, book_id: int, character_id: int, character_name: str, status: str) -> int:
    cursor.execute(
        """
        INSERT INTO character_profile_jobs
        (book_id, character_id, character_name, status, started_at, finished_at)
        VALUES (%s, %s, %s, %s, CASE WHEN %s IN ('running', 'completed', 'error', 'cached') THEN CURRENT_TIMESTAMP ELSE NULL END, CASE WHEN %s IN ('completed', 'error', 'cached') THEN CURRENT_TIMESTAMP ELSE NULL END)
        """,
        (int(book_id), int(character_id), character_name, status, status, status),
    )
    return int(cursor.lastrowid or 0)


def _update_job(cursor: Any, job_id: int, status: str, error_message: str = "") -> None:
    cursor.execute(
        """
        UPDATE character_profile_jobs
        SET status = %s,
            error_message = %s,
            started_at = CASE WHEN started_at IS NULL AND %s = 'running' THEN CURRENT_TIMESTAMP ELSE started_at END,
            finished_at = CASE WHEN %s IN ('completed', 'error', 'cached') THEN CURRENT_TIMESTAMP ELSE finished_at END
        WHERE id = %s
        """,
        (status, error_message or None, status, status, int(job_id)),
    )


def _save_profile(cursor: Any, book_id: int, character_row: dict[str, Any], version_hash: str, profile_json: dict[str, Any], source_chapters: list[int]) -> None:
    cursor.execute(
        """
        INSERT INTO character_profiles
        (
            book_id, character_id, character_name, aliases_json, first_chapter_index,
            last_chapter_index, record_count, profile_json, source_chapters_json, version_hash
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            character_name = VALUES(character_name),
            aliases_json = VALUES(aliases_json),
            first_chapter_index = VALUES(first_chapter_index),
            last_chapter_index = VALUES(last_chapter_index),
            record_count = VALUES(record_count),
            profile_json = VALUES(profile_json),
            source_chapters_json = VALUES(source_chapters_json),
            version_hash = VALUES(version_hash),
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            int(book_id),
            int(character_row["id"]),
            character_row["name"],
            json.dumps(character_row["aliases"], ensure_ascii=False),
            min(source_chapters) if source_chapters else 0,
            max(source_chapters) if source_chapters else 0,
            len(character_row["records"]),
            json.dumps(profile_json, ensure_ascii=False),
            json.dumps(source_chapters, ensure_ascii=False),
            version_hash,
        ),
    )


def _save_profile_chunks(cursor: Any, book_id: int, character_row: dict[str, Any], version_hash: str, chunks: list[dict[str, Any]]) -> None:
    cursor.execute("DELETE FROM character_profile_chunks WHERE book_id = %s AND character_id = %s", (int(book_id), int(character_row["id"])))
    for chunk in chunks:
        cursor.execute(
            """
            INSERT INTO character_profile_chunks
            (book_id, character_id, character_name, volume_index, chunk_index, chapter_start, chapter_end, source_chapters_json, chunk_json, version_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                int(book_id),
                int(character_row["id"]),
                character_row["name"],
                int(chunk.get("volume_index") or 0),
                int(chunk.get("chunk_index") or 0),
                int(chunk.get("chapter_start") or 0),
                int(chunk.get("chapter_end") or 0),
                json.dumps(chunk.get("source_chapters") or [], ensure_ascii=False),
                json.dumps(chunk.get("chunk_json") or {}, ensure_ascii=False),
                version_hash,
            ),
        )
        chunk["id"] = int(cursor.lastrowid or 0)


def _save_profile_volume_groups(cursor: Any, book_id: int, character_row: dict[str, Any], version_hash: str, groups: list[dict[str, Any]]) -> None:
    cursor.execute("DELETE FROM character_profile_volume_groups WHERE book_id = %s AND character_id = %s", (int(book_id), int(character_row["id"])))
    for group in groups:
        chunk_ids = [int(item.get("id") or 0) for item in group.get("chunk_refs") or [] if int(item.get("id") or 0) > 0]
        cursor.execute(
            """
            INSERT INTO character_profile_volume_groups
            (book_id, character_id, character_name, volume_index, chunk_ids_json, group_json, version_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                int(book_id),
                int(character_row["id"]),
                character_row["name"],
                int(group.get("volume_index") or 0),
                json.dumps(chunk_ids, ensure_ascii=False),
                json.dumps(group.get("group_json") or {}, ensure_ascii=False),
                version_hash,
            ),
        )
        group["id"] = int(cursor.lastrowid or 0)


def _save_relations(cursor: Any, book_id: int, character_row: dict[str, Any], version_hash: str, relations: list[dict[str, Any]]) -> None:
    cursor.execute(
        """
        DELETE FROM character_relations
        WHERE book_id = %s AND source_character_id = %s
        """,
        (int(book_id), int(character_row["id"])),
    )
    for row in _dedupe_relation_rows_for_storage(relations):
        cursor.execute(
            """
            INSERT INTO character_relations
            (
                book_id, source_character_id, source_character_name, target_character_id,
                target_character_name, summary, relation_model_json, history_json, first_chapter_index,
                last_chapter_index, version_hash
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                int(book_id),
                int(character_row["id"]),
                character_row["name"],
                row.get("target_character_id"),
                row["target_character_name"],
                row["summary"],
                json.dumps(
                    {
                        "structural_relation": row.get("structural_relation", []),
                        "action_relation": row.get("action_relation", []),
                        "emotional_relation": row.get("emotional_relation", []),
                        "directionality": row.get("directionality", ""),
                        "stability": row.get("stability", ""),
                        "current_status": row.get("current_status", ""),
                        "drivers": row.get("drivers", []),
                    },
                    ensure_ascii=False,
                ),
                json.dumps(row["history_json"], ensure_ascii=False),
                int(row.get("first_chapter_index") or 0),
                int(row.get("last_chapter_index") or 0),
                version_hash,
            ),
        )


def _save_relation_chunks(cursor: Any, book_id: int, character_row: dict[str, Any], version_hash: str, chunks: list[dict[str, Any]]) -> None:
    cursor.execute("DELETE FROM character_relation_chunks WHERE book_id = %s AND source_character_id = %s", (int(book_id), int(character_row["id"])))
    for chunk in chunks:
        cursor.execute(
            """
            INSERT INTO character_relation_chunks
            (book_id, source_character_id, source_character_name, target_character_id, target_character_name, volume_index, chunk_index, chapter_start, chapter_end, source_chapters_json, chunk_json, version_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                int(book_id),
                int(character_row["id"]),
                character_row["name"],
                chunk.get("target_character_id"),
                str(chunk.get("target_character_name") or "").strip(),
                int(chunk.get("volume_index") or 0),
                int(chunk.get("chunk_index") or 0),
                int(chunk.get("chapter_start") or 0),
                int(chunk.get("chapter_end") or 0),
                json.dumps(chunk.get("source_chapters") or [], ensure_ascii=False),
                json.dumps(chunk.get("chunk_json") or {}, ensure_ascii=False),
                version_hash,
            ),
        )
        chunk["id"] = int(cursor.lastrowid or 0)


def _save_relation_volume_groups(cursor: Any, book_id: int, character_row: dict[str, Any], version_hash: str, groups: list[dict[str, Any]]) -> None:
    cursor.execute("DELETE FROM character_relation_volume_groups WHERE book_id = %s AND source_character_id = %s", (int(book_id), int(character_row["id"])))
    for group in groups:
        chunk_ids = [int(item.get("id") or 0) for item in group.get("chunk_refs") or [] if int(item.get("id") or 0) > 0]
        cursor.execute(
            """
            INSERT INTO character_relation_volume_groups
            (book_id, source_character_id, source_character_name, target_character_id, target_character_name, volume_index, chunk_ids_json, group_json, version_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                int(book_id),
                int(character_row["id"]),
                character_row["name"],
                group.get("target_character_id"),
                str(group.get("target_character_name") or "").strip(),
                int(group.get("volume_index") or 0),
                json.dumps(chunk_ids, ensure_ascii=False),
                json.dumps(group.get("group_json") or {}, ensure_ascii=False),
                version_hash,
            ),
        )
        group["id"] = int(cursor.lastrowid or 0)


def generate_character_archive(book_id: int, character_id: int) -> dict[str, Any]:
    with _connect() as conn:
        with conn.cursor() as cursor:
            context = _load_character_context(cursor, int(book_id), int(character_id))
            character_row = context["character_row"]
            book_character_rows = _load_book_characters(cursor, int(book_id))
            cached_profile = _load_cached_profile(cursor, book_id, character_id, context["version_hash"])
            cached_relations = _load_cached_relations(cursor, book_id, character_id, context["version_hash"])
            if cached_profile is not None:
                job_id = _create_job(cursor, book_id, character_id, character_row["name"], "cached")
                result = {
                    "cached": True,
                    "job_id": job_id,
                    "character": character_row,
                    "profile": cached_profile["profile_json"],
                    "relations": cached_relations,
                    "version_hash": context["version_hash"],
                }
                try:
                    from relationGraph.sync import sync_character_relation_subgraph

                    sync_character_relation_subgraph(
                        book_id=int(book_id),
                        character_row=context["character_row"],
                        profile_json=cached_profile["profile_json"],
                        relations=cached_relations,
                        version_hash=context["version_hash"],
                        book_character_rows=book_character_rows,
                    )
                except Exception:
                    logger.exception(
                        "[character_profiles] relation graph sync failed on cached archive. character=%s book_id=%d",
                        context["character_row"]["name"],
                        int(book_id),
                    )
                return result
            job_id = _create_job(cursor, book_id, character_id, character_row["name"], "pending")
            _update_job(cursor, job_id, "running")

    try:
        alias_map, id_map = _build_alias_maps(book_character_rows)
        alias_lookup = _alias_lookup_by_name(book_character_rows)
        logger.warning(
            "[character_profiles] archive generation started. character=%s book_id=%d windows=%d chapters=%d",
            context["character_row"]["name"],
            int(book_id),
            len(context["windows"]),
            len(context["entries"]),
        )
        profile_slices, relation_events = _generate_window_outputs(
            context["character_row"]["name"],
            context["windows"],
            alias_map,
            id_map,
        )
        grouped_relations = _group_relation_events(relation_events)
        entry_map = _entry_lookup(context["entries"])
        profile_critical_selected = _select_profile_critical_chapters(
            context["entries"],
            context["character_row"]["aliases"],
            context["chapter_rows"],
        )
        relation_critical_selected = _select_relation_critical_chapters(
            grouped_relations,
            entry_map,
            context["chapter_rows"],
            alias_lookup,
        )
        critical_chapter_indexes = set(profile_critical_selected.keys())
        for packets in relation_critical_selected.values():
            critical_chapter_indexes.update(int(item.get("chapter_index") or 0) for item in packets)
        with _connect() as conn:
            with conn.cursor() as cursor:
                chapter_contents = _load_chapter_contents(cursor, int(book_id), list(critical_chapter_indexes))
        profile_critical_packets = _build_profile_critical_packets(
            context["chapter_rows"],
            chapter_contents,
            profile_critical_selected,
            context["character_row"]["aliases"],
            context["volume_rows"],
        )
        relation_critical_packets = _build_relation_critical_packets(
            relation_critical_selected,
            chapter_contents,
            context["character_row"]["aliases"],
            alias_lookup,
            context["volume_rows"],
        )
        logger.warning(
            "[character_profiles] critical chapter selection completed. character=%s profile_chapters=%d relation_targets=%d",
            context["character_row"]["name"],
            len(profile_critical_packets),
            len(relation_critical_packets),
        )
        with _connect() as conn:
            with conn.cursor() as cursor:
                cached_profile_chunks = _load_cached_profile_chunks(cursor, int(book_id), int(context["character_row"]["id"]), context["version_hash"])
                cached_profile_groups = _load_cached_profile_volume_groups(cursor, int(book_id), int(context["character_row"]["id"]), context["version_hash"])
                cached_relation_chunks = _load_cached_relation_chunks(cursor, int(book_id), int(context["character_row"]["id"]), context["version_hash"])
                cached_relation_groups = _load_cached_relation_volume_groups(cursor, int(book_id), int(context["character_row"]["id"]), context["version_hash"])

        profile_chunks = cached_profile_chunks
        if not profile_chunks and profile_critical_packets:
            profile_chunk_inputs = _chunk_packets_by_volume(profile_critical_packets, max_chapters=MAX_CRITICAL_CHAPTERS_PER_CHUNK)
            profile_chunks = _generate_profile_chunks(context["character_row"]["name"], profile_chunk_inputs)
            with _connect() as conn:
                with conn.cursor() as cursor:
                    _save_profile_chunks(cursor, book_id, context["character_row"], context["version_hash"], profile_chunks)

        profile_volume_groups = cached_profile_groups
        if not profile_volume_groups and profile_chunks:
            profile_volume_groups = _group_profile_chunks(context["character_row"]["name"], profile_chunks)
            with _connect() as conn:
                with conn.cursor() as cursor:
                    _save_profile_volume_groups(cursor, book_id, context["character_row"], context["version_hash"], profile_volume_groups)

        relation_chunks = cached_relation_chunks
        if not relation_chunks and relation_critical_packets:
            relation_chunks = _generate_relation_chunks(context["character_row"]["name"], relation_critical_packets)
            with _connect() as conn:
                with conn.cursor() as cursor:
                    _save_relation_chunks(cursor, book_id, context["character_row"], context["version_hash"], relation_chunks)

        relation_volume_groups = cached_relation_groups
        if not relation_volume_groups and relation_chunks:
            relation_volume_groups = _group_relation_chunks(context["character_row"]["name"], relation_chunks)
            with _connect() as conn:
                with conn.cursor() as cursor:
                    _save_relation_volume_groups(cursor, book_id, context["character_row"], context["version_hash"], relation_volume_groups)

        profile_json = _build_final_profile(
            context["character_row"],
            profile_slices,
            [item.get("group_json", {}) for item in profile_volume_groups],
        )
        relation_group_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in relation_volume_groups:
            target_character_name = str(item.get("target_character_name") or "").strip()
            if not target_character_name:
                continue
            relation_group_map[target_character_name].append(item.get("group_json", {}))
        relation_rows = _summarize_relations(
            context["character_row"]["name"],
            grouped_relations,
            relation_group_map,
        )

        with _connect() as conn:
            with conn.cursor() as cursor:
                source_chapters = [entry.chapter_index for entry in context["entries"]]
                _save_profile(cursor, book_id, context["character_row"], context["version_hash"], profile_json, source_chapters)
                _save_relations(cursor, book_id, context["character_row"], context["version_hash"], relation_rows)
                _update_job(cursor, job_id, "completed")
                logger.warning(
                    "[character_profiles] archive generation completed. character=%s book_id=%d slices=%d relations=%d",
                    context["character_row"]["name"],
                    int(book_id),
                    len(profile_slices),
                    len(relation_rows),
                )

        try:
            from relationGraph.sync import sync_character_relation_subgraph

            sync_character_relation_subgraph(
                book_id=int(book_id),
                character_row=context["character_row"],
                profile_json=profile_json,
                relations=relation_rows,
                version_hash=context["version_hash"],
                book_character_rows=book_character_rows,
            )
        except Exception:
            logger.exception(
                "[character_profiles] relation graph sync failed after archive generation. character=%s book_id=%d",
                context["character_row"]["name"],
                int(book_id),
            )

        return {
            "cached": False,
            "job_id": job_id,
            "character": context["character_row"],
            "profile": profile_json,
            "relations": relation_rows,
            "version_hash": context["version_hash"],
            "profile_slices": profile_slices,
        }
    except Exception as exc:
        with _connect() as conn:
            with conn.cursor() as cursor:
                _update_job(cursor, job_id, "error", str(exc))
        logger.exception(
            "[character_profiles] archive generation failed. character=%s book_id=%d",
            context["character_row"]["name"],
            int(book_id),
        )
        raise


def generate_character_roleplay_enhancement(book_id: int, character_id: int) -> dict[str, Any]:
    with _connect() as conn:
        with conn.cursor() as cursor:
            context = _load_character_context(cursor, int(book_id), int(character_id))
            cached_profile = _load_cached_profile(cursor, book_id, character_id, context["version_hash"])
            book_character_rows = _load_book_characters(cursor, int(book_id))

    if cached_profile is None:
        generate_character_archive(int(book_id), int(character_id))
        with _connect() as conn:
            with conn.cursor() as cursor:
                context = _load_character_context(cursor, int(book_id), int(character_id))
                cached_profile = _load_cached_profile(cursor, book_id, character_id, context["version_hash"])
                book_character_rows = _load_book_characters(cursor, int(book_id))
        if cached_profile is None:
            raise RuntimeError("Base character archive could not be generated before roleplay enhancement.")

    roleplay_windows = _split_roleplay_windows(context["entries"])
    chapter_indexes = [entry.chapter_index for window in roleplay_windows for entry in window.entries]
    with _connect() as conn:
        with conn.cursor() as cursor:
            chapter_contents = _load_chapter_contents(cursor, int(book_id), chapter_indexes)

    payloads = _build_roleplay_window_payloads(roleplay_windows, chapter_contents, context["character_row"]["aliases"])
    alias_map, id_map = _build_alias_maps(book_character_rows)
    style_batches = _generate_roleplay_style_batches(context["character_row"]["name"], payloads)
    style_summary = _summarize_roleplay_style(context["character_row"]["name"], style_batches) if style_batches else {
        "style_summary": "",
        "speech_style": [],
        "style_samples": [],
    }
    relation_batches = _generate_roleplay_relation_batches(
        context["character_row"]["name"],
        payloads,
        alias_map,
        id_map,
    )
    emotional_relations = _summarize_roleplay_relations(context["character_row"]["name"], relation_batches)

    base_profile = cached_profile["profile_json"]
    profile_json = _normalize_profile_json(
        {
            **base_profile,
            **style_summary,
            "emotional_relations": emotional_relations,
        },
        context["character_row"],
        cached_profile["first_chapter_index"],
        cached_profile["last_chapter_index"],
        context["character_row"]["aliases"],
    )

    with _connect() as conn:
        with conn.cursor() as cursor:
            source_chapters = [entry.chapter_index for entry in context["entries"]]
            _save_profile(cursor, book_id, context["character_row"], context["version_hash"], profile_json, source_chapters)

    return {
        "cached": False,
        "character": context["character_row"],
        "profile": profile_json,
        "version_hash": context["version_hash"],
        "style_batches": style_batches,
        "relation_batches": relation_batches,
    }
