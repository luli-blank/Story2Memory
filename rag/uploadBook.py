from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv
import pymysql

from agent.graph import build_llm
from database.mysql_client import MySQLChatStore
from database.session_keys import build_cosplay_session_info, build_legacy_qa_session_info, build_qa_session_info
from langchain_core.messages import AIMessage
from rag.bookSlice import slice_book_by_chapter
from rag.epub_parser import parse_epub_book
from rag.chatpersToPlots import PlotSegmentationEngine
from rag.createPlots import PlotRecordBuilder
from rag.prompt import CHAPTER_SUMMARY_PROMPT

ROOT_DIR = Path(__file__).resolve().parents[1]
BOOK_DIR = ROOT_DIR / "data" / "book"
PICTURE_DIR = ROOT_DIR / "data" / "picture"
SCHEMA_SQL = ROOT_DIR / "database" / "mysql" / "create_tables.sql"
DEDUP_ENV_VAR = "BOOK_UPLOAD_DEDUP_BY_TITLE"
CHAPTER_SUMMARY_MAX_WORKERS_ENV_VAR = "CHAPTER_SUMMARY_MAX_WORKERS"
BOOK_CHAPTER_SUMMARY_IF_EXISTS_ENV_VAR = "BOOK_CHAPTER_SUMMARY_IF_EXISTS"
logger = logging.getLogger(__name__)
LEGACY_FACT_TABLES = (
    "plot_entity_deltas",
    "plot_facts",
    "plot_interactions",
    "plot_entity_presence",
    "chapter_entity_mentions",
    "book_entity_aliases",
    "book_entities",
    "book_assets",
)
CHAPTER_SUMMARY_PRIMARY_MODEL = "deepseek-v3.2"
CHAPTER_SUMMARY_FALLBACK_MODEL = "doubao-seed-2.0-pro"
CHAPTER_SUMMARY_PRIMARY_RETRY_COUNT = 2
CHAPTER_SUMMARY_FALLBACK_RETRY_COUNT = 2
NON_NARRATIVE_SENTINEL = "非小说片段：无实质性叙事内容"
BOOK_DELETE_TABLES = (
    "character_relation_volume_groups",
    "character_relation_chunks",
    "character_relations",
    "character_profile_volume_groups",
    "character_profile_chunks",
    "character_profiles",
    "character_profile_jobs",
    "world_rules",
    "origanizations",
    "special_existences",
    "characters",
    "book_volumes",
    "book_plots",
    "book_chapters",
)


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


def _ensure_schema(conn) -> None:
    if not SCHEMA_SQL.exists():
        raise RuntimeError(f"Schema file not found: {SCHEMA_SQL}")
    schema_sql = SCHEMA_SQL.read_text(encoding="utf-8")
    statements = [statement.strip() for statement in schema_sql.split(";") if statement.strip()]
    with conn.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
        cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
        try:
            for table_name in LEGACY_FACT_TABLES:
                cursor.execute(f"DROP TABLE IF EXISTS `{table_name}`")
        finally:
            cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
        cursor.execute(
            """
            SELECT DATA_TYPE
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'book_chapters'
              AND COLUMN_NAME = 'character'
            LIMIT 1
            """
        )
        row = cursor.fetchone() or {}
        data_type = str(row.get("DATA_TYPE") or "").strip().lower()
        if not data_type:
            logger.warning("[schema] Auto-migrating book_chapters: add `character` column.")
            cursor.execute(
                """
                ALTER TABLE `book_chapters`
                ADD COLUMN `character` LONGTEXT COMMENT '章节级角色信息（名称 + 行为/语言概括）'
                AFTER `chapter_summary`
                """
            )
        elif data_type == "json":
            logger.warning("[schema] Auto-migrating book_chapters: convert `character` JSON -> LONGTEXT.")
            cursor.execute(
                """
                ALTER TABLE `book_chapters`
                MODIFY COLUMN `character` LONGTEXT COMMENT '章节级角色信息（名称 + 行为/语言概括）'
                """
            )
        chapter_extra_columns: tuple[tuple[str, str], ...] = (
            (
                "status",
                "ALTER TABLE `book_chapters` ADD COLUMN `status` ENUM('pending', 'success', 'error') DEFAULT NULL COMMENT '章节信息提取状态' AFTER `title`",
            ),
            (
                "special_existence",
                "ALTER TABLE `book_chapters` ADD COLUMN `special_existence` LONGTEXT COMMENT '章节级特殊存在/特殊物品信息（名称 + 一句话描述）' AFTER `character`",
            ),
            (
                "origanizations",
                "ALTER TABLE `book_chapters` ADD COLUMN `origanizations` LONGTEXT COMMENT '章节级组织/势力信息（名称 + 一句话概括）' AFTER `special_existence`",
            ),
            (
                "world_rules",
                "ALTER TABLE `book_chapters` ADD COLUMN `world_rules` LONGTEXT COMMENT '章节级世界规则/设定/限制信息' AFTER `origanizations`",
            ),
        )
        for column_name, ddl in chapter_extra_columns:
            cursor.execute(
                """
                SELECT 1
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'book_chapters'
                  AND COLUMN_NAME = %s
                LIMIT 1
                """,
                (column_name,),
            )
            if not cursor.fetchone():
                logger.warning("[schema] Auto-migrating book_chapters: add `%s` column.", column_name)
                cursor.execute(ddl)
        cursor.execute(
            """
            SELECT 1
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'books'
              AND COLUMN_NAME = 'source_format'
            LIMIT 1
            """
        )
        if not cursor.fetchone():
            logger.warning("[schema] Auto-migrating books: add `source_format` column.")
            cursor.execute(
                """
                ALTER TABLE `books`
                ADD COLUMN `source_format` ENUM('txt', 'epub') DEFAULT 'txt' COMMENT '原始来源格式'
                AFTER `author`
                """
            )
        cursor.execute(
            """
            SELECT 1
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'books'
              AND COLUMN_NAME = 'cover_asset_id'
            LIMIT 1
            """
        )
        if not cursor.fetchone():
            logger.warning("[schema] Auto-migrating books: add `cover_asset_id` column.")
            cursor.execute(
                """
                ALTER TABLE `books`
                ADD COLUMN `cover_asset_id` BIGINT DEFAULT NULL COMMENT '封面资源ID（已废弃）'
                AFTER `cover_url`
                """
            )
        cursor.execute(
            """
            SELECT 1
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'book_chapters'
              AND COLUMN_NAME = 'raw_summary_json'
            LIMIT 1
            """
        )
        if not cursor.fetchone():
            logger.warning("[schema] Auto-migrating book_chapters: add `raw_summary_json` column.")
            cursor.execute(
                """
                ALTER TABLE `book_chapters`
                ADD COLUMN `raw_summary_json` JSON DEFAULT NULL COMMENT '章节摘要模型原始输出'
                AFTER `world_rules`
                """
            )
        plot_extra_columns: tuple[tuple[str, str], ...] = (
            (
                "status",
                "ALTER TABLE `book_plots` ADD COLUMN `status` ENUM('pending', 'success', 'error') DEFAULT NULL COMMENT '情节信息提取状态' AFTER `title`",
            ),
            (
                "character",
                "ALTER TABLE `book_plots` ADD COLUMN `character` JSON DEFAULT NULL COMMENT '情节级角色聚合信息' AFTER `plot_summary`",
            ),
            (
                "special_existence",
                "ALTER TABLE `book_plots` ADD COLUMN `special_existence` JSON DEFAULT NULL COMMENT '情节级特殊存在聚合信息' AFTER `character`",
            ),
            (
                "origanizations",
                "ALTER TABLE `book_plots` ADD COLUMN `origanizations` JSON DEFAULT NULL COMMENT '情节级组织/势力聚合信息' AFTER `special_existence`",
            ),
            (
                "world_rules",
                "ALTER TABLE `book_plots` ADD COLUMN `world_rules` JSON DEFAULT NULL COMMENT '情节级世界规则聚合信息' AFTER `origanizations`",
            ),
        )
        for column_name, ddl in plot_extra_columns:
            cursor.execute(
                """
                SELECT 1
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'book_plots'
                  AND COLUMN_NAME = %s
                LIMIT 1
                """,
                (column_name,),
            )
            if not cursor.fetchone():
                logger.warning("[schema] Auto-migrating book_plots: add `%s` column.", column_name)
                cursor.execute(ddl)
        removed_plot_columns = (
            "search_content",
            "metadata",
            "step1_status",
            "step1_retry_count",
            "step1_last_error",
            "step1_next_retry_at",
            "step2_status",
            "step2_retry_count",
            "step2_last_error",
            "step2_next_retry_at",
            "raw_plot_step1_json",
            "raw_plot_step2_json",
            "raw_plot_json",
            "vector_id",
        )
        for column_name in removed_plot_columns:
            cursor.execute(
                """
                SELECT 1
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'book_plots'
                  AND COLUMN_NAME = %s
                LIMIT 1
                """,
                (column_name,),
            )
            if cursor.fetchone():
                logger.warning("[schema] Auto-migrating book_plots: drop `%s` column.", column_name)
                cursor.execute(f"ALTER TABLE `book_plots` DROP COLUMN `{column_name}`")
        removed_volume_columns = (
            "search_content",
            "metadata",
        )
        for column_name in removed_volume_columns:
            cursor.execute(
                """
                SELECT 1
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'book_volumes'
                  AND COLUMN_NAME = %s
                LIMIT 1
                """,
                (column_name,),
            )
            if cursor.fetchone():
                logger.warning("[schema] Auto-migrating book_volumes: drop `%s` column.", column_name)
                cursor.execute(f"ALTER TABLE `book_volumes` DROP COLUMN `{column_name}`")
        cursor.execute(
            """
            SELECT 1
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'book_volumes'
              AND COLUMN_NAME = 'raw_volume_json'
            LIMIT 1
            """
        )
        if not cursor.fetchone():
            logger.warning("[schema] Auto-migrating book_volumes: add `raw_volume_json` column.")
            cursor.execute(
                """
                ALTER TABLE `book_volumes`
                ADD COLUMN `raw_volume_json` JSON DEFAULT NULL COMMENT '卷摘要模型原始输出'
                AFTER `plot_count`
                """
            )
        cursor.execute(
            """
            UPDATE `book_chapters`
            SET `status` = CASE
                WHEN LOWER(COALESCE(`title`, '')) = 'error' THEN 'error'
                WHEN COALESCE(`chapter_summary`, '') <> ''
                     AND `raw_summary_json` IS NOT NULL
                     AND COALESCE(`character`, '') IS NOT NULL
                THEN 'success'
                ELSE `status`
            END
            WHERE `status` IS NULL
            """
        )
        cursor.execute(
            """
            UPDATE `book_plots`
            SET `status` = CASE
                WHEN COALESCE(`title`, '') <> '' AND COALESCE(`plot_summary`, '') <> '' THEN 'success'
                ELSE `status`
            END
            WHERE `status` IS NULL
            """
        )


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _count_words(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def _is_enabled(env_name: str, default: bool = False) -> bool:
    raw = os.getenv(env_name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_summary_max_workers(default: int = 4) -> int:
    raw = str(os.getenv(CHAPTER_SUMMARY_MAX_WORKERS_ENV_VAR, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "[analysis] Invalid %s=%s, fallback=%d",
            CHAPTER_SUMMARY_MAX_WORKERS_ENV_VAR,
            raw,
            default,
        )
        return default
    return min(25, max(1, value))


def _resolve_chapter_summary_if_exists_mode() -> str:
    raw_mode = str(os.getenv(BOOK_CHAPTER_SUMMARY_IF_EXISTS_ENV_VAR, "skip")).strip().lower()
    if raw_mode in {"overwrite", "rebuild", "regen"}:
        return "overwrite"
    return "skip"


def _make_unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("Cannot allocate unique file path for uploaded book.")


def _safe_file_stem(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", str(name).strip())
    return cleaned.strip(" ._") or "book"


def _next_id(cursor, table_name: str) -> int:
    if table_name not in {"books", "book_chapters"}:
        raise ValueError(f"Unsupported table name: {table_name}")
    cursor.execute(f"SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM `{table_name}`")
    row = cursor.fetchone() or {}
    return int(row.get("next_id") or 1)


def _stringify_content(content: object) -> str:
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


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    payload = raw.strip()
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


def _empty_chapter_summary_payload() -> dict[str, Any]:
    return {
        "chapter_summary": "",
        "character": [],
        "special_existence": [],
        "organizations": [],
        "world_rules": [],
    }


def _normalize_name_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        name = str(item or "").strip()
        if not name or name in seen:
            continue
        normalized.append(name)
        seen.add(name)
    return normalized


def _pick_first_text(source: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = source.get(key)
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _normalize_named_description_list(value: Any) -> list[dict[str, str]]:
    source = value
    if isinstance(source, dict):
        source = [{"name": key, "description": item} for key, item in source.items()]
    elif isinstance(source, str):
        source = [source]
    if not isinstance(source, list):
        return []

    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in source:
        if isinstance(item, dict):
            name = _pick_first_text(item, ("name", "title", "entity", "item", "organization", "organization_name"))
            description = _pick_first_text(
                item,
                ("description", "summary", "info", "content", "detail", "note", "text"),
            )
        else:
            name = str(item or "").strip()
            description = ""
        if not name and not description:
            continue
        signature = (name, description)
        if signature in seen:
            continue
        normalized.append({"name": name, "description": description})
        seen.add(signature)
    return normalized


def _validate_named_description_list(raw_value: Any, field_name: str) -> tuple[bool, str]:
    if not isinstance(raw_value, list):
        return False, f"{field_name} is not a list"

    for index, item in enumerate(raw_value):
        if not isinstance(item, dict):
            return False, f"{field_name}[{index}] is not an object"
        name = _pick_first_text(item, ("name", "title", "entity", "item", "organization", "organization_name", "rule", "label"))
        description = _pick_first_text(
            item,
            ("description", "summary", "info", "content", "detail", "note", "text"),
        )
        if not name:
            return False, f"{field_name}[{index}] missing name"
        if not description:
            return False, f"{field_name}[{index}] missing description"
    return True, ""


def _join_named_description_lines(items: list[dict[str, str]]) -> str | None:
    lines: list[str] = []
    for item in items:
        name = str(item.get("name") or "").strip()
        description = str(item.get("description") or "").strip()
        if name and description:
            lines.append(f"{name}：{description}")
        elif name:
            lines.append(name)
        elif description:
            lines.append(description)
    return "\n".join(lines) if lines else None


def _normalize_chapter_summary_payload(raw_obj: dict[str, Any] | None, fallback_summary: str = "") -> dict[str, Any]:
    source = raw_obj if isinstance(raw_obj, dict) else {}
    payload = _empty_chapter_summary_payload()
    payload["chapter_summary"] = str(source.get("chapter_summary") or fallback_summary or "").strip()
    payload["character"] = _normalize_named_description_list(source.get("character") or source.get("characters"))
    payload["special_existence"] = _normalize_named_description_list(
        source.get("special_existence") or source.get("special_existences") or source.get("special_items")
    )
    payload["organizations"] = _normalize_named_description_list(
        source.get("organizations") if source.get("organizations") is not None else source.get("origanizations")
    )
    payload["world_rules"] = _normalize_named_description_list(source.get("world_rules"))
    return payload


def _is_valid_chapter_summary_payload(raw_obj: Any) -> tuple[bool, str]:
    if not isinstance(raw_obj, dict):
        return False, "payload is not a JSON object"

    chapter_summary = raw_obj.get("chapter_summary")
    if not isinstance(chapter_summary, str) or not chapter_summary.strip():
        return False, "missing chapter_summary"

    normalized_fields = (
        ("character", raw_obj.get("character") if raw_obj.get("character") is not None else raw_obj.get("characters")),
        ("special_existence", raw_obj.get("special_existence") if raw_obj.get("special_existence") is not None else raw_obj.get("special_existences")),
        ("world_rules", raw_obj.get("world_rules")),
    )
    for field_name, raw_value in normalized_fields:
        is_valid, reason = _validate_named_description_list(raw_value, field_name)
        if not is_valid:
            return False, reason

    organizations_value = raw_obj.get("organizations")
    if organizations_value is None:
        organizations_value = raw_obj.get("origanizations")
    is_valid, reason = _validate_named_description_list(organizations_value, "organizations")
    if not is_valid:
        return False, reason

    return True, ""


def _is_valid_chapter_summary_raw(raw_text: str) -> tuple[bool, str]:
    text = str(raw_text or "").strip()
    if not text:
        return False, "empty response"
    if text == NON_NARRATIVE_SENTINEL:
        return True, ""
    obj = _extract_json_object(text)
    if obj is None:
        return False, "response is not valid JSON"
    return _is_valid_chapter_summary_payload(obj)


def _parse_chapter_summary_result(raw: str) -> tuple[str, str | None, str | None, str | None, str | None, str]:
    text = str(raw or "").strip()
    if not text:
        empty_payload = _empty_chapter_summary_payload()
        return "", None, None, None, None, json.dumps(empty_payload, ensure_ascii=False)

    obj = _extract_json_object(text)
    normalized_payload = _normalize_chapter_summary_payload(obj, fallback_summary=text)
    chapter_summary = str(normalized_payload.get("chapter_summary") or "").strip() or text
    character_text = _join_named_description_lines(normalized_payload["character"]) or ""
    special_existence_text = _join_named_description_lines(normalized_payload["special_existence"])
    organizations_text = _join_named_description_lines(normalized_payload["organizations"])
    world_rules_text = _join_named_description_lines(normalized_payload["world_rules"])
    normalized_payload["chapter_summary"] = chapter_summary
    return (
        chapter_summary,
        character_text,
        special_existence_text,
        organizations_text,
        world_rules_text,
        json.dumps(normalized_payload, ensure_ascii=False),
    )


def _invoke_chapter_summary_once(chapter_text: str, model_name: str) -> str:
    llm = build_llm(model_name)
    prompt = CHAPTER_SUMMARY_PROMPT.replace("{{text}}", chapter_text)
    result = llm.invoke(prompt)
    if isinstance(result, AIMessage):
        return _stringify_content(result.content)
    return _stringify_content(getattr(result, "content", result))


def _generate_chapter_summary_raw(chapter_text: str) -> str:
    attempt_plan = (
        [CHAPTER_SUMMARY_PRIMARY_MODEL] * (1 + CHAPTER_SUMMARY_PRIMARY_RETRY_COUNT)
        + [CHAPTER_SUMMARY_FALLBACK_MODEL] * CHAPTER_SUMMARY_FALLBACK_RETRY_COUNT
    )
    last_error = "unknown error"
    for attempt_index, model_name in enumerate(attempt_plan, start=1):
        try:
            raw_text = _invoke_chapter_summary_once(chapter_text, model_name)
        except Exception as exc:
            last_error = f"invoke_failed model={model_name} error={exc}"
            logger.warning(
                "[chapter_summary] attempt=%d/%d model=%s invoke failed: %s",
                attempt_index,
                len(attempt_plan),
                model_name,
                exc,
            )
            continue

        is_valid, reason = _is_valid_chapter_summary_raw(raw_text)
        if is_valid:
            return raw_text

        last_error = f"invalid_format model={model_name} reason={reason} raw_preview={raw_text[:300]}"
        logger.warning(
            "[chapter_summary] attempt=%d/%d model=%s invalid format: %s",
            attempt_index,
            len(attempt_plan),
            model_name,
            reason,
        )

    raise RuntimeError(f"chapter_summary_retry_exhausted: {last_error}")


def _chapter_row_has_valid_summary(row: dict[str, Any]) -> bool:
    if str(row.get("status") or "").strip().lower() != "success":
        return False
    if row.get("chapter_summary") is None or row.get("character") is None or row.get("raw_summary_json") is None:
        return False

    raw_summary_json = row.get("raw_summary_json")
    if isinstance(raw_summary_json, dict):
        payload = raw_summary_json
    else:
        payload = _extract_json_object(str(raw_summary_json or ""))
    is_valid, _ = _is_valid_chapter_summary_payload(payload)
    return is_valid


def _resolve_chapter_title_for_update(row: dict[str, Any]) -> str:
    current_title = str(row.get("title") or "").strip()
    if current_title and current_title.lower() != "error":
        return current_title

    raw_summary_json = row.get("raw_summary_json")
    payload = raw_summary_json if isinstance(raw_summary_json, dict) else _extract_json_object(str(raw_summary_json or ""))
    original_title = str((payload or {}).get("original_title") or "").strip()
    return original_title or current_title


def _guess_mime_type(suffix: str) -> str | None:
    mapping = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
    }
    return mapping.get(str(suffix or "").lower())


def _save_cover_image(book_name: str, cover_bytes: bytes | None, cover_extension: str | None) -> dict[str, str] | None:
    if not cover_bytes:
        return None
    suffix = str(cover_extension or ".jpg").strip() or ".jpg"
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    PICTURE_DIR.mkdir(parents=True, exist_ok=True)
    cover_path = _make_unique_path(PICTURE_DIR / f"{_safe_file_stem(book_name)}{suffix}")
    cover_path.write_bytes(cover_bytes)
    return {
        "public_url": f"/covers/{cover_path.name}",
        "storage_path": str(cover_path),
        "mime_type": _guess_mime_type(suffix) or "",
    }


def _persist_uploaded_book(
    *,
    target_path: Path,
    title: str,
    author: str,
    source_format: str,
    description: str | None,
    cover_url: str | None,
    cover_asset: dict[str, str] | None,
    chapters: list[dict[str, Any]],
    total_words: int,
) -> dict[str, Any]:
    dedup_enabled = _is_enabled(DEDUP_ENV_VAR, default=False)
    with _connect() as conn:
        _ensure_schema(conn)
        with conn.cursor() as cursor:
            book_id = 0
            if dedup_enabled:
                cursor.execute(
                    "SELECT id FROM books WHERE title = %s ORDER BY id DESC LIMIT 1",
                    (title,),
                )
                existing = cursor.fetchone()
                if existing:
                    book_id = int(existing["id"])
                    cursor.execute(
                        """
                        UPDATE books
                        SET author = %s,
                            source_format = %s,
                            cover_url = %s,
                            cover_asset_id = %s,
                            description = %s,
                            total_chapters = %s,
                            total_words = %s,
                            status = %s,
                            file_path = %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                        """,
                        (
                            author or "未知",
                            source_format,
                            cover_url,
                            None,
                            description,
                            len(chapters),
                            total_words,
                            "pending",
                            str(target_path),
                            book_id,
                        ),
                    )
                    cursor.execute("DELETE FROM book_volumes WHERE book_id = %s", (book_id,))
                    cursor.execute("DELETE FROM book_plots WHERE book_id = %s", (book_id,))
                    cursor.execute("DELETE FROM book_chapters WHERE book_id = %s", (book_id,))

            if not book_id:
                book_id = _next_id(cursor, "books")
                cursor.execute(
                    """
                    INSERT INTO books
                    (id, title, author, source_format, cover_url, description, total_chapters, total_words, status, file_path)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        book_id,
                        title,
                        author or "未知",
                        source_format,
                        cover_url,
                        description,
                        len(chapters),
                        total_words,
                        "pending",
                        str(target_path),
                    ),
                )

            next_chapter_id = _next_id(cursor, "book_chapters")
            for chapter in chapters:
                cursor.execute(
                    """
                    INSERT INTO book_chapters
                    (id, book_id, chapter_index, title, content, word_count, plot_id, volume_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        next_chapter_id,
                        book_id,
                        int(chapter["chapter_index"]),
                        str(chapter["title"]),
                        str(chapter["content"]),
                        int(chapter["word_count"]),
                        0,
                        0,
                    ),
                )
                next_chapter_id += 1

            cursor.execute(
                """
                UPDATE books
                SET status = %s, total_chapters = %s, total_words = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                ("pending", len(chapters), total_words, book_id),
            )

    return {
        "id": book_id,
        "title": title,
        "author": author or "未知",
        "cover_url": cover_url,
        "total_chapters": len(chapters),
        "total_words": total_words,
        "status": "pending",
        "file_path": str(target_path),
    }


def save_uploaded_txt_book(file_name: str, file_bytes: bytes) -> dict[str, Any]:
    name = Path(file_name).name
    if Path(name).suffix.lower() != ".txt":
        raise ValueError("Only .txt files are supported.")

    BOOK_DIR.mkdir(parents=True, exist_ok=True)
    target_path = _make_unique_path(BOOK_DIR / name)
    target_path.write_bytes(file_bytes)

    title = Path(name).stem.strip() or "未命名书籍"
    text = _decode_text(file_bytes)
    chapters = slice_book_by_chapter(text)
    total_words = _count_words(text)
    return _persist_uploaded_book(
        target_path=target_path,
        title=title,
        author="未知",
        source_format="txt",
        description=None,
        cover_url=None,
        cover_asset=None,
        chapters=chapters,
        total_words=total_words,
    )


def save_uploaded_epub_book(file_name: str, file_bytes: bytes) -> dict[str, Any]:
    name = Path(file_name).name
    if Path(name).suffix.lower() != ".epub":
        raise ValueError("Only .epub files are supported.")

    BOOK_DIR.mkdir(parents=True, exist_ok=True)
    target_path = _make_unique_path(BOOK_DIR / name)
    target_path.write_bytes(file_bytes)

    parsed = parse_epub_book(target_path)
    cover_asset = _save_cover_image(parsed.title or Path(name).stem, parsed.cover_bytes, parsed.cover_extension)
    return _persist_uploaded_book(
        target_path=target_path,
        title=parsed.title,
        author=parsed.author,
        source_format="epub",
        description=parsed.description,
        cover_url=cover_asset["public_url"] if cover_asset else None,
        cover_asset=cover_asset,
        chapters=parsed.chapters,
        total_words=parsed.total_words,
    )


def save_uploaded_book(file_name: str, file_bytes: bytes) -> dict[str, Any]:
    suffix = Path(file_name).suffix.lower()
    if suffix == ".txt":
        return save_uploaded_txt_book(file_name, file_bytes)
    if suffix == ".epub":
        return save_uploaded_epub_book(file_name, file_bytes)
    raise ValueError("Only .txt and .epub files are supported.")


def list_books() -> list[dict[str, Any]]:
    with _connect() as conn:
        _ensure_schema(conn)
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, title, author, cover_url, total_chapters, total_words, status
                FROM books
                ORDER BY updated_at DESC, id DESC
                """
            )
            return list(cursor.fetchall() or [])


def _managed_cover_path(cover_url: str) -> Path | None:
    raw = str(cover_url or "").strip()
    if not raw.startswith("/covers/"):
        return None
    candidate = (PICTURE_DIR / raw.split("/covers/", 1)[1]).resolve()
    picture_root = PICTURE_DIR.resolve()
    if picture_root == candidate or picture_root in candidate.parents:
        return candidate
    return None


def _managed_book_path(file_path: str) -> Path | None:
    raw = str(file_path or "").strip()
    if not raw:
        return None
    candidate = Path(raw).resolve()
    book_root = BOOK_DIR.resolve()
    if book_root == candidate or book_root in candidate.parents:
        return candidate
    return None


def _remove_managed_path(path: Path | None) -> bool:
    if path is None or not path.exists() or not path.is_file():
        return False
    path.unlink(missing_ok=True)
    return True


def delete_book_cascade(book_id: int) -> dict[str, Any]:
    normalized_book_id = int(book_id or 0)
    if normalized_book_id <= 0:
        raise ValueError("book_id must be positive integer.")

    with _connect() as conn:
        _ensure_schema(conn)
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, title, file_path, cover_url
                FROM books
                WHERE id = %s
                LIMIT 1
                """,
                (normalized_book_id,),
            )
            book_row = cursor.fetchone() or {}
            if not book_row:
                return {"book_id": normalized_book_id, "deleted": 0, "reason": "not_found"}
            cursor.execute(
                """
                SELECT id, name
                FROM characters
                WHERE book_id = %s
                ORDER BY id ASC
                """,
                (normalized_book_id,),
            )
            character_rows = list(cursor.fetchall() or [])
            cursor.execute(
                """
                SELECT COUNT(*) AS title_count
                FROM books
                WHERE title = %s
                """,
                (str(book_row.get("title") or "").strip(),),
            )
            title_count_row = cursor.fetchone() or {}

    title = str(book_row.get("title") or "").strip()
    title_count = int(title_count_row.get("title_count") or 0)
    store = MySQLChatStore()
    deleted_sessions = store.delete_sessions_for_book(normalized_book_id)
    fallback_session_ids: set[str] = set()
    current_qa_session_id, _, _ = build_qa_session_info(
        novel_title=title,
        book_id=normalized_book_id,
    )
    if current_qa_session_id != "0":
        fallback_session_ids.add(current_qa_session_id)
    if title_count == 1:
        legacy_qa_session_id, _, _ = build_legacy_qa_session_info(novel_title=title)
        if legacy_qa_session_id != "0":
            fallback_session_ids.add(legacy_qa_session_id)
    for row in character_rows:
        session_id, _, _ = build_cosplay_session_info(
            book_id=normalized_book_id,
            novel_title=title,
            character_id=int(row.get("id") or 0),
            character_name=str(row.get("name") or "").strip(),
        )
        if session_id != "0":
            fallback_session_ids.add(session_id)

    deleted_sessions += store.delete_sessions(sorted(fallback_session_ids))

    from database.qdrant_client import delete_book_embedding_collections
    from rag.entity_qdrant_sync import delete_entity_collections
    from relationGraph.sync import delete_book_relation_graph

    embedding_stats = delete_book_embedding_collections(normalized_book_id)
    entity_stats = delete_entity_collections(book_id=normalized_book_id)
    graph_stats = delete_book_relation_graph(normalized_book_id)

    mysql_deleted: dict[str, int] = {}
    with _connect() as conn:
        _ensure_schema(conn)
        with conn.cursor() as cursor:
            for table_name in BOOK_DELETE_TABLES:
                cursor.execute(f"DELETE FROM `{table_name}` WHERE book_id = %s", (normalized_book_id,))
                mysql_deleted[table_name] = int(cursor.rowcount or 0)
            cursor.execute("DELETE FROM books WHERE id = %s", (normalized_book_id,))
            mysql_deleted["books"] = int(cursor.rowcount or 0)

    source_deleted = _remove_managed_path(_managed_book_path(str(book_row.get("file_path") or "")))
    cover_deleted = _remove_managed_path(_managed_cover_path(str(book_row.get("cover_url") or "")))
    return {
        "book_id": normalized_book_id,
        "deleted": int(mysql_deleted.get("books", 0) or 0),
        "deleted_sessions": deleted_sessions,
        "mysql": mysql_deleted,
        "qdrant": embedding_stats,
        "entity_qdrant": entity_stats,
        "relation_graph": graph_stats,
        "source_deleted": source_deleted,
        "cover_deleted": cover_deleted,
    }


def recover_interrupted_book_statuses() -> int:
    """Reset stale processing status left by interrupted analysis runs."""
    with _connect() as conn:
        _ensure_schema(conn)
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE books
                SET status = %s, updated_at = CURRENT_TIMESTAMP
                WHERE status = %s
                """,
                ("pending", "processing"),
            )
            return int(cursor.rowcount or 0)


def summarize_book_chapters(title: str) -> dict[str, Any]:
    book_title = title.strip()
    if not book_title:
        raise ValueError("Book title is required.")

    book_id = 0
    updated_count = 0
    if_exists_mode = _resolve_chapter_summary_if_exists_mode()

    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id, title FROM books WHERE title = %s ORDER BY id DESC LIMIT 1",
                    (book_title,),
                )
                book = cursor.fetchone()
                if not book:
                    raise ValueError(f"Book not found: {book_title}")

                book_id = int(book["id"])
                cursor.execute(
                    "UPDATE books SET status = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                    ("processing", book_id),
                )
                cursor.execute(
                    """
                    SELECT id, title, status, content, chapter_summary, `character`, raw_summary_json
                    FROM book_chapters
                    WHERE book_id = %s
                    ORDER BY chapter_index ASC, id ASC
                    """,
                    (book_id,),
                )
                chapters = list(cursor.fetchall() or [])
                total_chapters = len(chapters)
                if if_exists_mode == "overwrite":
                    chapters_to_process = list(chapters)
                else:
                    chapters_to_process = [chapter for chapter in chapters if not _chapter_row_has_valid_summary(chapter)]
                has_all_summaries = total_chapters > 0 and not chapters_to_process
                logger.warning(
                    "[分析进度][book=%s] 章节摘要预检查：%s，策略=%s (%d章，待处理 %d 章)",
                    book_id,
                    "已存在，跳过生成" if has_all_summaries else "未完成，将开始生成",
                    if_exists_mode,
                    total_chapters,
                    len(chapters_to_process),
                )

                if not has_all_summaries:
                    pending_chapters = len(chapters_to_process)
                    logger.warning(
                        "[分析进度][章节摘要][book=%s] %s %d/%d",
                        book_id,
                        _progress_bar(0, pending_chapters),
                        0,
                        pending_chapters,
                    )
                    max_workers = _resolve_summary_max_workers(default=4)
                    logger.warning(
                        "[分析进度][章节摘要][book=%s] 并发生成启动，max_workers=%d",
                        book_id,
                        max_workers,
                    )
                    futures: dict[Future[str], dict[str, Any]] = {}
                    completed_count = 0
                    failed_chapter_ids: list[int] = []
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        for chapter in chapters_to_process:
                            cursor.execute(
                                "UPDATE book_chapters SET status = %s WHERE id = %s",
                                ("pending", int(chapter["id"])),
                            )
                            chapter_text = str(chapter.get("content") or "")
                            future = executor.submit(_generate_chapter_summary_raw, chapter_text)
                            futures[future] = chapter

                        for future in as_completed(futures):
                            chapter_row = futures[future]
                            chapter_id = int(chapter_row["id"])
                            chapter_title = _resolve_chapter_title_for_update(chapter_row)
                            try:
                                summary = future.result()
                            except Exception as exc:
                                failed_chapter_ids.append(chapter_id)
                                cursor.execute(
                                    """
                                    UPDATE book_chapters
                                    SET title = %s,
                                        status = %s,
                                        chapter_summary = %s,
                                        `character` = %s,
                                        special_existence = %s,
                                        origanizations = %s,
                                        world_rules = %s,
                                        raw_summary_json = %s
                                    WHERE id = %s
                                    """,
                                    (
                                        chapter_title or None,
                                        "error",
                                        None,
                                        None,
                                        None,
                                        None,
                                        None,
                                        json.dumps(
                                            {
                                                "status": "error",
                                                "original_title": chapter_title,
                                                "error": str(exc),
                                            },
                                            ensure_ascii=False,
                                        ),
                                        chapter_id,
                                    ),
                                )
                                completed_count += 1
                                logger.warning(
                                    "[分析进度][章节摘要][book=%s] %s %d/%d (failed=%d)",
                                    book_id,
                                    _progress_bar(completed_count, pending_chapters),
                                    completed_count,
                                    pending_chapters,
                                    len(failed_chapter_ids),
                                )
                                continue
                            (
                                chapter_summary,
                                character_text,
                                special_existence_text,
                                organizations_text,
                                world_rules_text,
                                raw_summary_json,
                            ) = _parse_chapter_summary_result(summary)
                            cursor.execute(
                                """
                                UPDATE book_chapters
                                SET title = %s,
                                    status = %s,
                                    chapter_summary = %s,
                                    `character` = %s,
                                    special_existence = %s,
                                    origanizations = %s,
                                    world_rules = %s,
                                    raw_summary_json = %s
                                WHERE id = %s
                                """,
                                (
                                    chapter_title or None,
                                    "success",
                                    chapter_summary or None,
                                    character_text,
                                    special_existence_text,
                                    organizations_text,
                                    world_rules_text,
                                    raw_summary_json,
                                    chapter_id,
                                ),
                            )
                            updated_count += 1
                            completed_count += 1
                            logger.warning(
                                "[分析进度][章节摘要][book=%s] %s %d/%d",
                                book_id,
                                _progress_bar(completed_count, pending_chapters),
                                completed_count,
                                pending_chapters,
                            )

                    if failed_chapter_ids:
                        raise RuntimeError(
                            f"chapter_summary_failed book_id={book_id} failed_chapters={failed_chapter_ids}"
                        )

                chapter_changed = not has_all_summaries
                logger.warning("[分析进度][book=%s] 开始情节聚类...", book_id)
                plot_segmentation_stats = PlotSegmentationEngine().run(book_id)
                logger.warning("[分析进度][book=%s] 开始生成情节摘要并入库...", book_id)
                plot_stats = PlotRecordBuilder().run(book_id)
                plot_changed = not bool(plot_stats.get("skipped"))
                volume_stats = plot_stats.get("volume") if isinstance(plot_stats, dict) else None
                volume_record_stats = (
                    volume_stats.get("volume_records")
                    if isinstance(volume_stats, dict)
                    else None
                )
                volume_changed = not (
                    isinstance(volume_stats, dict)
                    and bool(volume_stats.get("skipped"))
                    and isinstance(volume_record_stats, dict)
                    and bool(volume_record_stats.get("skipped"))
                )

                collections_to_sync: list[str] = []
                if chapter_changed:
                    collections_to_sync.append("chapterSummaryEmbedding")
                if plot_changed:
                    collections_to_sync.append("plotSummaryEmbedding")
                if volume_changed:
                    collections_to_sync.append("volumeSummaryEmbedding")

                if collections_to_sync:
                    logger.warning(
                        "[分析进度][book=%s] 开始写入Qdrant三层向量索引：%s",
                        book_id,
                        json.dumps(collections_to_sync, ensure_ascii=False),
                    )
                    from database.qdrant_client import sync_book_embedding_collections

                    embedding_stats = sync_book_embedding_collections(
                        book_id,
                        collections_to_sync,
                        force=True,
                        reset=True,
                    )
                    logger.warning(
                        "[分析进度][book=%s] Qdrant向量索引写入完成：%s",
                        book_id,
                        json.dumps(embedding_stats, ensure_ascii=False),
                    )
                else:
                    logger.warning(
                        "[分析进度][book=%s] 三层Qdrant向量索引跳过：chapter/plot/volume均未发生改动。",
                        book_id,
                    )
                cursor.execute(
                    "UPDATE books SET status = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                    ("completed", book_id),
                )
    except Exception:
        if book_id:
            try:
                with _connect() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            "UPDATE books SET status = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                            ("error", book_id),
                        )
            except Exception:
                pass
        raise

    return {"book_id": book_id, "title": book_title, "updated_chapters": updated_count}
