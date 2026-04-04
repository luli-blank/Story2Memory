from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent.graph as graph


class _FakeChatOpenAI:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_build_llm_disables_env_proxy_by_default(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_TRUST_ENV_PROXY", raising=False)
    monkeypatch.setattr(graph, "ChatOpenAI", _FakeChatOpenAI)
    monkeypatch.setattr(graph, "wrap_tracked_llm", lambda runnable: runnable)

    llm = graph.build_llm()

    assert "http_client" in llm.kwargs
    assert "http_async_client" in llm.kwargs
    assert llm.kwargs["http_client"]._trust_env is False
    assert llm.kwargs["http_async_client"]._trust_env is False


def test_build_llm_can_explicitly_trust_env_proxy(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_TRUST_ENV_PROXY", "1")
    monkeypatch.setattr(graph, "ChatOpenAI", _FakeChatOpenAI)
    monkeypatch.setattr(graph, "wrap_tracked_llm", lambda runnable: runnable)

    llm = graph.build_llm()

    assert "http_client" not in llm.kwargs
    assert "http_async_client" not in llm.kwargs


def test_compute_retry_delay_uses_fixed_random_window(monkeypatch):
    monkeypatch.setattr(graph.random, "uniform", lambda start, end: 7.5)

    first_attempt = graph._compute_retry_delay(attempt=0, base_delay=60.0, max_delay=1800.0, jitter=0.0)
    later_attempt = graph._compute_retry_delay(attempt=5, base_delay=60.0, max_delay=1800.0, jitter=0.0)

    assert first_attempt == 7.5
    assert later_attempt == 7.5
