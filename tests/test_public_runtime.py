from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.mysql_client import MySQLChatStore
from reflex_app.state import NovelState
from core.public_runtime import is_agent_runtime_prewarm_enabled, validate_public_runtime_env


def test_validate_public_runtime_env_rejects_placeholder_secrets():
    errors = validate_public_runtime_env(
        {
            "MYSQL_ROOT_PASSWORD": "change-me-mysql-root-password",
            "MYSQL_PASSWORD": "change-me-story2memory-db-password",
            "NEO4J_PASSWORD": "change-me-neo4j-password",
        }
    )

    assert len(errors) == 3
    assert any("MYSQL_ROOT_PASSWORD" in item for item in errors)
    assert any("MYSQL_PASSWORD" in item for item in errors)
    assert any("NEO4J_PASSWORD" in item for item in errors)


def test_validate_public_runtime_env_allows_fake_llm_values_when_secrets_are_real():
    errors = validate_public_runtime_env(
        {
            "MYSQL_ROOT_PASSWORD": "root-secret-for-smoke",
            "MYSQL_PASSWORD": "app-secret-for-smoke",
            "NEO4J_PASSWORD": "neo4j-secret-for-smoke",
            "LLM_API_KEY": "fake-key",
            "LLM_BASE_URL": "https://example.invalid/v1",
            "LLM_MODEL": "fake-model",
        }
    )

    assert errors == []


def test_prewarm_flag_defaults_to_disabled(monkeypatch):
    monkeypatch.delenv("AGENT_RUNTIME_PREWARM_ENABLED", raising=False)
    assert is_agent_runtime_prewarm_enabled() is False


def test_prewarm_flag_can_be_enabled(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_PREWARM_ENABLED", "1")
    assert is_agent_runtime_prewarm_enabled() is True


def test_load_books_skips_agent_runtime_prewarm_when_disabled(monkeypatch):
    import agent.chat_agent as chat_agent
    import rag.uploadBook as upload_book

    monkeypatch.setenv("AGENT_RUNTIME_PREWARM_ENABLED", "0")
    monkeypatch.setattr(upload_book, "recover_interrupted_book_statuses", lambda: 0)
    monkeypatch.setattr(upload_book, "list_books", lambda: [])

    def fail_if_called(_book_ids):
        raise AssertionError("prewarm should not run when public default disables it")

    monkeypatch.setattr(chat_agent, "schedule_agent_runtime_prewarm", fail_if_called)

    state = NovelState()
    state.load_books()
    assert state.uploaded_books == []


def test_load_books_schedules_agent_runtime_prewarm_when_enabled(monkeypatch):
    import agent.chat_agent as chat_agent
    import rag.uploadBook as upload_book
    import reflex_app.state as state_module

    monkeypatch.setenv("AGENT_RUNTIME_PREWARM_ENABLED", "1")
    monkeypatch.setattr(upload_book, "recover_interrupted_book_statuses", lambda: 0)
    monkeypatch.setattr(
        upload_book,
        "list_books",
        lambda: [{"id": 7, "title": "Demo", "author": "作者", "cover_url": "", "total_chapters": 1, "total_words": 10, "status": "completed"}],
    )
    monkeypatch.setattr(state_module, "_BOOK_STATUS_RECOVERY_DONE", False)

    calls: list[list[int]] = []

    def capture(book_ids):
        calls.append(list(book_ids))

    monkeypatch.setattr(chat_agent, "schedule_agent_runtime_prewarm", capture)

    state = NovelState()
    state.load_books()
    assert calls == [[7]]


def test_load_books_generates_book_specific_cover_when_managed_cover_file_is_missing(monkeypatch):
    import rag.uploadBook as upload_book
    import reflex_app.state as state_module

    monkeypatch.setattr(upload_book, "recover_interrupted_book_statuses", lambda: 0)
    monkeypatch.setattr(
        upload_book,
        "list_books",
        lambda: [
            {
                "id": 7,
                "title": "Demo",
                "author": "作者",
                "cover_url": "/covers/demo.jpg",
                "total_chapters": 1,
                "total_words": 10,
                "status": "completed",
            }
        ],
    )
    monkeypatch.setattr(state_module, "_BOOK_STATUS_RECOVERY_DONE", False)
    monkeypatch.setattr(state_module, "_managed_cover_exists", lambda raw_cover: False)

    state = NovelState()
    state.load_books()

    assert len(state.uploaded_books) == 1
    assert state.uploaded_books[0].cover.startswith("data:image/svg+xml;utf8,")
    assert "Demo" in state.uploaded_books[0].cover


def test_mysql_chat_store_append_message_uses_auto_increment(monkeypatch):
    class FakeCursor:
        def __init__(self):
            self.executed: list[tuple[str, tuple[object, ...] | None]] = []
            self.lastrowid = 42

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
    store = MySQLChatStore("mysql+pymysql://story2memory:secret@mysql:3306/novel_cognition")
    monkeypatch.setattr(store, "_ensure_schema", lambda: True)
    monkeypatch.setattr(store, "_connect", lambda: FakeConnection(cursor))

    inserted_id = store.append_message("session-1", "assistant", "hello", token_count=9)

    assert inserted_id == 42
    assert len(cursor.executed) == 1
    sql, params = cursor.executed[0]
    normalized_sql = " ".join(sql.split())
    assert "MAX(id)" not in normalized_sql
    assert "INSERT INTO messages (session_id, role, content, token_count)" in normalized_sql
    assert params == ("session-1", "assistant", "hello", 9)
