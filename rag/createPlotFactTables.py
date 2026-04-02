from __future__ import annotations

import hashlib
import json
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

from rag.entity_alias_cleanup import finalize_aliases_for_storage

ENV_OVERRIDE_VAR = "STORY2MEMORY_ENV_OVERRIDE"
TABLE_DEFINITIONS: dict[str, str] = {
    "special_existences": """
        CREATE TABLE IF NOT EXISTS `special_existences` (
            `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
            `book_id` INT NOT NULL,
            `name` VARCHAR(255) NOT NULL,
            `aliases` JSON NOT NULL,
            `records` JSON NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    "origanizations": """
        CREATE TABLE IF NOT EXISTS `origanizations` (
            `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
            `book_id` INT NOT NULL,
            `name` VARCHAR(255) NOT NULL,
            `aliases` JSON NOT NULL,
            `records` JSON NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    "world_rules": """
        CREATE TABLE IF NOT EXISTS `world_rules` (
            `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
            `book_id` INT NOT NULL,
            `name` VARCHAR(255) NOT NULL,
            `aliases` JSON NOT NULL,
            `records` JSON NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
}
SOURCE_COLUMNS: dict[str, str] = {
    "special_existences": "special_existence",
    "origanizations": "origanizations",
    "world_rules": "world_rules",
}
BLOCKED_ALIASES_BY_TABLE: dict[str, set[str]] = {
    "special_existences": {
        "鬼",
        "厉鬼",
        "恶鬼",
        "怪物",
        "存在",
        "灵异存在",
        "疑似灵异存在",
        "老人",
        "照片",
        "尸体",
        "黑影",
        "影子",
    },
    "origanizations": {
        "组织",
        "公司",
        "势力",
        "阵营",
        "团队",
        "机构",
        "部门",
        "分部",
    },
    "world_rules": {
        "规则",
        "规律",
        "法则",
        "设定",
    },
}
BLOCKED_ALIAS_PATTERNS_BY_TABLE: dict[str, tuple[re.Pattern[str], ...]] = {
    "special_existences": (
        re.compile(
            r"^(?:那|那个|这|这个|一只|那只|这只|某个|某只|一种|一个)?"
            r"(?:鬼|厉鬼|恶鬼|怪物|存在|灵异存在|老人|照片|尸体|黑影|影子)$"
        ),
    ),
    "origanizations": (
        re.compile(r"^(?:某个|这个|那个|一家|一支|一个)?(?:组织|公司|势力|阵营|团队|机构|部门|分部)$"),
    ),
    "world_rules": (
        re.compile(r"^(?:这个|那个|该|这种|那种|一种)?(?:规则|规律|法则|设定)$"),
    ),
}
PREFIX_PATTERNS_BY_TABLE: dict[str, tuple[re.Pattern[str], ...]] = {
    "special_existences": (
        re.compile(r"^(?:那个|这个|那只|这只|一只|一个|一种|某个|某只)+"),
    ),
    "origanizations": (
        re.compile(r"^(?:那个|这个|某个|一家|一支|一个)+"),
    ),
    "world_rules": (
        re.compile(r"^(?:这个|那个|这种|那种|一种|该)+"),
    ),
}
DE_SUFFIXES_BY_TABLE: dict[str, tuple[str, ...]] = {
    "special_existences": (
        "鬼",
        "鬼婴",
        "鬼影",
        "影子",
        "尸体",
        "老人",
        "照片",
        "怪物",
    ),
    "origanizations": (
        "公司",
        "组织",
        "势力",
        "阵营",
        "团队",
        "机构",
        "部门",
        "分部",
        "负责人",
        "老板",
        "店长",
        "店老板",
        "集团",
        "俱乐部",
        "商会",
        "协会",
        "联盟",
    ),
    "world_rules": (
        "规则",
        "规律",
        "法则",
        "设定",
        "限制",
        "能力",
    ),
}


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


def _should_drop_alias(table_name: str, alias: str) -> bool:
    text = re.sub(r"\s+", "", str(alias or "").strip())
    if not text:
        return True
    if text in BLOCKED_ALIASES_BY_TABLE[table_name]:
        return True
    return any(pattern.match(text) for pattern in BLOCKED_ALIAS_PATTERNS_BY_TABLE[table_name])


def _filter_aliases_before_merge(table_name: str, aliases: list[str]) -> list[str]:
    return [alias for alias in aliases if not _should_drop_alias(table_name, alias)]


def _normalize_alias_text_before_merge(table_name: str, alias: str) -> str:
    text = re.sub(r"\s+", "", str(alias or "").strip())
    if not text:
        return ""
    for pattern in PREFIX_PATTERNS_BY_TABLE[table_name]:
        text = pattern.sub("", text)
    for suffix in DE_SUFFIXES_BY_TABLE[table_name]:
        text = re.sub(rf"^(.+?)的({re.escape(suffix)})$", r"\1\2", text)
    return text.strip()


def _normalize_aliases_before_merge(table_name: str, aliases: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        text = _normalize_alias_text_before_merge(table_name, alias)
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
        index_value = item[0]
        description = str(item[1] or "").strip()
        try:
            normalized_index = int(index_value)
        except (TypeError, ValueError):
            normalized_index = index_value
        normalized.append([normalized_index, description])
    return normalized


def _normalize_entry(table_name: str, item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    raw_name = str(item.get("name") or "").strip()
    split_name_parts = _split_slash_text(raw_name)
    name = split_name_parts[0] if split_name_parts else raw_name
    normalized_name = _normalize_alias_text_before_merge(table_name, name)
    aliases = _normalize_aliases_before_merge(
        table_name,
        _filter_aliases_before_merge(
            table_name,
            _normalize_aliases(name, item.get("aliases"), extra_aliases=split_name_parts[1:]),
        ),
    )
    if not aliases:
        return None
    normalized_name = normalized_name if normalized_name and normalized_name in aliases else aliases[0]
    return {
        "name": normalized_name,
        "aliases": aliases,
        "records": _normalize_records(item.get("records")),
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


def _merge_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
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


def _finalize_aliases_for_table(table_name: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if table_name != "origanizations":
        return items
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


def _load_plot_items(source_column: str, book_id: int | None = None) -> dict[int, list[dict[str, Any]]]:
    by_book: dict[int, list[dict[str, Any]]] = {}
    table_name = next(name for name, column in SOURCE_COLUMNS.items() if column == source_column)
    with _connect() as conn:
        with conn.cursor() as cursor:
            if book_id is None:
                cursor.execute(
                    f"""
                    SELECT book_id, plot_id, `{source_column}`
                    FROM book_plots
                    WHERE `{source_column}` IS NOT NULL
                    ORDER BY book_id ASC, plot_id ASC, id ASC
                    """
                )
            else:
                cursor.execute(
                    f"""
                    SELECT book_id, plot_id, `{source_column}`
                    FROM book_plots
                    WHERE book_id = %s
                      AND `{source_column}` IS NOT NULL
                    ORDER BY plot_id ASC, id ASC
                    """,
                    (int(book_id),),
                )
            rows = list(cursor.fetchall() or [])

    for row in rows:
        book_id = int(row.get("book_id") or 0)
        if book_id <= 0:
            continue
        items = _parse_json_list(row.get(source_column))
        if not items:
            continue
        bucket = by_book.setdefault(book_id, [])
        for raw_item in items:
            normalized_item = _normalize_entry(table_name, raw_item)
            if normalized_item is None:
                continue
            bucket.append(normalized_item)
    return by_book


def _serialize_item(item: dict[str, Any]) -> str:
    payload = {
        "name": str(item.get("name") or "").strip(),
        "aliases": [str(alias or "").strip() for alias in item.get("aliases") or [] if str(alias or "").strip()],
        "records": _normalize_records(item.get("records")),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _build_item_key(item: dict[str, Any]) -> str:
    aliases = sorted({str(alias or "").strip() for alias in item.get("aliases") or [] if str(alias or "").strip()})
    payload = json.dumps(aliases, ensure_ascii=False, separators=(",", ":"))
    return _hash_text(payload)


def _load_existing_rows(table_name: str, book_id: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT id, book_id, name, aliases, records
                FROM `{table_name}`
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
            }
        )
    return [row for row in normalized_rows if row["id"] > 0]


def _sync_book_rows(cursor: Any, table_name: str, book_id: int, items: list[dict[str, Any]]) -> dict[str, int]:
    existing_rows = _load_existing_rows(table_name, book_id)
    existing_by_key = {
        _build_item_key(row): row
        for row in existing_rows
    }
    matched_existing_ids: set[int] = set()
    inserted = 0
    updated = 0
    skipped = 0
    for item in items:
        item_key = _build_item_key(item)
        item_hash = _hash_text(_serialize_item(item))
        existing_row = existing_by_key.get(item_key)
        if existing_row is None:
            cursor.execute(
                f"""
                INSERT INTO `{table_name}` (`book_id`, `name`, `aliases`, `records`)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    int(book_id),
                    item["name"],
                    json.dumps(item["aliases"], ensure_ascii=False),
                    json.dumps(item["records"], ensure_ascii=False),
                ),
            )
            inserted += 1
            continue

        matched_existing_ids.add(int(existing_row["id"]))
        existing_hash = _hash_text(_serialize_item(existing_row))
        if existing_hash == item_hash:
            skipped += 1
            continue
        cursor.execute(
            f"""
            UPDATE `{table_name}`
            SET `name` = %s,
                `aliases` = %s,
                `records` = %s
            WHERE id = %s
            """,
            (
                item["name"],
                json.dumps(item["aliases"], ensure_ascii=False),
                json.dumps(item["records"], ensure_ascii=False),
                int(existing_row["id"]),
            ),
        )
        updated += 1

    stale_ids = [int(row["id"]) for row in existing_rows if int(row["id"]) not in matched_existing_ids]
    deleted = 0
    if stale_ids:
        placeholders = ", ".join(["%s"] * len(stale_ids))
        cursor.execute(
            f"DELETE FROM `{table_name}` WHERE id IN ({placeholders})",
            stale_ids,
        )
        deleted = int(cursor.rowcount or 0)
    return {
        "rows": len(items),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "deleted": deleted,
    }


def rebuild_fact_table(table_name: str, source_column: str, book_id: int | None = None) -> dict[str, int]:
    raw_items_by_book = _load_plot_items(source_column, book_id)
    items_by_book = {
        current_book_id: _finalize_aliases_for_table(table_name, _merge_items(items))
        for current_book_id, items in raw_items_by_book.items()
    }
    total_rows = 0
    inserted_rows = 0
    updated_rows = 0
    skipped_rows = 0
    deleted_rows = 0
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(TABLE_DEFINITIONS[table_name])
            if book_id is not None and int(book_id) not in items_by_book:
                cursor.execute(f"DELETE FROM `{table_name}` WHERE book_id = %s", (int(book_id),))
            for current_book_id in sorted(items_by_book.keys()):
                sync_stats = _sync_book_rows(cursor, table_name, current_book_id, items_by_book[current_book_id])
                total_rows += int(sync_stats.get("rows", 0))
                inserted_rows += int(sync_stats.get("inserted", 0))
                updated_rows += int(sync_stats.get("updated", 0))
                skipped_rows += int(sync_stats.get("skipped", 0))
                deleted_rows += int(sync_stats.get("deleted", 0))
    return {
        "books": len(items_by_book),
        "rows": total_rows,
        "inserted_rows": inserted_rows,
        "changed_rows": updated_rows,
        "skipped_rows": skipped_rows,
        "deleted_rows": deleted_rows,
    }


def rebuild_all_fact_tables(book_id: int | None = None) -> dict[str, dict[str, int]]:
    return {
        table_name: rebuild_fact_table(table_name, source_column, book_id)
        for table_name, source_column in SOURCE_COLUMNS.items()
    }


def main() -> None:
    stats = rebuild_all_fact_tables()
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
