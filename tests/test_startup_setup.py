from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.public_runtime import (
    build_startup_settings,
    validate_startup_settings,
    write_startup_settings,
)
from reflex_app.frontend.startup_setup import startup_setup_view
from reflex_app.state import NovelState


def test_build_startup_settings_defaults_optional_capabilities_to_disabled(tmp_path):
    settings = build_startup_settings(
        env={
            "LLM_API_KEY": "env-llm-key",
            "LLM_BASE_URL": "https://llm.example/v1",
            "LLM_MODEL": "gpt-test",
            "QDRANT_URL": "http://qdrant:6333",
        },
        path=tmp_path / "runtime.env",
    )

    assert settings["llm_api_key"] == "env-llm-key"
    assert settings["llm_base_url"] == "https://llm.example/v1"
    assert settings["llm_model"] == "gpt-test"
    assert settings["vector_retrieval_enabled"] is False
    assert settings["rerank_enabled"] is False
    assert settings["qdrant_url"] == "http://qdrant:6333"


def test_validate_startup_settings_requires_optional_fields_when_enabled():
    errors = validate_startup_settings(
        {
            "llm_api_key": "llm-key",
            "llm_base_url": "https://llm.example/v1",
            "llm_model": "gpt-test",
            "vector_retrieval_enabled": True,
            "embed_api_key": "",
            "embed_base_url": "",
            "embed_model": "",
            "rerank_enabled": True,
            "rerank_provider": "local",
            "rerank_base_url": "",
            "rerank_api_key": "",
            "rerank_model": "",
        }
    )

    assert any("EMBED_API_KEY" in item for item in errors)
    assert any("EMBED_BASE_URL" in item for item in errors)
    assert any("EMBED_MODEL" in item for item in errors)
    assert any("RERANK_BASE_URL" in item for item in errors)
    assert any("RERANK_MODEL" in item for item in errors)
    assert all("RERANK_API_KEY" not in item for item in errors)


def test_write_startup_settings_persists_managed_runtime_env(tmp_path):
    target = tmp_path / "runtime.env"

    saved_path = write_startup_settings(
        {
            "llm_api_key": "llm-key",
            "llm_base_url": "https://llm.example/v1",
            "llm_model": "gpt-test",
            "vector_retrieval_enabled": True,
            "embed_api_key": "embed-key",
            "embed_base_url": "https://embed.example/v1",
            "embed_model": "embed-model",
            "qdrant_url": "http://qdrant:6333",
            "rerank_enabled": False,
            "rerank_provider": "local",
            "rerank_base_url": "http://rerank-local:8000/rerank",
            "rerank_api_key": "",
            "rerank_model": "BAAI/bge-reranker-v2-m3",
            "prewarm_enabled": True,
        },
        path=target,
    )

    content = saved_path.read_text(encoding="utf-8")
    assert saved_path == target
    assert "LLM_API_KEY=llm-key" in content
    assert "HYBRID_DENSE_RETRIEVAL_ENABLED=1" in content
    assert "EMBED_MODEL=embed-model" in content
    assert "RERANK_DISABLED=1" in content
    assert "RERANK_PROVIDER=local" in content
    assert "AGENT_RUNTIME_PREWARM_ENABLED=1" in content


def test_initialize_app_opens_startup_setup_and_loads_books(monkeypatch):
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
            "llm_api_key": "",
            "llm_base_url": "",
            "llm_model": "",
            "vector_retrieval_enabled": False,
            "embed_api_key": "",
            "embed_base_url": "",
            "embed_model": "",
            "qdrant_url": "http://qdrant:6333",
            "rerank_enabled": False,
            "rerank_provider": "local",
            "rerank_base_url": "http://rerank-local:8000/rerank",
            "rerank_api_key": "",
            "rerank_model": "BAAI/bge-reranker-v2-m3",
            "prewarm_enabled": False,
        },
    )

    state.initialize_app()

    assert state.page_mode == "startup_setup"
    assert len(state.uploaded_books) == 1
    assert state.uploaded_books[0].title == "向导测试"


def test_enter_bookshelf_from_startup_setup_routes_back_to_bookshelf(monkeypatch):
    import rag.uploadBook as upload_book
    import reflex_app.state as state_module

    state = NovelState()
    state.page_mode = "startup_setup"
    monkeypatch.setattr(upload_book, "recover_interrupted_book_statuses", lambda: 0)
    monkeypatch.setattr(
        upload_book,
        "list_books",
        lambda: [{"id": 3, "title": "书架入口", "author": "作者", "cover_url": "", "total_chapters": 2, "status": "completed"}],
    )
    monkeypatch.setattr(state_module, "_BOOK_STATUS_RECOVERY_DONE", False)

    state.enter_bookshelf()

    assert state.page_mode == "bookshelf"
    assert len(state.uploaded_books) == 1
    assert state.uploaded_books[0].title == "书架入口"


def test_startup_setup_view_builds():
    startup_setup_view()
