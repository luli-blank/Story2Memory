from pathlib import Path
import os
import sys
from functools import lru_cache
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.mysql_client import MySQLChatStore
from reflex_app.state import NovelState
from core.public_runtime import (
    build_startup_settings,
    is_agent_runtime_prewarm_enabled,
    probe_startup_services,
    refresh_runtime_clients,
    validate_public_runtime_env,
)
import core.public_runtime as runtime


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


def test_build_startup_settings_prefers_single_ark_key_when_runtime_env_is_ark_compatible(tmp_path):
    settings = build_startup_settings(
        env={
            "LLM_API_KEY": "ark-key",
            "LLM_BASE_URL": "https://ark.cn-beijing.volces.com/api/coding/v3",
            "LLM_MODEL": "llm-model",
            "EMBED_API_KEY": "ark-key",
            "EMBED_BASE_URL": "https://ark.cn-beijing.volces.com/api/v3",
            "EMBED_MODEL": "embed-model",
            "HYBRID_DENSE_RETRIEVAL_ENABLED": "1",
            "RERANK_DISABLED": "0",
            "RERANK_PROVIDER": "qwen",
            "RERANK_BASE_URL": "https://dashscope.aliyuncs.com/compatible-api/v1/reranks",
            "RERANK_API_KEY": "rerank-key",
            "RERANK_MODEL": "qwen3-rerank",
        },
        path=tmp_path / "runtime.env",
    )

    assert settings["ark_api_key"] == "ark-key"
    assert settings["llm_model"] == "llm-model"
    assert settings["embed_model"] == "embed-model"
    assert settings["vector_retrieval_enabled"] is True
    assert settings["rerank_enabled"] is True
    assert settings["rerank_provider"] == "qwen"
    assert settings["rerank_api_key"] == "rerank-key"
    assert settings["rerank_model"] == "qwen3-rerank"


def test_prewarm_flag_defaults_to_disabled(monkeypatch):
    monkeypatch.delenv("AGENT_RUNTIME_PREWARM_ENABLED", raising=False)
    monkeypatch.setenv("STORY2MEMORY_ENV_OVERRIDE", "/tmp/story2memory-nonexistent.env")
    assert is_agent_runtime_prewarm_enabled(env={}) is False


def test_prewarm_flag_can_be_enabled(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_PREWARM_ENABLED", "1")
    monkeypatch.setenv("STORY2MEMORY_ENV_OVERRIDE", "/tmp/story2memory-nonexistent.env")
    assert is_agent_runtime_prewarm_enabled(
        env={
            "AGENT_RUNTIME_PREWARM_ENABLED": "1",
            "STORY2MEMORY_ENV_OVERRIDE": "/tmp/story2memory-nonexistent.env",
        }
    ) is True


def test_load_books_skips_agent_runtime_prewarm_when_disabled(monkeypatch):
    import agent.chat_agent as chat_agent
    import rag.uploadBook as upload_book

    monkeypatch.setenv("AGENT_RUNTIME_PREWARM_ENABLED", "0")
    monkeypatch.setattr(upload_book, "recover_interrupted_book_statuses", lambda: 0)
    monkeypatch.setattr(upload_book, "list_books", lambda: [])
    monkeypatch.setattr(
        "reflex_app.state.is_agent_runtime_prewarm_enabled",
        lambda: False,
    )

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
    monkeypatch.setattr(
        "reflex_app.state.is_agent_runtime_prewarm_enabled",
        lambda: True,
    )

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


def test_probe_startup_services_treats_qdrant_and_rerank_as_required(monkeypatch):
    import core.public_runtime as runtime

    monkeypatch.setattr(
        runtime,
        "_probe_mysql_status",
        lambda _env: {"key": "mysql", "label": "MySQL", "status": "ready", "detail": "ok", "blocking": True},
    )
    monkeypatch.setattr(
        runtime,
        "_probe_neo4j_status",
        lambda _env: {"key": "neo4j", "label": "Neo4j", "status": "ready", "detail": "ok", "blocking": True},
    )
    monkeypatch.setattr(
        runtime,
        "_probe_app_backend_status",
        lambda: {"key": "backend", "label": "App Backend", "status": "ready", "detail": "ok", "blocking": True},
    )

    services = probe_startup_services(
        {
            "ark_api_key": "ark-key",
            "llm_model": "llm-model",
            "embed_model": "embed-model",
            "vector_retrieval_enabled": False,
            "rerank_enabled": False,
            "prewarm_enabled": False,
            "rerank_provider": "qwen",
            "rerank_base_url": "https://dashscope.aliyuncs.com/compatible-api/v1/reranks",
            "rerank_api_key": "rerank-key",
            "rerank_model": "qwen3-rerank",
        },
        env={
            "MYSQL_DSN": "mysql+pymysql://story2memory:secret@mysql:3306/novel_cognition",
            "MYSQL_PASSWORD": "secret",
            "NEO4J_PASSWORD": "neo4j-secret",
        },
    )

    qdrant = next(item for item in services if item["key"] == "qdrant")
    rerank = next(item for item in services if item["key"] == "rerank")
    assert qdrant["blocking"] is True
    assert rerank["status"] == "ready"
    assert rerank["blocking"] is True


def test_refresh_runtime_clients_updates_env_and_clears_caches(monkeypatch):
    def _cached(label):
        @lru_cache(maxsize=1)
        def _inner():
            return label

        return _inner

    cache_targets = {
        "agent.chat_agent": SimpleNamespace(get_chat_agent=_cached("chat")),
        "agent.cosplay_agent": SimpleNamespace(get_cosplay_agent=_cached("cosplay")),
        "database.qdrant_client": SimpleNamespace(
            _get_embedding_client=_cached("embed"),
            get_qdrant_embedding_store=_cached("store"),
        ),
        "agent.hybridSearch": SimpleNamespace(
            _get_embedding_query_client=_cached("hybrid_embed"),
            _get_entity_embedding_query_client=_cached("hybrid_entity"),
            _get_hybrid_filter_llm=_cached("hybrid_llm"),
            _get_rerank_client=_cached("hybrid_rerank"),
        ),
        "agent.deepSearch": SimpleNamespace(_get_llm=_cached("deep_llm")),
        "agent.skills.retrieval_route_skill.route_skill": SimpleNamespace(_build_route_llm=_cached("route_llm")),
        "agent.searchAgent": SimpleNamespace(
            _get_recovery_planner_llm=_cached("planner"),
            get_agentic_research_graph=_cached("research_graph"),
        ),
        "rag.entity_qdrant_sync": SimpleNamespace(_get_entity_embedding_client=_cached("entity_embed")),
    }

    for module in cache_targets.values():
        for value in vars(module).values():
            if callable(value) and hasattr(value, "cache_info"):
                value()
                assert value.cache_info().currsize == 1

    for name, module in cache_targets.items():
        monkeypatch.setitem(sys.modules, name, module)

    managed_keys = ["LLM_API_KEY", "LLM_MODEL", "EMBED_MODEL", "HYBRID_DENSE_RETRIEVAL_ENABLED"]
    original_env = {key: os.environ.get(key) for key in managed_keys}

    try:
        cleared = refresh_runtime_clients(
            {
                "ark_api_key": "ark-key",
                "llm_model": "llm-model",
                "embed_model": "embed-model",
                "vector_retrieval_enabled": True,
                "rerank_enabled": True,
                "rerank_provider": "qwen",
                "rerank_base_url": "https://dashscope.aliyuncs.com/compatible-api/v1/reranks",
                "rerank_api_key": "rerank-key",
                "rerank_model": "qwen3-rerank",
                "prewarm_enabled": False,
            }
        )

        assert "agent.chat_agent.get_chat_agent" in cleared
        assert "agent.searchAgent.get_agentic_research_graph" in cleared
        assert "rag.entity_qdrant_sync._get_entity_embedding_client" in cleared
        assert os.environ["LLM_API_KEY"] == "ark-key"
        assert os.environ["EMBED_MODEL"] == "embed-model"
    finally:
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


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


def test_mysql_chat_store_derives_dsn_from_mysql_fields_when_example_dsn_is_left_unchanged(monkeypatch):
    monkeypatch.setenv(
        "MYSQL_DSN",
        "mysql+pymysql://story2memory:change-me-story2memory-db-password@mysql:3306/novel_cognition",
    )
    monkeypatch.setenv("MYSQL_USER", "story2memory")
    monkeypatch.setenv("MYSQL_PASSWORD", "runtime-secret")
    monkeypatch.setenv("MYSQL_DATABASE", "novel_cognition")
    monkeypatch.setenv("MYSQL_HOST", "mysql")
    monkeypatch.setenv("MYSQL_PORT", "3306")

    store = MySQLChatStore()

    assert store.enabled is True
    assert store._conn_cfg is not None
    assert store._conn_cfg["password"] == "runtime-secret"
    assert store._conn_cfg["host"] == "mysql"


def test_upload_book_chapter_summary_attempt_plan_uses_runtime_models(monkeypatch):
    import rag.uploadBook as upload_book

    monkeypatch.setenv("LLM_MODEL", "public-default-model")
    monkeypatch.delenv("CHAPTER_SUMMARY_MODEL", raising=False)
    monkeypatch.delenv("CHAPTER_SUMMARY_FALLBACK_MODEL", raising=False)

    attempt_plan = upload_book._chapter_summary_model_attempt_plan()

    expected_attempts = (
        1
        + upload_book.CHAPTER_SUMMARY_PRIMARY_RETRY_COUNT
        + upload_book.CHAPTER_SUMMARY_FALLBACK_RETRY_COUNT
    )
    assert attempt_plan == ["public-default-model"] * expected_attempts


def test_upload_book_chapter_summary_attempt_plan_supports_optional_fallback(monkeypatch):
    import rag.uploadBook as upload_book

    monkeypatch.setenv("LLM_MODEL", "primary-model")
    monkeypatch.setenv("CHAPTER_SUMMARY_FALLBACK_MODEL", "fallback-model")
    monkeypatch.delenv("CHAPTER_SUMMARY_MODEL", raising=False)

    attempt_plan = upload_book._chapter_summary_model_attempt_plan()

    assert attempt_plan[: 1 + upload_book.CHAPTER_SUMMARY_PRIMARY_RETRY_COUNT] == [
        "primary-model"
    ] * (1 + upload_book.CHAPTER_SUMMARY_PRIMARY_RETRY_COUNT)
    assert attempt_plan[-upload_book.CHAPTER_SUMMARY_FALLBACK_RETRY_COUNT :] == [
        "fallback-model"
    ] * upload_book.CHAPTER_SUMMARY_FALLBACK_RETRY_COUNT


def test_character_profile_json_retry_fallback_is_opt_in(monkeypatch):
    import rag.character_profiles as character_profiles

    monkeypatch.delenv("CHARACTER_PROFILE_JSON_RETRY_FALLBACK_MODEL", raising=False)
    monkeypatch.setenv("LLM_MODEL", "runtime-default-model")

    assert character_profiles._json_retry_fallback_model() is None

    monkeypatch.setenv("CHARACTER_PROFILE_JSON_RETRY_FALLBACK_MODEL", "secondary-runtime-model")

    assert character_profiles._json_retry_fallback_model() == "secondary-runtime-model"


def test_startup_embedding_test_uses_multimodal_endpoint_when_forced(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(runtime.httpx, "post", fake_post)

    runtime._test_ark_embedding(
        {
            "EMBED_API_KEY": "ark-key",
            "EMBED_BASE_URL": "https://ark.cn-beijing.volces.com/api/v3",
            "EMBED_MODEL": "ep-embed-model",
            "EMBED_ENDPOINT_MODE": "multimodal",
        }
    )

    assert captured["url"] == "https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal"
    assert captured["headers"] == {
        "Authorization": "Bearer ark-key",
        "Content-Type": "application/json",
    }
    assert captured["json"] == {
        "model": "ep-embed-model",
        "input": [{"type": "text", "text": "hello from story2memory"}],
        "encoding_format": "float",
    }


def test_startup_embedding_test_retries_with_multimodal_after_standard_api_rejection(monkeypatch):
    calls: list[tuple[str, object]] = []

    class FakeEmbeddings:
        def create(self, **kwargs):
            calls.append(("text", kwargs))
            raise RuntimeError(
                "Error code: 400 - {'error': {'code': 'InvalidParameter', 'message': 'the requested model "
                "doubao-embedding-vision-250615 does not support this api.', 'param': 'model'}}"
            )

    class FakeOpenAIClient:
        def __init__(self, *args, **kwargs):
            self.embeddings = FakeEmbeddings()

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(("multimodal", {"url": url, "headers": headers, "json": json, "timeout": timeout}))
        return FakeResponse()

    monkeypatch.setattr(runtime, "OpenAI", FakeOpenAIClient)
    monkeypatch.setattr(runtime.httpx, "post", fake_post)

    runtime._test_ark_embedding(
        {
            "EMBED_API_KEY": "ark-key",
            "EMBED_BASE_URL": "https://ark.cn-beijing.volces.com/api/v3",
            "EMBED_MODEL": "ep-embed-model",
        }
    )

    assert calls[0][0] == "text"
    assert calls[1][0] == "multimodal"
    assert calls[1][1]["url"] == "https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal"


def test_validate_startup_settings_requires_remote_rerank_key_when_enabled():
    from core.public_runtime import validate_startup_settings

    errors = validate_startup_settings(
        {
            "ark_api_key": "ark-key",
            "llm_model": "llm-model",
            "embed_model": "embed-model",
            "rerank_enabled": True,
            "rerank_provider": "qwen",
            "rerank_base_url": "https://dashscope.aliyuncs.com/compatible-api/v1/reranks",
            "rerank_api_key": "",
            "rerank_model": "qwen3-rerank",
        }
    )

    assert any("RERANK_API_KEY" in item for item in errors)


def test_validate_startup_settings_requires_rerank_fields_even_without_explicit_enable_flag():
    from core.public_runtime import validate_startup_settings

    errors = validate_startup_settings(
        {
            "ark_api_key": "ark-key",
            "llm_model": "llm-model",
            "embed_model": "embed-model",
            "rerank_provider": "",
            "rerank_base_url": "",
            "rerank_api_key": "",
            "rerank_model": "",
        }
    )

    assert any("RERANK_PROVIDER" in item for item in errors)
    assert any("RERANK_BASE_URL" in item for item in errors)
    assert any("RERANK_API_KEY" in item for item in errors)
    assert any("RERANK_MODEL" in item for item in errors)


def test_startup_qwen_rerank_test_calls_dashscope_compatible_endpoint(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"index": 0, "relevance_score": 0.9}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(runtime.httpx, "post", fake_post)

    runtime._test_rerank(
        {
            "RERANK_PROVIDER": "qwen",
            "RERANK_BASE_URL": "https://dashscope.aliyuncs.com/compatible-api/v1/reranks",
            "RERANK_API_KEY": "rerank-key",
            "RERANK_MODEL": "qwen3-rerank",
            "RERANK_INSTRUCTION": "instruction",
        }
    )

    assert captured["url"] == "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"
    assert captured["headers"] == {
        "Authorization": "Bearer rerank-key",
        "Content-Type": "application/json",
    }
    assert captured["json"]["model"] == "qwen3-rerank"
    assert captured["json"]["instruct"] == "instruction"
