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
from rag.entity_alias_cleanup import finalize_aliases_for_storage
from rag.prompt import CHARACTER_DIRTY_REVIEW_PROMPT

ENV_OVERRIDE_VAR = "STORY2MEMORY_ENV_OVERRIDE"
logger = logging.getLogger(__name__)
CHARACTERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `characters` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `book_id` INT NOT NULL,
    `name` VARCHAR(255) NOT NULL,
    `aliases` JSON NOT NULL,
    `records` JSON NOT NULL,
    `NEED_DELETE` ENUM('yes', 'no') NOT NULL DEFAULT 'yes'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""
CHARACTER_DIRTY_REVIEW_BATCH_SIZE = 50
CHARACTER_DIRTY_REVIEW_MAX_CONCURRENCY = 20
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
                    ADD COLUMN `NEED_DELETE` ENUM('yes', 'no') NOT NULL DEFAULT 'yes'
                    AFTER `records`
                    """
                )
                return
            if str(row.get("COLUMN_DEFAULT") or "").strip().lower() != "yes" or str(row.get("IS_NULLABLE") or "").strip().upper() != "NO":
                cursor.execute(
                    """
                    UPDATE `characters`
                    SET `NEED_DELETE` = 'yes'
                    WHERE `NEED_DELETE` IS NULL
                    """
                )
                cursor.execute(
                    """
                    ALTER TABLE `characters`
                    MODIFY COLUMN `NEED_DELETE` ENUM('yes', 'no') NOT NULL DEFAULT 'yes'
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
    for pattern in ALIAS_PREFIX_PATTERNS:
        text = pattern.sub("", text)
    for suffix in ALIAS_DE_SUFFIXES:
        text = re.sub(rf"^(.+?)的({re.escape(suffix)})$", r"\1\2", text)
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
    if not aliases:
        return None
    normalized_name = name or aliases[0]
    return {
        "name": normalized_name,
        "aliases": aliases,
        "records": _normalize_records(item.get("records")),
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
        aliases = finalize_aliases_for_storage(item["name"], item["aliases"])
        if not aliases:
            continue
        finalized.append(
            {
                "name": item["name"],
                "aliases": aliases,
                "records": item["records"],
            }
        )
    return finalized


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
    aliases = sorted({str(alias or "").strip() for alias in item.get("aliases") or [] if str(alias or "").strip()})
    payload = json.dumps(aliases, ensure_ascii=False, separators=(",", ":"))
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
                "NEED_DELETE": str(row.get("NEED_DELETE") or "").strip().lower() or "yes",
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
                    "yes",
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
                "yes",
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
    items_by_book = {
        current_book_id: _finalize_character_aliases(_merge_character_items(items))
        for current_book_id, items in raw_items_by_book.items()
    }
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
