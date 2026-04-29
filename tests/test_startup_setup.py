import asyncio
import inspect
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.public_runtime import build_startup_settings, validate_startup_settings, write_startup_settings
from reflex_app.frontend.startup_setup import startup_setup_view
from reflex_app.state import NovelState, StartupServiceStatus


async def _drain_state_event(result):
    if inspect.isasyncgen(result):
        async for _ in result:
            pass
        return
    if inspect.isawaitable(result):
        await result


def test_build_startup_settings_maps_legacy_ark_compatible_env(tmp_path):
    settings = build_startup_settings(
        env={
            "LLM_API_KEY": "ark-key",
            "LLM_BASE_URL": "https://ark.cn-beijing.volces.com/api/coding/v3",
            "LLM_MODEL": "ark-llm-model",
            "EMBED_API_KEY": "ark-key",
            "EMBED_BASE_URL": "https://ark.cn-beijing.volces.com/api/v3",
            "EMBED_MODEL": "ark-embed-model",
            "HYBRID_DENSE_RETRIEVAL_ENABLED": "1",
            "RERANK_DISABLED": "1",
        },
        path=tmp_path / "runtime.env",
    )

    assert settings["ark_api_key"] == "ark-key"
    assert settings["llm_model"] == "ark-llm-model"
    assert settings["embed_model"] == "ark-embed-model"
    assert settings["vector_retrieval_enabled"] is True
    assert settings["rerank_enabled"] is True


def test_validate_startup_settings_requires_ark_fields():
    errors = validate_startup_settings(
        {
            "ark_api_key": "",
            "llm_model": "",
            "embed_model": "",
            "vector_retrieval_enabled": True,
            "rerank_enabled": True,
            "rerank_provider": "",
            "rerank_base_url": "",
            "rerank_api_key": "",
            "rerank_model": "",
            "prewarm_enabled": False,
        }
    )

    assert any("ARK_API_KEY" in item for item in errors)
    assert any("LLM_MODEL" in item for item in errors)
    assert any("EMBED_MODEL" in item for item in errors)
    assert any("RERANK_PROVIDER" in item for item in errors)
    assert any("RERANK_BASE_URL" in item for item in errors)
    assert any("RERANK_API_KEY" in item for item in errors)
    assert any("RERANK_MODEL" in item for item in errors)


def test_write_startup_settings_persists_ark_runtime_env(tmp_path):
    target = tmp_path / "runtime.env"

    saved_path = write_startup_settings(
        {
            "ark_api_key": "ark-key",
            "llm_model": "ark-llm-model",
            "embed_model": "ark-embed-model",
            "vector_retrieval_enabled": True,
            "rerank_enabled": True,
            "rerank_provider": "qwen",
            "rerank_base_url": "https://dashscope.aliyuncs.com/compatible-api/v1/reranks",
            "rerank_api_key": "rerank-key",
            "rerank_model": "qwen3-rerank",
            "rerank_instruction": "Given a web search query, retrieve relevant passages that answer the query.",
            "prewarm_enabled": True,
        },
        path=target,
    )

    content = saved_path.read_text(encoding="utf-8")
    assert saved_path == target
    assert "LLM_API_KEY=ark-key" in content
    assert "LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/coding/v3" in content
    assert "EMBED_API_KEY=ark-key" in content
    assert "EMBED_MODEL=ark-embed-model" in content
    assert "QDRANT_URL=http://qdrant:6333" in content
    assert "RERANK_DISABLED=0" in content
    assert "RERANK_PROVIDER=qwen" in content
    assert "RERANK_BASE_URL=https://dashscope.aliyuncs.com/compatible-api/v1/reranks" in content
    assert "RERANK_API_KEY=rerank-key" in content
    assert "RERANK_MODEL=qwen3-rerank" in content
    assert "AGENT_RUNTIME_PREWARM_ENABLED=1" in content


def test_initialize_app_opens_startup_setup_and_loads_services(monkeypatch):
    import rag.uploadBook as upload_book
    import reflex_app.state as state_module

    state = NovelState()
    monkeypatch.setattr(upload_book, "recover_interrupted_book_statuses", lambda: 0)
    monkeypatch.setattr(
        upload_book,
        "list_books",
        lambda: [{"id": 9, "title": "向导测试", "author": "作者", "cover_url": "", "total_chapters": 1, "status": "completed"}],
    )
    monkeypatch.setattr(state_module, "_BOOK_STATUS_RECOVERY_DONE", False)
    monkeypatch.setattr(
        "reflex_app.state.build_startup_settings",
        lambda **_: {
            "ark_api_key": "",
            "llm_model": "",
            "embed_model": "",
            "vector_retrieval_enabled": True,
            "rerank_enabled": True,
            "rerank_provider": "qwen",
            "rerank_base_url": "https://dashscope.aliyuncs.com/compatible-api/v1/reranks",
            "rerank_api_key": "",
            "rerank_model": "qwen3-rerank",
            "rerank_instruction": "Given a web search query, retrieve relevant passages that answer the query.",
            "prewarm_enabled": False,
        },
    )
    monkeypatch.setattr(
        "reflex_app.state.probe_startup_services",
        lambda *_args, **_kwargs: [
            {"key": "mysql", "label": "MySQL", "status": "ready", "detail": "ok", "blocking": True},
            {"key": "neo4j", "label": "Neo4j", "status": "ready", "detail": "ok", "blocking": True},
            {"key": "qdrant", "label": "Qdrant", "status": "ready", "detail": "ok", "blocking": True},
            {"key": "rerank", "label": "Rerank Remote", "status": "ready", "detail": "ok", "blocking": True},
            {"key": "backend", "label": "App Backend", "status": "ready", "detail": "ok", "blocking": True},
        ],
    )

    state.initialize_app()

    assert state.page_mode == "startup_setup"
    assert len(state.uploaded_books) == 1
    assert state.uploaded_books[0].title == "向导测试"
    assert len(state.startup_service_statuses) == 5
    assert state.startup_service_statuses[0].label == "MySQL"


def test_enter_bookshelf_from_startup_setup_is_strictly_blocked_without_fresh_test():
    state = NovelState()
    state.page_mode = "startup_setup"
    state.enter_bookshelf()

    assert state.page_mode == "startup_setup"
    assert "请先完成 Ark 配置测试" in state.startup_feedback
    assert state.startup_feedback_is_error is True


def test_startup_input_change_invalidates_previous_test_state():
    state = NovelState()
    state.startup_test_passed = True
    state.startup_last_test_signature = state._startup_settings_signature()

    state.set_setup_llm_model("new-model")

    assert state.startup_test_passed is False
    assert state.startup_last_test_signature == ""
    assert state.startup_test_is_fresh is False


def test_required_qdrant_and_rerank_rows_remain_blocking():
    state = NovelState()
    state.startup_service_statuses = [
        StartupServiceStatus(key="qdrant", label="Qdrant", status="failed", detail="bad", blocking=True),
        StartupServiceStatus(key="rerank", label="Rerank Remote", status="failed", detail="bad", blocking=True),
    ]

    state.set_setup_vector_retrieval_enabled(False)
    state.set_setup_rerank_enabled(False)

    assert state.setup_vector_retrieval_enabled is True
    assert state.setup_rerank_enabled is True
    assert state.startup_service_statuses[0].status == "failed"
    assert state.startup_service_statuses[0].blocking is True
    assert state.startup_service_statuses[1].status == "failed"
    assert state.startup_service_statuses[1].blocking is True


def test_apply_startup_config_writes_and_hot_refreshes_without_restart(monkeypatch):
    import rag.uploadBook as upload_book
    import reflex_app.state as state_module

    state = NovelState()
    state.setup_ark_api_key = "ark-key"
    state.setup_llm_model = "llm-model"
    state.setup_embed_model = "embed-model"
    state.setup_vector_retrieval_enabled = True
    state.setup_rerank_enabled = True
    state.setup_rerank_provider = "qwen"
    state.setup_rerank_base_url = "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"
    state.setup_rerank_api_key = "rerank-key"
    state.setup_rerank_model = "qwen3-rerank"
    state.startup_test_passed = True
    state.startup_last_test_signature = state._startup_settings_signature()
    state.startup_service_statuses = []

    writes: list[dict[str, object]] = []
    refreshes: list[dict[str, object]] = []
    status_loads: list[str] = []

    monkeypatch.setattr(
        state_module,
        "write_startup_settings",
        lambda payload: writes.append(dict(payload)) or Path("/tmp/runtime.env"),
    )
    monkeypatch.setattr(
        state_module,
        "refresh_runtime_clients",
        lambda payload: refreshes.append(dict(payload)) or ["agent.chat_agent.get_chat_agent"],
    )
    monkeypatch.setattr(
        state_module,
        "build_startup_settings",
        lambda **_: {
            "ark_api_key": "ark-key",
            "llm_model": "llm-model",
            "embed_model": "embed-model",
            "vector_retrieval_enabled": True,
            "rerank_enabled": True,
            "rerank_provider": "qwen",
            "rerank_base_url": "https://dashscope.aliyuncs.com/compatible-api/v1/reranks",
            "rerank_api_key": "rerank-key",
            "rerank_model": "qwen3-rerank",
            "rerank_instruction": "Given a web search query, retrieve relevant passages that answer the query.",
            "prewarm_enabled": False,
        },
    )
    monkeypatch.setattr(upload_book, "recover_interrupted_book_statuses", lambda: 0)
    monkeypatch.setattr(upload_book, "list_books", lambda: [])
    monkeypatch.setattr(
        state_module,
        "probe_startup_services",
        lambda *_args, **_kwargs: status_loads.append("loaded")
        or [
            {"key": "mysql", "label": "MySQL", "status": "ready", "detail": "ok", "blocking": True},
            {"key": "neo4j", "label": "Neo4j", "status": "ready", "detail": "ok", "blocking": True},
            {"key": "qdrant", "label": "Qdrant", "status": "ready", "detail": "ok", "blocking": True},
            {"key": "rerank", "label": "Rerank Remote", "status": "ready", "detail": "ok", "blocking": True},
            {"key": "backend", "label": "App Backend", "status": "ready", "detail": "ok", "blocking": True},
        ],
    )

    asyncio.run(_drain_state_event(state.apply_startup_config()))

    assert len(writes) == 1
    assert len(refreshes) == 1
    assert state.page_mode == "bookshelf"
    assert status_loads == ["loaded"]
    assert state.uploaded_books == []
    assert state.startup_feedback_is_error is False
    assert "当前会话已切换" in state.startup_feedback


def test_startup_setup_view_builds_ark_only_ui():
    rendered = str(startup_setup_view())

    assert "ARK_API_KEY" in rendered
    assert "LLM_MODEL" in rendered
    assert "EMBED_MODEL" in rendered
    assert "LLM_BASE_URL" in rendered
    assert "EMBED_BASE_URL" in rendered
    assert "RERANK_PROVIDER" in rendered
    assert "RERANK_API_KEY" in rendered
    assert "RERANK_BASE_URL" in rendered
    assert "Embedding API Key" not in rendered
    assert "Qdrant URL" not in rendered
    assert "进入书架" not in rendered
