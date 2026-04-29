from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

try:
    import pymysql
except ModuleNotFoundError:  # pragma: no cover - runtime optional dependency
    pymysql = None

from .mysql_dsn import parse_mysql_dsn, resolve_mysql_dsn

logger = logging.getLogger(__name__)


class MySQLChatStore:
    """Minimal MySQL storage for session summary + raw messages."""

    def __init__(self, dsn: str | None = None):
        self._dsn = (dsn or resolve_mysql_dsn()).strip()
        self._conn_cfg = self._parse_mysql_dsn(self._dsn) if self._dsn else None
        self.enabled = bool(self._conn_cfg) and pymysql is not None
        self._schema_ready = False
        self._schema_lock = threading.Lock()

    @staticmethod
    def _parse_mysql_dsn(dsn: str) -> dict[str, Any] | None:
        parsed = parse_mysql_dsn(dsn)
        if not parsed:
            return None

        return {
            **parsed,
            "charset": "utf8mb4",
            "autocommit": True,
            "cursorclass": pymysql.cursors.DictCursor if pymysql else None,
        }

    def _connect(self):
        if not self.enabled or not self._conn_cfg or pymysql is None:
            raise RuntimeError("MySQL chat store is not enabled.")
        return pymysql.connect(**self._conn_cfg)

    def _ensure_schema(self) -> bool:
        if not self.enabled:
            return False
        if self._schema_ready:
            return True

        with self._schema_lock:
            if self._schema_ready:
                return True

            sql_path = Path(__file__).resolve().parent / "mysql" / "create_tables.sql"
            if not sql_path.exists():
                logger.warning("MySQL schema file not found: %s", sql_path)
                return False

            try:
                schema_sql = sql_path.read_text(encoding="utf-8")
                statements = [stmt.strip() for stmt in schema_sql.split(";") if stmt.strip()]
                with self._connect() as conn:
                    with conn.cursor() as cursor:
                        for statement in statements:
                            cursor.execute(statement)
                        session_columns: tuple[tuple[str, str], ...] = (
                            (
                                "book_id",
                                "ALTER TABLE `sessions` ADD COLUMN `book_id` INT DEFAULT NULL COMMENT '归属书籍ID（用于精确删除）' AFTER `title`",
                            ),
                            (
                                "session_kind",
                                "ALTER TABLE `sessions` ADD COLUMN `session_kind` ENUM('qa', 'roleplay') NOT NULL DEFAULT 'qa' COMMENT '会话类型' AFTER `book_id`",
                            ),
                            (
                                "character_id",
                                "ALTER TABLE `sessions` ADD COLUMN `character_id` BIGINT DEFAULT NULL COMMENT '角色扮演会话绑定的角色ID' AFTER `session_kind`",
                            ),
                        )
                        for column_name, ddl in session_columns:
                            cursor.execute(
                                """
                                SELECT 1
                                FROM information_schema.COLUMNS
                                WHERE TABLE_SCHEMA = DATABASE()
                                  AND TABLE_NAME = 'sessions'
                                  AND COLUMN_NAME = %s
                                LIMIT 1
                                """,
                                (column_name,),
                            )
                            if not cursor.fetchone():
                                cursor.execute(ddl)
            except Exception:
                logger.exception("Failed to ensure MySQL schema.")
                return False

            self._schema_ready = True
            return True

    def ensure_session(
        self,
        session_id: str,
        user_id: str,
        title: str | None = None,
        *,
        book_id: int | None = None,
        session_kind: str = "qa",
        character_id: int | None = None,
    ) -> bool:
        if not self._ensure_schema():
            return False
        normalized_book_id = int(book_id or 0)
        normalized_character_id = int(character_id or 0)
        safe_session_kind = "roleplay" if str(session_kind or "").strip().lower() == "roleplay" else "qa"
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO sessions (id, user_id, title, book_id, session_kind, character_id)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            title = COALESCE(VALUES(title), title),
                            book_id = COALESCE(VALUES(book_id), book_id),
                            session_kind = VALUES(session_kind),
                            character_id = COALESCE(VALUES(character_id), character_id)
                        """,
                        (
                            session_id,
                            user_id,
                            title,
                            normalized_book_id or None,
                            safe_session_kind,
                            normalized_character_id or None,
                        ),
                    )
            return True
        except Exception:
            logger.exception("Failed to ensure session: session_id=%s", session_id)
            return False

    def get_summary(self, session_id: str) -> str:
        if not self._ensure_schema():
            return ""
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT COALESCE(current_summary, '') AS summary FROM sessions WHERE id = %s",
                        (session_id,),
                    )
                    row = cursor.fetchone()
                    if row:
                        return str(row.get("summary", "") or "")
        except Exception:
            logger.exception("Failed to get summary: session_id=%s", session_id)
        return ""

    def get_last_summarized_msg_id(self, session_id: str) -> int:
        if not self._ensure_schema():
            return 0
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT COALESCE(last_summarized_msg_id, 0) AS last_msg_id
                        FROM sessions
                        WHERE id = %s
                        """,
                        (session_id,),
                    )
                    row = cursor.fetchone()
                    if row:
                        return int(row.get("last_msg_id") or 0)
        except Exception:
            logger.exception("Failed to get last_summarized_msg_id: session_id=%s", session_id)
        return 0

    def append_message(self, session_id: str, role: str, content: str, token_count: int = 0) -> int:
        if not self._ensure_schema():
            return 0
        safe_role = role if role in {"user", "assistant", "system"} else "user"
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO messages (session_id, role, content, token_count)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (session_id, safe_role, content, max(0, int(token_count))),
                    )
                    return int(getattr(cursor, "lastrowid", 0) or 0)
        except Exception:
            logger.exception("Failed to append message: session_id=%s role=%s", session_id, safe_role)
        return 0

    def get_recent_messages(self, session_id: str, limit: int = 5) -> list[dict[str, Any]]:
        if not self._ensure_schema():
            return []
        size = max(1, int(limit))
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT id, role, content, token_count
                        FROM messages
                        WHERE session_id = %s
                        ORDER BY id DESC
                        LIMIT %s
                        """,
                        (session_id, size),
                    )
                    rows = cursor.fetchall() or []
                    rows.reverse()
                    return rows
        except Exception:
            logger.exception("Failed to get recent messages: session_id=%s", session_id)
        return []

    def get_messages_after(self, session_id: str, after_msg_id: int) -> list[dict[str, Any]]:
        if not self._ensure_schema():
            return []
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT id, role, content, token_count
                        FROM messages
                        WHERE session_id = %s AND id > %s
                        ORDER BY id ASC
                        """,
                        (session_id, max(0, int(after_msg_id))),
                    )
                    return cursor.fetchall() or []
        except Exception:
            logger.exception("Failed to get messages after id: session_id=%s", session_id)
        return []

    def get_latest_message_id(self, session_id: str) -> int:
        if not self._ensure_schema():
            return 0
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT COALESCE(MAX(id), 0) AS latest_id FROM messages WHERE session_id = %s",
                        (session_id,),
                    )
                    row = cursor.fetchone()
                    if row:
                        return int(row.get("latest_id") or 0)
        except Exception:
            logger.exception("Failed to get latest message id: session_id=%s", session_id)
        return 0

    def update_summary(self, session_id: str, summary: str, last_msg_id: int) -> bool:
        if not self._ensure_schema():
            return False
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE sessions
                        SET current_summary = %s, last_summarized_msg_id = %s
                        WHERE id = %s
                        """,
                        (summary, max(0, int(last_msg_id)), session_id),
                    )
            return True
        except Exception:
            logger.exception("Failed to update summary: session_id=%s", session_id)
            return False

    def delete_sessions(self, session_ids: list[str] | tuple[str, ...]) -> int:
        if not self._ensure_schema():
            return 0
        normalized_ids = [str(item or "").strip() for item in session_ids if str(item or "").strip()]
        if not normalized_ids:
            return 0
        placeholders = ", ".join(["%s"] * len(normalized_ids))
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        f"DELETE FROM sessions WHERE id IN ({placeholders})",
                        tuple(normalized_ids),
                    )
                    return int(getattr(cursor, "rowcount", 0) or 0)
        except Exception:
            logger.exception("Failed to delete sessions: count=%d", len(normalized_ids))
            return 0

    def delete_sessions_for_book(self, book_id: int) -> int:
        if not self._ensure_schema():
            return 0
        normalized_book_id = int(book_id or 0)
        if normalized_book_id <= 0:
            return 0
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM sessions WHERE book_id = %s",
                        (normalized_book_id,),
                    )
                    return int(getattr(cursor, "rowcount", 0) or 0)
        except Exception:
            logger.exception("Failed to delete sessions for book: book_id=%s", normalized_book_id)
            return 0
