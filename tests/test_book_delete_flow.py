from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.mysql_client import MySQLChatStore
from database.session_keys import (
    build_cosplay_session_info,
    build_legacy_qa_session_info,
    build_qa_session_info,
)
from rag import uploadBook
from reflex_app.state import NovelState


def test_build_qa_session_info_uses_book_id_for_precise_scope():
    scoped_session_id, _, scoped_title = build_qa_session_info(novel_title="测试书", book_id=7)
    legacy_session_id, _, legacy_title = build_legacy_qa_session_info(novel_title="测试书")

    assert scoped_session_id != legacy_session_id
    assert scoped_title == "测试书"
    assert legacy_title == "测试书"


def test_mysql_chat_store_delete_sessions_uses_session_ids(monkeypatch):
    class FakeCursor:
        def __init__(self):
            self.executed: list[tuple[str, tuple[object, ...] | None]] = []
            self.rowcount = 2

        def execute(self, sql, params=None):
            self.executed.append((str(sql), params))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeConnection:
        def __init__(self, cursor):
            self._cursor = cursor

        def cursor(self):
            return self._cursor

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    cursor = FakeCursor()
    connection = FakeConnection(cursor)
    store = MySQLChatStore("mysql+pymysql://user:pass@localhost:3306/testdb")

    monkeypatch.setattr(store, "_ensure_schema", lambda: True)
    monkeypatch.setattr(store, "_connect", lambda: connection)

    deleted = store.delete_sessions(["session-a", "session-b"])

    assert deleted == 2
    assert "DELETE FROM sessions WHERE id IN (%s, %s)" in cursor.executed[0][0]
    assert cursor.executed[0][1] == ("session-a", "session-b")


def test_mysql_chat_store_ensure_session_persists_precise_book_scope(monkeypatch):
    class FakeCursor:
        def __init__(self):
            self.executed: list[tuple[str, tuple[object, ...] | None]] = []

        def execute(self, sql, params=None):
            self.executed.append((str(sql), params))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeConnection:
        def __init__(self, cursor):
            self._cursor = cursor

        def cursor(self):
            return self._cursor

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    cursor = FakeCursor()
    store = MySQLChatStore("mysql+pymysql://user:pass@localhost:3306/testdb")
    monkeypatch.setattr(store, "_ensure_schema", lambda: True)
    monkeypatch.setattr(store, "_connect", lambda: FakeConnection(cursor))

    assert store.ensure_session(
        "session-a",
        "user-a",
        "测试书",
        book_id=7,
        session_kind="qa",
        character_id=0,
    )

    sql, params = cursor.executed[0]
    assert "INSERT INTO sessions (id, user_id, title, book_id, session_kind, character_id)" in sql
    assert params == ("session-a", "user-a", "测试书", 7, "qa", None)


def test_mysql_chat_store_delete_sessions_for_book_uses_book_id(monkeypatch):
    class FakeCursor:
        def __init__(self):
            self.executed: list[tuple[str, tuple[object, ...] | None]] = []
            self.rowcount = 4

        def execute(self, sql, params=None):
            self.executed.append((str(sql), params))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeConnection:
        def __init__(self, cursor):
            self._cursor = cursor

        def cursor(self):
            return self._cursor

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    cursor = FakeCursor()
    connection = FakeConnection(cursor)
    store = MySQLChatStore("mysql+pymysql://user:pass@localhost:3306/testdb")

    monkeypatch.setattr(store, "_ensure_schema", lambda: True)
    monkeypatch.setattr(store, "_connect", lambda: connection)

    deleted = store.delete_sessions_for_book(3)

    assert deleted == 4
    assert "DELETE FROM sessions WHERE book_id = %s" in cursor.executed[0][0]
    assert cursor.executed[0][1] == (3,)


def test_delete_book_cascade_removes_precise_sessions_and_managed_files(monkeypatch, tmp_path):
    book_dir = tmp_path / "book"
    picture_dir = tmp_path / "picture"
    book_dir.mkdir()
    picture_dir.mkdir()
    source_path = book_dir / "demo.epub"
    cover_path = picture_dir / "demo.jpg"
    source_path.write_text("demo", encoding="utf-8")
    cover_path.write_text("cover", encoding="utf-8")

    executed_sql: list[tuple[str, tuple[object, ...] | None]] = []

    class FakeSelectCursor:
        def __init__(self):
            self._last_sql = ""

        def execute(self, sql, params=None):
            self._last_sql = str(sql)
            executed_sql.append((self._last_sql, params))

        def fetchone(self):
            if "COUNT(*) AS title_count" in self._last_sql:
                return {"title_count": 1}
            if "FROM books" in self._last_sql:
                return {
                    "id": 3,
                    "title": "测试书",
                    "file_path": str(source_path),
                    "cover_url": "/covers/demo.jpg",
                }
            return {}

        def fetchall(self):
            if "FROM characters" in self._last_sql:
                return [{"id": 11, "name": "林夏"}]
            return []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeDeleteCursor:
        def __init__(self):
            self.rowcount = 1

        def execute(self, sql, params=None):
            executed_sql.append((str(sql), params))
            self.rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeConnection:
        def __init__(self, cursor):
            self._cursor = cursor

        def cursor(self):
            return self._cursor

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    connections = iter([FakeConnection(FakeSelectCursor()), FakeConnection(FakeDeleteCursor())])
    monkeypatch.setattr(uploadBook, "_connect", lambda: next(connections))
    monkeypatch.setattr(uploadBook, "_ensure_schema", lambda conn: None)
    monkeypatch.setattr(uploadBook, "BOOK_DIR", book_dir)
    monkeypatch.setattr(uploadBook, "PICTURE_DIR", picture_dir)

    deleted_book_ids: list[int] = []
    deleted_session_ids: list[str] = []

    class FakeChatStore:
        def delete_sessions_for_book(self, book_id):
            deleted_book_ids.append(int(book_id))
            return 2

        def delete_sessions(self, session_ids):
            deleted_session_ids.extend(session_ids)
            return len(session_ids)

    monkeypatch.setattr(uploadBook, "MySQLChatStore", lambda: FakeChatStore())
    monkeypatch.setattr("database.qdrant_client.delete_book_embedding_collections", lambda book_id: {"ok": {"deleted": book_id}})
    monkeypatch.setattr("rag.entity_qdrant_sync.delete_entity_collections", lambda *, book_id: {"ok": {"deleted": book_id}})
    monkeypatch.setattr("relationGraph.sync.delete_book_relation_graph", lambda book_id: {"deleted": book_id})

    result = uploadBook.delete_book_cascade(3)

    current_qa_session_id, _, _ = build_qa_session_info(novel_title="测试书", book_id=3)
    legacy_qa_session_id, _, _ = build_legacy_qa_session_info(novel_title="测试书")
    cosplay_session_id, _, _ = build_cosplay_session_info(
        book_id=3,
        novel_title="测试书",
        character_id=11,
        character_name="林夏",
    )

    assert result["deleted"] == 1
    assert deleted_book_ids == [3]
    assert sorted(deleted_session_ids) == sorted([current_qa_session_id, legacy_qa_session_id, cosplay_session_id])
    assert not source_path.exists()
    assert not cover_path.exists()
    assert any("DELETE FROM `book_chapters`" in sql for sql, _ in executed_sql)
    assert any("DELETE FROM books WHERE id = %s" in sql for sql, _ in executed_sql)


def test_delete_book_cascade_skips_ambiguous_legacy_title_sessions(monkeypatch, tmp_path):
    book_dir = tmp_path / "book"
    picture_dir = tmp_path / "picture"
    book_dir.mkdir()
    picture_dir.mkdir()
    source_path = book_dir / "demo.epub"
    source_path.write_text("demo", encoding="utf-8")

    class FakeSelectCursor:
        def __init__(self):
            self._last_sql = ""

        def execute(self, sql, params=None):
            self._last_sql = str(sql)

        def fetchone(self):
            if "COUNT(*) AS title_count" in self._last_sql:
                return {"title_count": 2}
            if "FROM books" in self._last_sql:
                return {
                    "id": 5,
                    "title": "同名书",
                    "file_path": str(source_path),
                    "cover_url": "",
                }
            return {}

        def fetchall(self):
            if "FROM characters" in self._last_sql:
                return []
            return []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeDeleteCursor:
        def __init__(self):
            self.rowcount = 1

        def execute(self, sql, params=None):
            self.rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeConnection:
        def __init__(self, cursor):
            self._cursor = cursor

        def cursor(self):
            return self._cursor

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    connections = iter([FakeConnection(FakeSelectCursor()), FakeConnection(FakeDeleteCursor())])
    monkeypatch.setattr(uploadBook, "_connect", lambda: next(connections))
    monkeypatch.setattr(uploadBook, "_ensure_schema", lambda conn: None)
    monkeypatch.setattr(uploadBook, "BOOK_DIR", book_dir)
    monkeypatch.setattr(uploadBook, "PICTURE_DIR", picture_dir)

    deleted_book_ids: list[int] = []
    deleted_session_ids: list[str] = []

    class FakeChatStore:
        def delete_sessions_for_book(self, book_id):
            deleted_book_ids.append(int(book_id))
            return 1

        def delete_sessions(self, session_ids):
            deleted_session_ids.extend(session_ids)
            return len(session_ids)

    monkeypatch.setattr(uploadBook, "MySQLChatStore", lambda: FakeChatStore())
    monkeypatch.setattr("database.qdrant_client.delete_book_embedding_collections", lambda book_id: {"ok": {"deleted": book_id}})
    monkeypatch.setattr("rag.entity_qdrant_sync.delete_entity_collections", lambda *, book_id: {"ok": {"deleted": book_id}})
    monkeypatch.setattr("relationGraph.sync.delete_book_relation_graph", lambda book_id: {"deleted": book_id})

    uploadBook.delete_book_cascade(5)

    current_qa_session_id, _, _ = build_qa_session_info(novel_title="同名书", book_id=5)
    legacy_qa_session_id, _, _ = build_legacy_qa_session_info(novel_title="同名书")

    assert deleted_book_ids == [5]
    assert current_qa_session_id in deleted_session_ids
    assert legacy_qa_session_id not in deleted_session_ids


def test_state_delete_dialog_flow_resets_after_cancel():
    state = NovelState()
    state.prompt_delete_book(5, "测试书")

    assert state.delete_book_dialog_open is True
    assert state.delete_book_target_id == 5
    assert state.delete_book_target_title == "测试书"

    state.cancel_delete_book()

    assert state.delete_book_dialog_open is False
    assert state.delete_book_target_id == 0
    assert state.delete_book_target_title == ""
