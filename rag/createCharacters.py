from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv
import pymysql


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from agent.graph import build_llm
from rag.prompt import (
    CHARACTER_CANONICAL_REWRITE_PROMPT,
    CHARACTER_DIRTY_REVIEW_PROMPT,
    CHARACTER_GROUP_FINALIZE_PROMPT,
    CHARACTER_GENERIC_REWRITE_PROMPT,
    CHARACTER_MERGE_CANDIDATE_PROMPT,
    CHARACTER_MERGE_DECISION_PROMPT,
)

ENV_OVERRIDE_VAR = "STORY2MEMORY_ENV_OVERRIDE"
logger = logging.getLogger(__name__)
CHARACTERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `characters` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `book_id` INT NOT NULL,
    `name` VARCHAR(255) NOT NULL,
    `aliases` JSON NOT NULL,
    `records` JSON NOT NULL,
    `NEED_DELETE` ENUM('yes', 'no') NOT NULL DEFAULT 'no'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""
CHARACTER_DIRTY_REVIEW_BATCH_SIZE = 50
CHARACTER_DIRTY_REVIEW_MAX_CONCURRENCY = 20
CHARACTER_GENERIC_REWRITE_MODEL = "Doubao-Seed-2.0-lite"
CHARACTER_GENERIC_REWRITE_MAX_CONTEXT_CHAPTERS = 6
CHARACTER_GENERIC_REWRITE_MAX_CONTENT_EXCERPT_CHARS = 240
CHARACTER_GENERIC_REWRITE_MAX_CONCURRENCY = 25
CHARACTER_GROUP_FINALIZE_MODEL = "Doubao-Seed-2.0-lite"
CHARACTER_GROUP_FINALIZE_MAX_CONCURRENCY = 6
CHARACTER_CANONICAL_REWRITE_BATCH_SIZE = 60
CHARACTER_CANONICAL_REWRITE_MAX_CONCURRENCY = 12
CHARACTER_MERGE_RECALL_BLOCK_MAX_ITEMS = 24
CHARACTER_MERGE_RECALL_MAX_CONCURRENCY = 12
CHARACTER_MERGE_DECISION_MAX_CONCURRENCY = 16
CHARACTER_MERGE_RECORD_SAMPLE_LIMIT = 4
BLOCKED_CHARACTER_ALIASES = {
    "鬼",
    "人",
    "他",
    "她",
    "它",
    "老人",
    "老人家",
    "年轻人",
    "女人",
    "男人",
    "小孩",
    "孩子",
    "学生",
    "同学",
    "老师",
    "教授",
    "队长",
    "负责人",
    "老板",
    "店老板",
    "司机",
    "售货员",
    "青年",
    "少女",
    "少年",
    "父亲",
    "母亲",
    "儿子",
    "女儿",
    "那个人",
    "那只鬼",
    "那个老人",
    "黑影",
    "一个女生",
    "一个同学",
    "一个男人",
    "一个女人",
    "一个学生",
    "一个青年",
    "一个年轻人",
    "一个老人",
    "一个小孩",
    "一个孩子",
    "小年轻",
    "同伴",
    "伙伴",
    "光头壮汉",
    "光头",
    "青年男子",
    "青年女人",
    "青年女子",
    "年轻男子",
    "年轻女子",
    "中年男人",
    "中年女人",
    "壮汉",
    "女生",
    "男生",
    "妈妈",
    "爸爸",
    "叔叔",
    "婶婶",
    "阿姨",
    "伯父",
    "伯母",
    "舅舅",
    "姑妈",
    "老板娘",
    "护士长",
    "医生",
    "主治医生",
}
BLOCKED_CHARACTER_ALIAS_PATTERNS = (
    re.compile(
        r"^(?:那|那个|这|这个|一名|一位|某个|某位|那位|这位|那只|这只|一个)?"
        r"(?:鬼|人|老人|年轻人|女人|男人|小孩|孩子|学生|同学|老师|教授|队长|负责人|老板|司机|售货员|青年|少女|少年|同伴|伙伴|小年轻|光头|壮汉|光头壮汉|女生|男生|青年男子|青年女子|年轻男子|年轻女子|中年男人|中年女人)$"
    ),
)
ALIAS_PREFIX_PATTERNS = (
    re.compile(r"^(?:那个|这个|那只|这只|一只|一个|一名|一位|某个|某位|那名|这名)+"),
)
ALIAS_DE_SUFFIXES = (
    "老板",
    "负责人",
    "店长",
    "店老板",
    "司机",
    "同学",
    "老师",
    "教授",
    "朋友",
    "同伴",
    "男人",
    "女人",
    "年轻人",
    "老人",
    "学生",
    "青年",
    "青年男子",
    "青年女子",
    "年轻男子",
    "年轻女子",
    "中年男人",
    "中年女人",
    "壮汉",
    "女生",
    "男生",
)
GENERIC_RELATION_OR_TITLE_TERMS = {
    "妈妈",
    "爸爸",
    "父亲",
    "母亲",
    "叔叔",
    "婶婶",
    "阿姨",
    "伯父",
    "伯母",
    "舅舅",
    "姑妈",
    "哥哥",
    "姐姐",
    "弟弟",
    "妹妹",
    "师兄",
    "师姐",
    "老板",
    "老板娘",
    "护士长",
    "医生",
    "主治医生",
    "主任",
    "校长",
    "老师",
    "教授",
    "会长",
    "主席",
    "家主",
}
NICKNAME_PREFIXES = ("小", "老", "阿")
GENERIC_DESCRIPTOR_SUFFIXES = (
    "孩子",
    "男孩",
    "女孩",
    "女人",
    "男人",
    "秘书",
    "女秘书",
    "老板",
    "老板娘",
    "经理",
    "护士",
    "研究人员",
    "客人",
    "大副",
    "女王",
)


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


def _connect():
    _load_runtime_env()
    dsn = os.getenv("MYSQL_DSN", "").strip()
    cfg = _parse_mysql_dsn(dsn)
    if not cfg:
        raise RuntimeError("Missing or invalid MYSQL_DSN.")
    return pymysql.connect(**cfg)


def _ensure_characters_schema() -> None:
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(CHARACTERS_TABLE_SQL)
            cursor.execute(
                """
                SELECT COLUMN_DEFAULT, IS_NULLABLE
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'characters'
                  AND COLUMN_NAME = 'NEED_DELETE'
                LIMIT 1
                """
            )
            row = cursor.fetchone() or {}
            if not row:
                cursor.execute(
                    """
                    ALTER TABLE `characters`
                    ADD COLUMN `NEED_DELETE` ENUM('yes', 'no') NOT NULL DEFAULT 'no'
                    AFTER `records`
                    """
                )
                return
            if str(row.get("COLUMN_DEFAULT") or "").strip().lower() != "no" or str(row.get("IS_NULLABLE") or "").strip().upper() != "NO":
                cursor.execute(
                    """
                    UPDATE `characters`
                    SET `NEED_DELETE` = 'no'
                    WHERE `NEED_DELETE` IS NULL
                    """
                )
                cursor.execute(
                    """
                    ALTER TABLE `characters`
                    MODIFY COLUMN `NEED_DELETE` ENUM('yes', 'no') NOT NULL DEFAULT 'no'
                    """
                )


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


def _extract_json_object(text: str) -> dict[str, Any] | None:
    payload = str(text or "").strip()
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", payload, flags=re.IGNORECASE)
    if fenced:
        payload = fenced.group(1).strip()
    left = payload.find("{")
    right = payload.rfind("}")
    if left >= 0 and right > left:
        try:
            parsed = json.loads(payload[left : right + 1])
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _normalize_text_key(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).lower()


def _dedupe_texts(values: list[Any]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        key = _normalize_text_key(text)
        if not key or key in seen:
            continue
        deduped.append(text)
        seen.add(key)
    return deduped


def _is_anchored_character_descriptor(text: str) -> bool:
    normalized = re.sub(r"\s+", "", str(text or "").strip())
    if not normalized or "的" not in normalized:
        return False
    left, right = normalized.split("的", 1)
    if not left or not right:
        return False
    if right in GENERIC_RELATION_OR_TITLE_TERMS:
        return True
    return any(right.endswith(term) for term in GENERIC_RELATION_OR_TITLE_TERMS)


def _is_unanchored_generic_character_name(text: str) -> bool:
    normalized = re.sub(r"\s+", "", str(text or "").strip())
    if not normalized:
        return True
    if _is_anchored_character_descriptor(normalized):
        return False
    if normalized in BLOCKED_CHARACTER_ALIASES:
        return True
    if normalized in GENERIC_RELATION_OR_TITLE_TERMS:
        return True
    return any(pattern.match(normalized) for pattern in BLOCKED_CHARACTER_ALIAS_PATTERNS)


def _sanitize_character_candidate(text: str) -> str:
    cleaned = str(text or "").strip().strip("\"'`“”‘’")
    if not cleaned:
        return ""
    if _is_unanchored_generic_character_name(cleaned):
        return ""
    return cleaned


def _sanitize_character_candidates(values: list[Any]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _sanitize_character_candidate(str(item or "").strip())
        key = _normalize_text_key(text)
        if not key or key in seen:
            continue
        cleaned.append(text)
        seen.add(key)
    return cleaned


def _is_likely_nickname(text: str) -> bool:
    normalized = re.sub(r"\s+", "", str(text or "").strip())
    if not normalized or _is_anchored_character_descriptor(normalized):
        return False
    return any(normalized.startswith(prefix) for prefix in NICKNAME_PREFIXES)


def _looks_like_specific_person_name(text: str) -> bool:
    normalized = re.sub(r"\s+", "", str(text or "").strip())
    if not normalized or _is_anchored_character_descriptor(normalized) or _is_likely_nickname(normalized):
        return False
    if len(normalized) < 2:
        return False
    if any(normalized.endswith(suffix) for suffix in GENERIC_DESCRIPTOR_SUFFIXES):
        return False
    return True


def _character_name_rank(text: str) -> tuple[int, int, str]:
    candidate = str(text or "").strip()
    if _looks_like_specific_person_name(candidate):
        return (0, -len(candidate), _normalize_text_key(candidate))
    if _is_anchored_character_descriptor(candidate):
        return (1, -len(candidate), _normalize_text_key(candidate))
    if _is_likely_nickname(candidate):
        return (3, -len(candidate), _normalize_text_key(candidate))
    return (2, -len(candidate), _normalize_text_key(candidate))


def _pick_preferred_character_name(name: str, aliases: list[str]) -> str:
    raw_name = str(name or "").strip()
    candidates = _sanitize_character_candidates([raw_name, *aliases])
    if candidates:
        return sorted(candidates, key=_character_name_rank)[0]
    return raw_name


def _hash_text(value: str) -> str:
    digest = hashlib.sha256()
    digest.update(value.encode("utf-8"))
    return digest.hexdigest()


def _split_slash_text(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    parts = [item.strip() for item in re.split(r"\s*[\/／]\s*", text) if str(item or "").strip()]
    return parts or [text]


def _normalize_aliases(name: str, aliases: Any, extra_aliases: list[str] | None = None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    candidates: list[str] = []
    extra_candidates = [str(item or "").strip() for item in (extra_aliases or [])]
    for item in [name, *extra_candidates, *[str(alias or "").strip() for alias in _parse_json_list(aliases)]]:
        candidates.extend(_split_slash_text(item))
    for item in candidates:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        normalized.append(text)
        seen.add(text)
    return normalized


def _should_drop_alias(alias: str) -> bool:
    text = re.sub(r"\s+", "", str(alias or "").strip())
    if not text:
        return True
    if text in BLOCKED_CHARACTER_ALIASES:
        return True
    return any(pattern.match(text) for pattern in BLOCKED_CHARACTER_ALIAS_PATTERNS)


def _filter_aliases_before_merge(aliases: list[str]) -> list[str]:
    return [alias for alias in aliases if not _should_drop_alias(alias)]


def _normalize_alias_text_before_merge(alias: str) -> str:
    text = re.sub(r"\s+", "", str(alias or "").strip())
    if not text:
        return ""
    if _is_anchored_character_descriptor(text):
        return text
    for pattern in ALIAS_PREFIX_PATTERNS:
        text = pattern.sub("", text)
    return text.strip()


def _normalize_aliases_before_merge(aliases: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        text = _normalize_alias_text_before_merge(alias)
        if not text or text in seen:
            continue
        normalized.append(text)
        seen.add(text)
    return normalized


def _normalize_records(records: Any) -> list[list[Any]]:
    normalized: list[list[Any]] = []
    for item in _parse_json_list(records):
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        chapter_or_plot = item[0]
        description = str(item[1] or "").strip()
        try:
            normalized_index = int(chapter_or_plot)
        except (TypeError, ValueError):
            normalized_index = chapter_or_plot
        normalized.append([normalized_index, description])
    return normalized


def _normalize_character_entry(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    raw_name = str(item.get("name") or "").strip()
    split_name_parts = _split_slash_text(raw_name)
    name = split_name_parts[0] if split_name_parts else raw_name
    aliases = _normalize_aliases_before_merge(
        _filter_aliases_before_merge(
            _normalize_aliases(name, item.get("aliases"), extra_aliases=split_name_parts[1:])
        )
    )
    preferred_name = _pick_preferred_character_name(name, aliases)
    aliases = _sanitize_character_candidates([preferred_name, *aliases])
    if not aliases:
        return None
    normalized_name = preferred_name or aliases[0]
    return {
        "name": normalized_name,
        "aliases": aliases,
        "records": _normalize_records(item.get("records")),
    }


def _extract_content_excerpt(content: Any, keyword: str, limit: int = CHARACTER_GENERIC_REWRITE_MAX_CONTENT_EXCERPT_CHARS) -> str:
    text = str(content or "").strip()
    needle = str(keyword or "").strip()
    if not text:
        return ""
    if not needle:
        return text[:limit]
    index = text.find(needle)
    if index < 0:
        return text[:limit]
    half = max(20, limit // 2)
    start = max(0, index - half)
    end = min(len(text), index + len(needle) + half)
    return text[start:end].strip()


def _normalize_ambiguous_character_mentions(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        surface_name = str(
            item.get("surface_name")
            or item.get("name")
            or item.get("title")
            or item.get("entity")
            or item.get("item")
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
        evidence_excerpt = str(
            item.get("evidence_excerpt")
            or item.get("excerpt")
            or item.get("evidence")
            or item.get("quote")
            or ""
        ).strip()
        signature = (surface_name, description, evidence_excerpt)
        if not any(signature) or signature in seen:
            continue
        normalized.append(
            {
                "surface_name": surface_name,
                "description": description,
                "evidence_excerpt": evidence_excerpt,
            }
        )
        seen.add(signature)
    return normalized


def _load_chapter_contexts(book_id: int, chapter_indexes: list[int]) -> dict[int, dict[str, Any]]:
    normalized_indexes = sorted({int(item) for item in chapter_indexes if int(item) > 0})
    if not normalized_indexes:
        return {}
    placeholders = ", ".join(["%s"] * len(normalized_indexes))
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT chapter_index, chapter_summary, raw_summary_json, content
                FROM book_chapters
                WHERE book_id = %s AND chapter_index IN ({placeholders})
                ORDER BY chapter_index ASC
                """,
                [int(book_id), *normalized_indexes],
            )
            rows = list(cursor.fetchall() or [])

    contexts: dict[int, dict[str, Any]] = {}
    for row in rows:
        raw_summary = row.get("raw_summary_json")
        payload = raw_summary if isinstance(raw_summary, dict) else _extract_json_object(str(raw_summary or ""))
        contexts[int(row.get("chapter_index") or 0)] = {
            "chapter_summary": str(row.get("chapter_summary") or "").strip(),
            "known_characters": _normalize_named_description_list((payload or {}).get("character")),
            "ambiguous_character_mentions": _normalize_ambiguous_character_mentions((payload or {}).get("ambiguous_character_mentions")),
            "content_excerpt": str(row.get("content") or ""),
        }
    return contexts


def _build_generic_rewrite_context(item: dict[str, Any], chapter_contexts: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    chapter_indexes = [int(record[0]) for record in _normalize_records(item.get("records")) if int(record[0]) > 0]
    seen: set[int] = set()
    for chapter_index in chapter_indexes[:CHARACTER_GENERIC_REWRITE_MAX_CONTEXT_CHAPTERS]:
        if chapter_index in seen:
            continue
        seen.add(chapter_index)
        chapter_row = chapter_contexts.get(chapter_index, {})
        record_descriptions = [
            str(record[1] or "").strip()
            for record in _normalize_records(item.get("records"))
            if int(record[0]) == chapter_index and str(record[1] or "").strip()
        ]
        known_characters = [entry.get("name") for entry in chapter_row.get("known_characters", []) if str(entry.get("name") or "").strip()]
        contexts.append(
            {
                "chapter_index": chapter_index,
                "record_descriptions": record_descriptions,
                "chapter_summary": str(chapter_row.get("chapter_summary") or "").strip(),
                "known_characters": known_characters,
                "ambiguous_character_mentions": chapter_row.get("ambiguous_character_mentions", []),
                "content_excerpt": _extract_content_excerpt(chapter_row.get("content_excerpt"), str(item.get("name") or "").strip()),
            }
        )
    return contexts


def _apply_generic_character_rewrite_result(original_item: dict[str, Any], rewritten_item: dict[str, Any]) -> dict[str, Any] | None:
    action = str(rewritten_item.get("action") or "").strip().lower()
    if action != "rewrite":
        return None

    raw_name = str(rewritten_item.get("canonical_name") or "").strip()
    canonical_name = _sanitize_character_candidate(raw_name)
    if not canonical_name:
        return None

    selected_aliases = _sanitize_character_candidates(
        [
            canonical_name,
            *[str(alias or "").strip() for alias in rewritten_item.get("aliases") or []],
        ]
    )
    aliases = [alias for alias in selected_aliases if not _is_unanchored_generic_character_name(alias)]
    if canonical_name not in aliases:
        aliases.insert(0, canonical_name)

    return {
        **original_item,
        "name": canonical_name,
        "aliases": aliases or [canonical_name],
    }


def _extract_json_array(text: str) -> list[dict[str, Any]]:
    payload = str(text or "").strip()
    if not payload:
        return []
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", payload, flags=re.IGNORECASE)
    if fenced:
        payload = fenced.group(1).strip()
    if payload.startswith("[") and payload.endswith("]"):
        candidate = payload
    else:
        matched = re.search(r"\[[\s\S]*\]", payload)
        candidate = matched.group(0).strip() if matched else payload
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _requires_generic_character_rewrite(item: dict[str, Any]) -> bool:
    return _is_unanchored_generic_character_name(str(item.get("name") or "").strip())


def _chunk_items(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    step = max(1, int(size))
    return [items[index : index + step] for index in range(0, len(items), step)]


async def _invoke_llm_text(llm_client: Any, prompt: str) -> str:
    ainvoke = getattr(llm_client, "ainvoke", None)
    if callable(ainvoke):
        response = await ainvoke(prompt)
    else:
        response = await asyncio.to_thread(llm_client.invoke, prompt)
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


async def _rewrite_single_generic_character_item(
    llm_client: Any,
    semaphore: asyncio.Semaphore,
    item: dict[str, Any],
    chapter_contexts: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    prompt_payload = {
        "name": str(item.get("name") or "").strip(),
        "aliases": [str(alias or "").strip() for alias in item.get("aliases") or [] if str(alias or "").strip()],
        "records": _normalize_records(item.get("records")),
        "chapter_contexts": _build_generic_rewrite_context(item, chapter_contexts),
    }
    prompt = CHARACTER_GENERIC_REWRITE_PROMPT.format(
        item_json=json.dumps(prompt_payload, ensure_ascii=False, indent=2)
    )
    async with semaphore:
        raw_text = await _invoke_llm_text(llm_client, prompt)
    return _extract_json_object(raw_text)


async def _rewrite_generic_character_items_async(
    llm_client: Any,
    items: list[dict[str, Any]],
    chapter_contexts: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(CHARACTER_GENERIC_REWRITE_MAX_CONCURRENCY)

    async def _run_single(index: int, item: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
        if not _requires_generic_character_rewrite(item):
            return index, item
        try:
            rewritten = await _rewrite_single_generic_character_item(llm_client, semaphore, item, chapter_contexts)
        except Exception as exc:
            logger.warning("[characters] generic rewrite failed for %s: %s", item.get("name"), exc)
            rewritten = {}
        return index, _apply_generic_character_rewrite_result(item, rewritten or {})

    tasks = [asyncio.create_task(_run_single(index, item)) for index, item in enumerate(items)]
    rewritten_by_index: dict[int, dict[str, Any]] = {}
    for task in asyncio.as_completed(tasks):
        index, row = await task
        if row is not None:
            rewritten_by_index[index] = row
    return [rewritten_by_index[index] for index in sorted(rewritten_by_index)]


def _rewrite_generic_character_items(book_id: int, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not items:
        return []
    chapter_indexes: list[int] = []
    for item in items:
        if not _requires_generic_character_rewrite(item):
            continue
        chapter_indexes.extend(int(record[0]) for record in _normalize_records(item.get("records")) if int(record[0]) > 0)
    if not chapter_indexes:
        return items

    chapter_contexts = _load_chapter_contexts(book_id, chapter_indexes)
    llm_client = build_llm(CHARACTER_GENERIC_REWRITE_MODEL)
    return asyncio.run(_rewrite_generic_character_items_async(llm_client, items, chapter_contexts))


async def _review_single_character_batch(
    llm_client: Any,
    semaphore: asyncio.Semaphore,
    batch_items: list[dict[str, Any]],
) -> dict[str, str]:
    prompt_items = [
        {
            "name": item["name"],
            "aliases": item["aliases"],
            "records": item["records"],
        }
        for item in batch_items
    ]
    prompt = CHARACTER_DIRTY_REVIEW_PROMPT.format(
        items_json=json.dumps(prompt_items, ensure_ascii=False)
    )
    async with semaphore:
        raw_text = await _invoke_llm_text(llm_client, prompt)
    parsed = _extract_json_array(raw_text)
    decisions: dict[str, str] = {}
    for item in parsed:
        name = str(item.get("name") or "").strip()
        decision = str(item.get("NEED_DELETE") or "").strip().lower()
        if not name:
            continue
        decisions[name] = "yes" if decision == "yes" else "no"
    for item in batch_items:
        decisions.setdefault(item["name"], "no")
    return decisions


async def _review_character_batches(
    llm_client: Any,
    batches: list[list[dict[str, Any]]],
) -> dict[tuple[int, str], str]:
    semaphore = asyncio.Semaphore(CHARACTER_DIRTY_REVIEW_MAX_CONCURRENCY)

    async def _run_single(batch_items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
        try:
            decisions = await _review_single_character_batch(llm_client, semaphore, batch_items)
        except Exception as exc:
            logger.warning("[characters] dirty review batch failed: %s", exc)
            decisions = {item["name"]: "no" for item in batch_items}
        return batch_items, decisions

    tasks = [asyncio.create_task(_run_single(batch)) for batch in batches]
    merged: dict[tuple[int, str], str] = {}
    for task in asyncio.as_completed(tasks):
        batch_items, decisions = await task
        for item in batch_items:
            merged[(int(item["book_id"]), item["name"])] = decisions.get(item["name"], "no")
    return merged


def _load_single_record_character_rows(
    book_id: int | None = None,
    row_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    if row_ids is not None and not row_ids:
        return []
    with _connect() as conn:
        with conn.cursor() as cursor:
            if row_ids:
                placeholders = ", ".join(["%s"] * len(row_ids))
                params: list[Any] = [int(row_id) for row_id in row_ids]
                sql = f"""
                    SELECT id, book_id, name, aliases, records
                    FROM `characters`
                    WHERE id IN ({placeholders})
                      AND JSON_LENGTH(`records`) = 1
                    ORDER BY book_id ASC, id ASC
                    """
                cursor.execute(sql, params)
            elif book_id is None:
                cursor.execute(
                    """
                    SELECT id, book_id, name, aliases, records
                    FROM `characters`
                    WHERE JSON_LENGTH(`records`) = 1
                    ORDER BY book_id ASC, id ASC
                    """
                )
            else:
                cursor.execute(
                    """
                    SELECT id, book_id, name, aliases, records
                    FROM `characters`
                    WHERE book_id = %s
                      AND JSON_LENGTH(`records`) = 1
                    ORDER BY id ASC
                    """,
                    (int(book_id),),
                )
            rows = list(cursor.fetchall() or [])
    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        normalized_rows.append(
            {
                "id": int(row.get("id") or 0),
                "book_id": int(row.get("book_id") or 0),
                "name": str(row.get("name") or "").strip(),
                "aliases": _parse_json_list(row.get("aliases")),
                "records": _parse_json_list(row.get("records")),
            }
        )
    return [row for row in normalized_rows if row["id"] > 0 and row["book_id"] > 0 and row["name"]]


def _write_need_delete_flags(decisions: dict[tuple[int, str], str]) -> int:
    updated = 0
    if not decisions:
        return updated
    with _connect() as conn:
        with conn.cursor() as cursor:
            for (book_id, name), decision in decisions.items():
                cursor.execute(
                    """
                    UPDATE `characters`
                    SET `NEED_DELETE` = %s
                    WHERE book_id = %s AND name = %s AND JSON_LENGTH(`records`) = 1
                    """,
                    (decision, book_id, name),
                )
                updated += int(cursor.rowcount or 0)
    return updated


def _run_dirty_character_review(
    book_id: int | None = None,
    row_ids: list[int] | None = None,
) -> dict[str, int]:
    rows = _load_single_record_character_rows(book_id, row_ids)
    if not rows:
        return {"reviewed_rows": 0, "updated_rows": 0, "batches": 0}
    llm_client = build_llm()
    batches = _chunk_items(rows, CHARACTER_DIRTY_REVIEW_BATCH_SIZE)
    decisions = asyncio.run(_review_character_batches(llm_client, batches))
    updated_rows = _write_need_delete_flags(decisions)
    return {
        "reviewed_rows": len(rows),
        "updated_rows": updated_rows,
        "batches": len(batches),
    }


def _ensure_character_item_ids(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        row = dict(item)
        row.setdefault("_item_id", f"character-item-{index + 1}")
        normalized.append(row)
    return normalized


def _available_character_name_candidates(item: dict[str, Any]) -> list[str]:
    return _dedupe_texts([item.get("name"), *list(item.get("aliases") or [])])


def _resolve_candidate_from_allowed(raw_value: Any, allowed: list[str]) -> str:
    normalized_allowed = {
        _normalize_text_key(candidate): candidate
        for candidate in allowed
        if _normalize_text_key(candidate)
    }
    return normalized_allowed.get(_normalize_text_key(raw_value), "")


def _apply_character_rewrite_result(original_item: dict[str, Any], rewritten_item: dict[str, Any]) -> dict[str, Any]:
    allowed_candidates = _available_character_name_candidates(original_item)
    canonical_name = _resolve_candidate_from_allowed(
        rewritten_item.get("canonical_name") or rewritten_item.get("name"),
        allowed_candidates,
    )
    if not canonical_name:
        canonical_name = _pick_preferred_character_name(
            str(original_item.get("name") or "").strip(),
            [str(alias or "").strip() for alias in original_item.get("aliases") or []],
        )

    selected_aliases = [
        _resolve_candidate_from_allowed(alias, allowed_candidates)
        for alias in list(rewritten_item.get("aliases") or [])
    ]
    aliases = _sanitize_character_candidates([canonical_name, *selected_aliases])
    if not aliases and canonical_name:
        aliases = [canonical_name]

    return {
        **original_item,
        "name": canonical_name,
        "aliases": aliases,
    }


async def _rewrite_single_character_batch(
    llm_client: Any,
    semaphore: asyncio.Semaphore,
    batch_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    prompt_items = [
        {
            "item_id": str(item.get("_item_id") or ""),
            "name": str(item.get("name") or "").strip(),
            "aliases": [str(alias or "").strip() for alias in item.get("aliases") or [] if str(alias or "").strip()],
        }
        for item in batch_items
    ]
    prompt = CHARACTER_CANONICAL_REWRITE_PROMPT.format(items_json=json.dumps(prompt_items, ensure_ascii=False))
    async with semaphore:
        raw_text = await _invoke_llm_text(llm_client, prompt)
    return _extract_json_array(raw_text)


async def _rewrite_character_batches(
    llm_client: Any,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not items:
        return []
    semaphore = asyncio.Semaphore(CHARACTER_CANONICAL_REWRITE_MAX_CONCURRENCY)
    batches = _chunk_items(items, CHARACTER_CANONICAL_REWRITE_BATCH_SIZE)

    async def _run_single(batch_items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        try:
            rewritten = await _rewrite_single_character_batch(llm_client, semaphore, batch_items)
        except Exception as exc:
            logger.warning("[characters] canonical rewrite batch failed: %s", exc)
            rewritten = []
        return batch_items, rewritten

    rewrite_map: dict[str, dict[str, Any]] = {}
    tasks = [asyncio.create_task(_run_single(batch)) for batch in batches]
    for task in asyncio.as_completed(tasks):
        batch_items, rewritten = await task
        valid_ids = {str(item.get("_item_id") or "") for item in batch_items}
        for row in rewritten:
            item_id = str(row.get("item_id") or "").strip()
            if not item_id or item_id not in valid_ids:
                continue
            rewrite_map[item_id] = row

    return [
        _apply_character_rewrite_result(item, rewrite_map.get(str(item.get("_item_id") or ""), {}))
        for item in items
    ]


def _candidate_blocking_keys(item: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for candidate in _available_character_name_candidates(item):
        text = _sanitize_character_candidate(candidate)
        key = _normalize_text_key(text)
        if not key or len(key) < 2 or key in seen:
            continue
        keys.append(key)
        seen.add(key)
    return keys


def _build_merge_recall_blocks(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    by_key: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        for key in _candidate_blocking_keys(item):
            by_key.setdefault(key, []).append(item)

    blocks: list[list[dict[str, Any]]] = []
    seen_blocks: set[tuple[str, ...]] = set()
    overlap = 4
    step = max(1, CHARACTER_MERGE_RECALL_BLOCK_MAX_ITEMS - overlap)
    for rows in by_key.values():
        deduped: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for row in rows:
            item_id = str(row.get("_item_id") or "")
            if not item_id or item_id in seen_ids:
                continue
            deduped.append(row)
            seen_ids.add(item_id)
        if len(deduped) < 2:
            continue
        deduped.sort(key=lambda row: str(row.get("name") or ""))
        if len(deduped) <= CHARACTER_MERGE_RECALL_BLOCK_MAX_ITEMS:
            signature = tuple(str(row.get("_item_id") or "") for row in deduped)
            if signature not in seen_blocks:
                blocks.append(deduped)
                seen_blocks.add(signature)
            continue
        for index in range(0, len(deduped), step):
            chunk = deduped[index : index + CHARACTER_MERGE_RECALL_BLOCK_MAX_ITEMS]
            if len(chunk) < 2:
                continue
            signature = tuple(str(row.get("_item_id") or "") for row in chunk)
            if signature in seen_blocks:
                continue
            blocks.append(chunk)
            seen_blocks.add(signature)
    return blocks


async def _recall_single_merge_block(
    llm_client: Any,
    semaphore: asyncio.Semaphore,
    block_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    prompt_items = [
        {
            "item_id": str(item.get("_item_id") or ""),
            "name": str(item.get("name") or "").strip(),
            "aliases": [str(alias or "").strip() for alias in item.get("aliases") or [] if str(alias or "").strip()],
        }
        for item in block_items
    ]
    prompt = CHARACTER_MERGE_CANDIDATE_PROMPT.format(items_json=json.dumps(prompt_items, ensure_ascii=False))
    async with semaphore:
        raw_text = await _invoke_llm_text(llm_client, prompt)
    return _extract_json_array(raw_text)


async def _recall_merge_candidate_pairs(
    llm_client: Any,
    items: list[dict[str, Any]],
) -> set[frozenset[str]]:
    blocks = _build_merge_recall_blocks(items)
    if not blocks:
        return set()
    semaphore = asyncio.Semaphore(CHARACTER_MERGE_RECALL_MAX_CONCURRENCY)
    pairs: set[frozenset[str]] = set()

    async def _run_single(block_items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        try:
            recalled = await _recall_single_merge_block(llm_client, semaphore, block_items)
        except Exception as exc:
            logger.warning("[characters] merge recall block failed: %s", exc)
            recalled = []
        return block_items, recalled

    tasks = [asyncio.create_task(_run_single(block)) for block in blocks]
    for task in asyncio.as_completed(tasks):
        block_items, recalled = await task
        valid_ids = {str(item.get("_item_id") or "") for item in block_items}
        for row in recalled:
            left = str(row.get("left_item_id") or "").strip()
            right = str(row.get("right_item_id") or "").strip()
            if not left or not right or left == right:
                continue
            if left not in valid_ids or right not in valid_ids:
                continue
            pairs.add(frozenset({left, right}))
    return pairs


def _sample_character_records(item: dict[str, Any], limit: int = CHARACTER_MERGE_RECORD_SAMPLE_LIMIT) -> list[list[Any]]:
    records = _normalize_records(item.get("records"))
    if len(records) <= limit:
        return records
    head = records[: max(1, limit // 2)]
    tail = records[-max(1, limit - len(head)) :]
    sampled: list[list[Any]] = []
    seen: set[tuple[Any, Any]] = set()
    for record in [*head, *tail]:
        signature = (record[0], record[1])
        if signature in seen:
            continue
        sampled.append(record)
        seen.add(signature)
    return sampled[:limit]


async def _decide_single_merge_pair(
    llm_client: Any,
    semaphore: asyncio.Semaphore,
    left_item: dict[str, Any],
    right_item: dict[str, Any],
) -> dict[str, Any]:
    prompt = CHARACTER_MERGE_DECISION_PROMPT.format(
        left_json=json.dumps(
            {
                "item_id": str(left_item.get("_item_id") or ""),
                "name": str(left_item.get("name") or "").strip(),
                "aliases": [str(alias or "").strip() for alias in left_item.get("aliases") or [] if str(alias or "").strip()],
                "records": _sample_character_records(left_item),
            },
            ensure_ascii=False,
            indent=2,
        ),
        right_json=json.dumps(
            {
                "item_id": str(right_item.get("_item_id") or ""),
                "name": str(right_item.get("name") or "").strip(),
                "aliases": [str(alias or "").strip() for alias in right_item.get("aliases") or [] if str(alias or "").strip()],
                "records": _sample_character_records(right_item),
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    async with semaphore:
        raw_text = await _invoke_llm_text(llm_client, prompt)
    return _extract_json_object(raw_text) or {}


async def _decide_merge_pairs(
    llm_client: Any,
    items: list[dict[str, Any]],
    candidate_pairs: set[frozenset[str]],
) -> dict[frozenset[str], str]:
    if not candidate_pairs:
        return {}
    items_by_id = {str(item.get("_item_id") or ""): item for item in items}
    semaphore = asyncio.Semaphore(CHARACTER_MERGE_DECISION_MAX_CONCURRENCY)
    decisions: dict[frozenset[str], str] = {}

    async def _run_single(pair: frozenset[str]) -> tuple[frozenset[str], str]:
        left_id, right_id = sorted(pair)
        left_item = items_by_id.get(left_id)
        right_item = items_by_id.get(right_id)
        if left_item is None or right_item is None:
            return pair, "different_person"
        try:
            payload = await _decide_single_merge_pair(llm_client, semaphore, left_item, right_item)
        except Exception as exc:
            logger.warning("[characters] merge decision failed for %s/%s: %s", left_id, right_id, exc)
            payload = {}
        decision = str(payload.get("decision") or "").strip().lower()
        if decision not in {"same_person", "different_person", "uncertain"}:
            decision = "uncertain"
        return pair, decision

    tasks = [asyncio.create_task(_run_single(pair)) for pair in sorted(candidate_pairs, key=lambda pair: tuple(sorted(pair)))]
    for task in asyncio.as_completed(tasks):
        pair, decision = await task
        decisions[pair] = decision
    return decisions


def _group_items_for_merge(
    items: list[dict[str, Any]],
    decisions: dict[frozenset[str], str],
) -> list[list[dict[str, Any]]]:
    items_by_id = {str(item.get("_item_id") or ""): item for item in items}
    same_pairs = {
        pair
        for pair, decision in decisions.items()
        if decision == "same_person"
    }
    remaining = {item_id for item_id in items_by_id}
    groups: list[list[dict[str, Any]]] = []

    for item_id in sorted(remaining):
        if item_id not in remaining:
            continue
        group_ids = [item_id]
        remaining.remove(item_id)
        changed = True
        while changed:
            changed = False
            for candidate_id in sorted(list(remaining)):
                if all(frozenset({candidate_id, member_id}) in same_pairs for member_id in group_ids):
                    group_ids.append(candidate_id)
                    remaining.remove(candidate_id)
                    changed = True
        groups.append([items_by_id[group_id] for group_id in group_ids])
    return groups


def _pick_group_canonical_name(group: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for item in group:
        candidate = _sanitize_character_candidate(str(item.get("name") or "").strip())
        if not candidate:
            continue
        counts[candidate] = counts.get(candidate, 0) + 1
    if counts:
        ranked = sorted(
            counts.items(),
            key=lambda row: (
                -int(row[1]),
                *_character_name_rank(row[0]),
            ),
        )
        return ranked[0][0]
    first_item = group[0] if group else {}
    return _pick_preferred_character_name(
        str(first_item.get("name") or "").strip(),
        [str(alias or "").strip() for alias in first_item.get("aliases") or []],
    )


def _merge_group_records(group: list[dict[str, Any]]) -> list[list[Any]]:
    records: list[list[Any]] = []
    seen_records: set[tuple[Any, Any]] = set()
    for item in group:
        for record in _normalize_records(item.get("records")):
            signature = (record[0], record[1])
            if signature in seen_records:
                continue
            records.append(record)
            seen_records.add(signature)
    def _record_sort_key(row: list[Any]) -> tuple[int, int | str, str]:
        chapter = row[0] if row else ""
        if isinstance(chapter, int):
            return (0, chapter, str(row[1] if len(row) > 1 else ""))
        try:
            return (0, int(chapter), str(row[1] if len(row) > 1 else ""))
        except (TypeError, ValueError):
            return (1, str(chapter), str(row[1] if len(row) > 1 else ""))

    records.sort(key=_record_sort_key)
    return records


def _available_group_name_candidates(group: list[dict[str, Any]]) -> list[str]:
    return _dedupe_texts(
        [
            item.get("name")
            for item in group
        ]
        + [
            alias
            for item in group
            for alias in list(item.get("aliases") or [])
        ]
    )


def _build_group_candidate_stats(group: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stats_by_key: dict[str, dict[str, Any]] = {}
    for item in group:
        item_id = str(item.get("_item_id") or "").strip()
        for candidate in [str(item.get("name") or "").strip(), *[str(alias or "").strip() for alias in item.get("aliases") or []]]:
            key = _normalize_text_key(candidate)
            if not key:
                continue
            row = stats_by_key.setdefault(
                key,
                {
                    "candidate": candidate,
                    "occurrences": 0,
                    "as_name_count": 0,
                    "as_alias_count": 0,
                    "item_ids": [],
                },
            )
            row["occurrences"] += 1
            if candidate == str(item.get("name") or "").strip():
                row["as_name_count"] += 1
            else:
                row["as_alias_count"] += 1
            if item_id and item_id not in row["item_ids"]:
                row["item_ids"].append(item_id)
    return sorted(
        stats_by_key.values(),
        key=lambda row: (-int(row.get("occurrences") or 0), _normalize_text_key(row.get("candidate") or "")),
    )


def _build_group_finalize_payload(group: list[dict[str, Any]]) -> dict[str, Any]:
    prompt_items: list[dict[str, Any]] = []
    for item in group:
        records = _normalize_records(item.get("records"))
        chapter_indexes = [int(record[0]) for record in records if isinstance(record[0], int)]
        chapter_span = [min(chapter_indexes), max(chapter_indexes)] if chapter_indexes else []
        prompt_items.append(
            {
                "item_id": str(item.get("_item_id") or ""),
                "name": str(item.get("name") or "").strip(),
                "aliases": [str(alias or "").strip() for alias in item.get("aliases") or [] if str(alias or "").strip()],
                "record_count": len(records),
                "chapter_span": chapter_span,
            }
        )
    return {
        "items": prompt_items,
        "candidate_stats": _build_group_candidate_stats(group),
    }


def _merge_item_group(group: list[dict[str, Any]]) -> dict[str, Any]:
    canonical_name = _pick_group_canonical_name(group)
    aliases = _sanitize_character_candidates(
        [
            canonical_name,
            *[item.get("name") for item in group],
            *[
                alias
                for item in group
                for alias in list(item.get("aliases") or [])
            ],
        ]
    )
    records = _merge_group_records(group)
    return {
        "name": canonical_name,
        "aliases": aliases,
        "records": records,
    }


def _apply_group_finalize_result(group: list[dict[str, Any]], result: dict[str, Any]) -> dict[str, Any]:
    allowed_candidates = _available_group_name_candidates(group)
    canonical_name = _resolve_candidate_from_allowed(result.get("canonical_name"), allowed_candidates)
    if not canonical_name:
        return _merge_item_group(group)

    aliases = _dedupe_texts(
        [
            _resolve_candidate_from_allowed(alias, allowed_candidates)
            for alias in list(result.get("aliases") or [])
        ]
    )
    aliases = [
        alias
        for alias in aliases
        if _normalize_text_key(alias) != _normalize_text_key(canonical_name)
    ]
    return {
        "name": canonical_name,
        "aliases": aliases,
        "records": _merge_group_records(group),
    }


async def _finalize_single_item_group(
    llm_client: Any,
    semaphore: asyncio.Semaphore,
    group: list[dict[str, Any]],
) -> dict[str, Any]:
    prompt = CHARACTER_GROUP_FINALIZE_PROMPT.format(
        group_json=json.dumps(_build_group_finalize_payload(group), ensure_ascii=False, indent=2)
    )
    async with semaphore:
        raw_text = await _invoke_llm_text(llm_client, prompt)
    return _extract_json_object(raw_text) or {}


async def _finalize_item_groups_async(
    llm_client: Any,
    groups: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(CHARACTER_GROUP_FINALIZE_MAX_CONCURRENCY)

    async def _run_single(index: int, group: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
        try:
            payload = await _finalize_single_item_group(llm_client, semaphore, group)
        except Exception as exc:
            logger.warning("[characters] group finalize failed for %s: %s", index, exc)
            payload = {}
        return index, _apply_group_finalize_result(group, payload)

    tasks = [asyncio.create_task(_run_single(index, group)) for index, group in enumerate(groups)]
    finalized_by_index: dict[int, dict[str, Any]] = {}
    for task in asyncio.as_completed(tasks):
        index, item = await task
        finalized_by_index[index] = item
    return [finalized_by_index[index] for index in sorted(finalized_by_index)]


def _finalize_item_groups(groups: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    if not groups:
        return []
    llm_client = build_llm(CHARACTER_GROUP_FINALIZE_MODEL)
    return asyncio.run(_finalize_item_groups_async(llm_client, groups))


def _run_character_rewrite_and_merge(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized_items = _ensure_character_item_ids(items)
    if not normalized_items:
        return []
    llm_client = build_llm()
    rewritten_items = asyncio.run(_rewrite_character_batches(llm_client, normalized_items))
    candidate_pairs = asyncio.run(_recall_merge_candidate_pairs(llm_client, rewritten_items))
    decisions = asyncio.run(_decide_merge_pairs(llm_client, rewritten_items, candidate_pairs))
    groups = _group_items_for_merge(rewritten_items, decisions)
    return _finalize_item_groups(groups)


def _merge_alias_lists(*alias_lists: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for alias_list in alias_lists:
        for alias in alias_list:
            text = str(alias or "").strip()
            if not text or text in seen:
                continue
            merged.append(text)
            seen.add(text)
    return merged


def _merge_character_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for item in items:
        item_aliases = set(item["aliases"])
        matching_indices = [
            index
            for index, existing in enumerate(merged)
            if item_aliases & set(existing["aliases"])
        ]
        if not matching_indices:
            merged.append(
                {
                    "name": item["name"],
                    "aliases": list(item["aliases"]),
                    "records": list(item["records"]),
                }
            )
            continue

        base_index = matching_indices[0]
        base_item = merged[base_index]
        merged_aliases = list(base_item["aliases"])
        merged_records = list(base_item["records"])
        for index in matching_indices[1:]:
            merged_aliases = _merge_alias_lists(merged_aliases, merged[index]["aliases"])
            merged_records.extend(merged[index]["records"])
        merged_aliases = _merge_alias_lists(merged_aliases, item["aliases"])
        merged_records.extend(item["records"])
        merged[base_index] = {
            "name": base_item["name"],
            "aliases": merged_aliases,
            "records": merged_records,
        }
        for index in reversed(matching_indices[1:]):
            merged.pop(index)
    return merged


def _finalize_character_aliases(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    finalized: list[dict[str, Any]] = []
    for item in items:
        canonical_name = str(item.get("name") or "").strip()
        if not canonical_name:
            continue
        aliases = [
            alias
            for alias in _dedupe_texts([str(alias or "").strip() for alias in item.get("aliases") or []])
            if _normalize_text_key(alias) != _normalize_text_key(canonical_name)
        ]
        finalized.append(
            {
                "name": canonical_name,
                "aliases": aliases,
                "records": _normalize_records(item.get("records")),
            }
        )
    return finalized


def _merge_same_canonical_character_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged_by_name: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        canonical_name = str(item.get("name") or "").strip()
        if not canonical_name:
            continue
        key = _normalize_text_key(canonical_name)
        if not key:
            continue
        existing = merged_by_name.get(key)
        normalized_aliases = [
            alias
            for alias in _dedupe_texts([str(alias or "").strip() for alias in item.get("aliases") or []])
            if _normalize_text_key(alias) != key
        ]
        if existing is None:
            merged_by_name[key] = {
                "name": canonical_name,
                "aliases": normalized_aliases,
                "records": _normalize_records(item.get("records")),
            }
            order.append(key)
            continue
        existing["aliases"] = _merge_alias_lists(existing["aliases"], normalized_aliases)
        existing["records"] = _merge_group_records([existing, item])
    return [merged_by_name[key] for key in order]


def _load_plot_character_items(book_id: int | None = None) -> dict[int, list[dict[str, Any]]]:
    by_book: dict[int, list[dict[str, Any]]] = {}
    with _connect() as conn:
        with conn.cursor() as cursor:
            if book_id is None:
                cursor.execute(
                    """
                    SELECT book_id, plot_id, `character`
                    FROM book_plots
                    WHERE `character` IS NOT NULL
                    ORDER BY book_id ASC, plot_id ASC, id ASC
                    """
                )
            else:
                cursor.execute(
                    """
                    SELECT book_id, plot_id, `character`
                    FROM book_plots
                    WHERE book_id = %s
                      AND `character` IS NOT NULL
                    ORDER BY plot_id ASC, id ASC
                    """,
                    (int(book_id),),
                )
            rows = list(cursor.fetchall() or [])

    for row in rows:
        book_id = int(row.get("book_id") or 0)
        if book_id <= 0:
            continue
        characters = _parse_json_list(row.get("character"))
        if not characters:
            continue
        bucket = by_book.setdefault(book_id, [])
        for raw_item in characters:
            normalized_item = _normalize_character_entry(raw_item)
            if normalized_item is None:
                continue
            bucket.append(normalized_item)
    return by_book


def _serialize_character_item(item: dict[str, Any]) -> str:
    payload = {
        "name": str(item.get("name") or "").strip(),
        "aliases": [str(alias or "").strip() for alias in item.get("aliases") or [] if str(alias or "").strip()],
        "records": _normalize_records(item.get("records")),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _build_character_item_key(item: dict[str, Any]) -> str:
    name = str(item.get("name") or "").strip()
    aliases = sorted({str(alias or "").strip() for alias in item.get("aliases") or [] if str(alias or "").strip()})
    payload = json.dumps({"name": name, "aliases": aliases}, ensure_ascii=False, separators=(",", ":"))
    return _hash_text(payload)


def _load_existing_character_rows(book_id: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, book_id, name, aliases, records, NEED_DELETE
                FROM `characters`
                WHERE book_id = %s
                ORDER BY id ASC
                """,
                (int(book_id),),
            )
            rows = list(cursor.fetchall() or [])
    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        normalized_rows.append(
            {
                "id": int(row.get("id") or 0),
                "book_id": int(row.get("book_id") or 0),
                "name": str(row.get("name") or "").strip(),
                "aliases": _parse_json_list(row.get("aliases")),
                "records": _parse_json_list(row.get("records")),
                "NEED_DELETE": str(row.get("NEED_DELETE") or "").strip().lower() or "no",
            }
        )
    return [row for row in normalized_rows if row["id"] > 0]


def _sync_book_characters(cursor: Any, book_id: int, items: list[dict[str, Any]]) -> dict[str, Any]:
    existing_rows = _load_existing_character_rows(book_id)
    existing_by_key = {
        _build_character_item_key(row): row
        for row in existing_rows
    }
    matched_existing_ids: set[int] = set()
    changed_row_ids: list[int] = []
    inserted = 0
    updated = 0
    skipped = 0
    for item in items:
        item_key = _build_character_item_key(item)
        item_hash = _hash_text(_serialize_character_item(item))
        existing_row = existing_by_key.get(item_key)
        if existing_row is None:
            cursor.execute(
                """
                INSERT INTO `characters` (`book_id`, `name`, `aliases`, `records`, `NEED_DELETE`)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    int(book_id),
                    item["name"],
                    json.dumps(item["aliases"], ensure_ascii=False),
                    json.dumps(item["records"], ensure_ascii=False),
                    "no",
                ),
            )
            row_id = int(cursor.lastrowid or 0)
            if row_id > 0:
                changed_row_ids.append(row_id)
            inserted += 1
            continue

        matched_existing_ids.add(int(existing_row["id"]))
        existing_hash = _hash_text(_serialize_character_item(existing_row))
        if existing_hash == item_hash:
            skipped += 1
            continue

        cursor.execute(
            """
            UPDATE `characters`
            SET `name` = %s,
                `aliases` = %s,
                `records` = %s,
                `NEED_DELETE` = %s
            WHERE id = %s
            """,
            (
                item["name"],
                json.dumps(item["aliases"], ensure_ascii=False),
                json.dumps(item["records"], ensure_ascii=False),
                "no",
                int(existing_row["id"]),
            ),
        )
        changed_row_ids.append(int(existing_row["id"]))
        updated += 1

    deleted = 0
    stale_ids = [int(row["id"]) for row in existing_rows if int(row["id"]) not in matched_existing_ids]
    if stale_ids:
        placeholders = ", ".join(["%s"] * len(stale_ids))
        cursor.execute(
            f"DELETE FROM `characters` WHERE id IN ({placeholders})",
            stale_ids,
        )
        deleted = int(cursor.rowcount or 0)

    return {
        "rows": len(items),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "deleted": deleted,
        "changed_row_ids": changed_row_ids,
    }


def rebuild_characters_table(book_id: int | None = None) -> dict[str, int]:
    _ensure_characters_schema()
    raw_items_by_book = _load_plot_character_items(book_id)
    items_by_book: dict[int, list[dict[str, Any]]] = {}
    for current_book_id, items in raw_items_by_book.items():
        generic_rewritten_items = _rewrite_generic_character_items(current_book_id, items)
        rewritten_items = _run_character_rewrite_and_merge(generic_rewritten_items)
        finalized_items = _merge_same_canonical_character_items(_finalize_character_aliases(rewritten_items))
        if finalized_items:
            items_by_book[current_book_id] = finalized_items
    total_rows = 0
    inserted_rows = 0
    updated_rows = 0
    skipped_rows = 0
    deleted_rows = 0
    changed_row_ids: list[int] = []
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(CHARACTERS_TABLE_SQL)
            if book_id is not None and int(book_id) not in items_by_book:
                cursor.execute("DELETE FROM `characters` WHERE book_id = %s", (int(book_id),))
            for current_book_id in sorted(items_by_book.keys()):
                sync_stats = _sync_book_characters(cursor, current_book_id, items_by_book[current_book_id])
                total_rows += int(sync_stats.get("rows", 0))
                inserted_rows += int(sync_stats.get("inserted", 0))
                updated_rows += int(sync_stats.get("updated", 0))
                skipped_rows += int(sync_stats.get("skipped", 0))
                deleted_rows += int(sync_stats.get("deleted", 0))
                changed_row_ids.extend(int(row_id) for row_id in sync_stats.get("changed_row_ids", []))
    review_stats = _run_dirty_character_review(book_id, changed_row_ids)
    return {
        "books": len(items_by_book),
        "rows": total_rows,
        "inserted_rows": inserted_rows,
        "changed_rows": updated_rows,
        "skipped_rows": skipped_rows,
        "deleted_rows": deleted_rows,
        "reviewed_rows": int(review_stats.get("reviewed_rows", 0)),
        "updated_rows": int(review_stats.get("updated_rows", 0)),
        "batches": int(review_stats.get("batches", 0)),
    }


def main() -> None:
    stats = rebuild_characters_table()
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
