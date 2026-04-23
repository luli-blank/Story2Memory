import threading
import time
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
    assert llm.kwargs["max_retries"] == 0


def test_build_llm_can_explicitly_trust_env_proxy(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_TRUST_ENV_PROXY", "1")
    monkeypatch.setattr(graph, "ChatOpenAI", _FakeChatOpenAI)
    monkeypatch.setattr(graph, "wrap_tracked_llm", lambda runnable: runnable)

    llm = graph.build_llm()

    assert "http_client" not in llm.kwargs
    assert "http_async_client" not in llm.kwargs
    assert llm.kwargs["max_retries"] == 0


def test_compute_retry_delay_uses_progressive_windows(monkeypatch):
    ranges: list[tuple[float, float]] = []

    def _fake_uniform(start: float, end: float) -> float:
        ranges.append((start, end))
        return end

    monkeypatch.setattr(graph.random, "uniform", _fake_uniform)

    delays = [
        graph._compute_retry_delay(attempt=0, base_delay=60.0, max_delay=1800.0, jitter=0.0),
        graph._compute_retry_delay(attempt=1, base_delay=60.0, max_delay=1800.0, jitter=0.0),
        graph._compute_retry_delay(attempt=2, base_delay=60.0, max_delay=1800.0, jitter=0.0),
        graph._compute_retry_delay(attempt=3, base_delay=60.0, max_delay=1800.0, jitter=0.0),
        graph._compute_retry_delay(attempt=9, base_delay=60.0, max_delay=1800.0, jitter=0.0),
    ]

    assert ranges == [(3.0, 5.0), (10.0, 15.0), (25.0, 30.0), (45.0, 60.0), (45.0, 60.0)]
    assert delays == [5.0, 15.0, 30.0, 60.0, 60.0]


def test_register_global_rate_limit_cooldown_escalates_and_resets(monkeypatch):
    graph._reset_llm_throttle_state_for_tests()
    now = {"value": 100.0}
    ranges: list[tuple[float, float]] = []

    monkeypatch.setattr(graph.time, "monotonic", lambda: now["value"])

    def _fake_uniform(start: float, end: float) -> float:
        ranges.append((start, end))
        return end

    monkeypatch.setattr(graph.random, "uniform", _fake_uniform)

    first = graph._register_global_rate_limit_cooldown()
    second = graph._register_global_rate_limit_cooldown()
    now["value"] = 200.0
    third = graph._register_global_rate_limit_cooldown()

    assert first == 5.0
    assert second == 15.0
    assert third == 5.0
    assert ranges == [(3.0, 5.0), (10.0, 15.0), (3.0, 5.0)]


def test_tracked_llm_retries_rate_limits_without_probe(monkeypatch):
    graph._reset_llm_throttle_state_for_tests()
    now = {"value": 0.0}
    sleeps: list[float] = []

    monkeypatch.setattr(graph.time, "monotonic", lambda: now["value"])

    def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now["value"] += seconds

    monkeypatch.setattr(graph.time, "sleep", _fake_sleep)
    monkeypatch.setattr(graph.random, "uniform", lambda start, end: end)

    class _FakeRunnable:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, input, **kwargs):
            del input, kwargs
            self.calls += 1
            if self.calls < 3:
                raise Exception("Error code: 429 - {'error': {'type': 'TooManyRequests'}}")
            return type("Response", (), {"content": "ok"})()

    runnable = _FakeRunnable()

    response = graph.TrackedLLM(runnable).invoke("hello")

    assert response.content == "ok"
    assert runnable.calls == 3
    assert sleeps == [5.0, 15.0]


def test_tracked_llm_respects_global_concurrency_limit(monkeypatch):
    monkeypatch.setenv("LLM_GLOBAL_MAX_CONCURRENCY", "1")
    graph._reset_llm_throttle_state_for_tests()

    class _BlockingRunnable:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.active = 0
            self.max_active = 0

        def invoke(self, input, **kwargs):
            del input, kwargs
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.05)
                return type("Response", (), {"content": "ok"})()
            finally:
                with self.lock:
                    self.active -= 1

    llm = graph.TrackedLLM(_BlockingRunnable())
    threads = [threading.Thread(target=llm.invoke, args=("hello",)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert llm._runnable.max_active == 1
