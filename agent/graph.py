from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import threading
import time
from typing import Annotated, Any, TypedDict

import httpx
from dotenv import load_dotenv

from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool, tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

logger = logging.getLogger(__name__)

load_dotenv()

ENV_OVERRIDE_VAR = "STORY2MEMORY_ENV_OVERRIDE"
override_path = os.getenv(ENV_OVERRIDE_VAR)
if override_path:
    loaded = load_dotenv(override_path, override=True)
    if not loaded:
        logger.warning("Env override file not found: %s=%s", ENV_OVERRIDE_VAR, override_path)


class AgentState(TypedDict):
    """Graph state shared across nodes."""

    messages: Annotated[list[BaseMessage], add_messages]


_REQUEST_ID_LOCAL = threading.local()
_METRICS_LOCK = threading.Lock()
_REQUEST_METRICS: dict[str, dict[str, int]] = {}
LLM_RATE_LIMIT_MAX_RETRIES_ENV_VAR = "LLM_RATE_LIMIT_MAX_RETRIES"
LLM_SDK_MAX_RETRIES_ENV_VAR = "LLM_SDK_MAX_RETRIES"
LLM_GLOBAL_MAX_CONCURRENCY_ENV_VAR = "LLM_GLOBAL_MAX_CONCURRENCY"
LLM_TRUST_ENV_PROXY_ENV_VAR = "LLM_TRUST_ENV_PROXY"
_LLM_HTTP_CLIENT: httpx.Client | None = None
_LLM_HTTP_ASYNC_CLIENT: httpx.AsyncClient | None = None
_LLM_THROTTLE_LOCK = threading.Lock()
_LLM_GLOBAL_COOLDOWN_UNTIL_MONOTONIC = 0.0
_LLM_GLOBAL_COOLDOWN_LEVEL = 0
_LLM_GLOBAL_SEMAPHORE: threading.BoundedSemaphore | None = None
_LLM_GLOBAL_SEMAPHORE_LIMIT: int | None = None
_RATE_LIMIT_DELAY_WINDOWS: tuple[tuple[float, float], ...] = (
    (3.0, 5.0),
    (10.0, 15.0),
    (25.0, 30.0),
    (45.0, 60.0),
)


def set_active_request_id(request_id: str) -> None:
    setattr(_REQUEST_ID_LOCAL, "request_id", request_id or "")


def get_active_request_id() -> str:
    return str(getattr(_REQUEST_ID_LOCAL, "request_id", "") or "")


def clear_active_request_id() -> None:
    setattr(_REQUEST_ID_LOCAL, "request_id", "")


def start_request_metrics(request_id: str) -> None:
    normalized = (request_id or "").strip()
    if not normalized:
        return
    with _METRICS_LOCK:
        _REQUEST_METRICS[normalized] = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    set_active_request_id(normalized)


def consume_request_metrics(request_id: str) -> dict[str, int]:
    normalized = (request_id or "").strip()
    if not normalized:
        return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    with _METRICS_LOCK:
        metrics = dict(_REQUEST_METRICS.pop(normalized, {"calls": 0, "input_tokens": 0, "output_tokens": 0}))
    metrics["total_tokens"] = int(metrics.get("input_tokens", 0)) + int(metrics.get("output_tokens", 0))
    return metrics


def _record_api_usage(request_id: str, input_tokens: int, output_tokens: int) -> None:
    normalized = (request_id or "").strip()
    if not normalized:
        return
    with _METRICS_LOCK:
        metrics = _REQUEST_METRICS.setdefault(
            normalized,
            {"calls": 0, "input_tokens": 0, "output_tokens": 0},
        )
        metrics["calls"] += 1
        metrics["input_tokens"] += max(0, int(input_tokens))
        metrics["output_tokens"] += max(0, int(output_tokens))


def _stringify_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                stripped = item.strip()
                if stripped:
                    parts.append(stripped)
                continue
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
                continue
            serialized = str(item).strip()
            if serialized:
                parts.append(serialized)
        return "\n".join(parts)
    return str(content).strip()


def _estimate_tokens(text: str) -> int:
    normalized = text.strip()
    if not normalized:
        return 0
    return max(1, len(normalized) // 4)


def _estimate_input_tokens(payload: Any) -> int:
    if isinstance(payload, str):
        return _estimate_tokens(payload)
    if isinstance(payload, list):
        total = 0
        for item in payload:
            content = getattr(item, "content", None)
            if content is None and isinstance(item, dict):
                content = item.get("content")
            if content is None and isinstance(item, tuple) and len(item) >= 2:
                content = item[1]
            total += _estimate_tokens(_stringify_content(content if content is not None else item))
        return total
    return _estimate_tokens(_stringify_content(payload))


def _estimate_output_tokens(response: Any) -> int:
    content = getattr(response, "content", response)
    text = _stringify_content(content)
    tool_calls = getattr(response, "tool_calls", None)
    if tool_calls:
        try:
            text = f"{text}\n{json.dumps(tool_calls, ensure_ascii=False)}".strip()
        except Exception:
            pass
    return _estimate_tokens(text)


def _resolve_rate_limit_max_retries() -> int:
    def _as_int(env_name: str, default: int) -> int:
        raw = str(os.getenv(env_name, "")).strip()
        if not raw:
            return default
        try:
            return max(0, int(raw))
        except ValueError:
            return default

    return _as_int(LLM_RATE_LIMIT_MAX_RETRIES_ENV_VAR, 6)


def _is_rate_limit_error(exc: Exception) -> bool:
    visited: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        name = current.__class__.__name__
        message = str(current)
        lowered = message.lower()
        if name == "RateLimitError":
            return True
        if "accountratelimitexceeded" in lowered:
            return True
        if "toomanyrequests" in lowered:
            return True
        if "rate limit" in lowered:
            return True
        if "error code: 429" in lowered:
            return True
        current = current.__cause__ or current.__context__
    return False


def _resolve_sdk_max_retries() -> int:
    raw = str(os.getenv(LLM_SDK_MAX_RETRIES_ENV_VAR, "")).strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _resolve_global_llm_max_concurrency() -> int:
    raw = str(os.getenv(LLM_GLOBAL_MAX_CONCURRENCY_ENV_VAR, "")).strip()
    if not raw:
        return 25
    try:
        return max(1, int(raw))
    except ValueError:
        return 25


def _retry_delay_window(attempt: int) -> tuple[float, float]:
    normalized = max(0, int(attempt))
    index = min(normalized, len(_RATE_LIMIT_DELAY_WINDOWS) - 1)
    return _RATE_LIMIT_DELAY_WINDOWS[index]


def _compute_retry_delay(attempt: int, base_delay: float, max_delay: float, jitter: float) -> float:
    del base_delay, max_delay, jitter
    start, end = _retry_delay_window(attempt)
    return random.uniform(start, end)


def _reset_llm_throttle_state_for_tests() -> None:
    global _LLM_GLOBAL_COOLDOWN_UNTIL_MONOTONIC, _LLM_GLOBAL_COOLDOWN_LEVEL
    global _LLM_GLOBAL_SEMAPHORE, _LLM_GLOBAL_SEMAPHORE_LIMIT
    with _LLM_THROTTLE_LOCK:
        _LLM_GLOBAL_COOLDOWN_UNTIL_MONOTONIC = 0.0
        _LLM_GLOBAL_COOLDOWN_LEVEL = 0
        _LLM_GLOBAL_SEMAPHORE = None
        _LLM_GLOBAL_SEMAPHORE_LIMIT = None


def _get_global_cooldown_remaining() -> float:
    with _LLM_THROTTLE_LOCK:
        remaining = _LLM_GLOBAL_COOLDOWN_UNTIL_MONOTONIC - time.monotonic()
    return max(0.0, remaining)


def _register_global_rate_limit_cooldown() -> float:
    global _LLM_GLOBAL_COOLDOWN_UNTIL_MONOTONIC, _LLM_GLOBAL_COOLDOWN_LEVEL
    with _LLM_THROTTLE_LOCK:
        now = time.monotonic()
        if now >= _LLM_GLOBAL_COOLDOWN_UNTIL_MONOTONIC:
            _LLM_GLOBAL_COOLDOWN_LEVEL = 0
        start, end = _retry_delay_window(_LLM_GLOBAL_COOLDOWN_LEVEL)
        cooldown_delay = random.uniform(start, end)
        _LLM_GLOBAL_COOLDOWN_UNTIL_MONOTONIC = max(_LLM_GLOBAL_COOLDOWN_UNTIL_MONOTONIC, now + cooldown_delay)
        if _LLM_GLOBAL_COOLDOWN_LEVEL < len(_RATE_LIMIT_DELAY_WINDOWS) - 1:
            _LLM_GLOBAL_COOLDOWN_LEVEL += 1
        return max(0.0, _LLM_GLOBAL_COOLDOWN_UNTIL_MONOTONIC - now)


def _env_flag_enabled(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _get_global_llm_semaphore() -> threading.BoundedSemaphore:
    global _LLM_GLOBAL_SEMAPHORE, _LLM_GLOBAL_SEMAPHORE_LIMIT
    limit = _resolve_global_llm_max_concurrency()
    with _LLM_THROTTLE_LOCK:
        if _LLM_GLOBAL_SEMAPHORE is None or _LLM_GLOBAL_SEMAPHORE_LIMIT != limit:
            _LLM_GLOBAL_SEMAPHORE = threading.BoundedSemaphore(limit)
            _LLM_GLOBAL_SEMAPHORE_LIMIT = limit
        return _LLM_GLOBAL_SEMAPHORE


def _wait_for_global_cooldown_sync() -> None:
    while True:
        remaining = _get_global_cooldown_remaining()
        if remaining <= 0:
            return
        time.sleep(remaining)


async def _wait_for_global_cooldown_async() -> None:
    while True:
        remaining = _get_global_cooldown_remaining()
        if remaining <= 0:
            return
        await asyncio.sleep(remaining)


def _acquire_global_llm_slot_sync() -> threading.BoundedSemaphore:
    while True:
        _wait_for_global_cooldown_sync()
        semaphore = _get_global_llm_semaphore()
        semaphore.acquire()
        remaining = _get_global_cooldown_remaining()
        if remaining <= 0:
            return semaphore
        semaphore.release()


async def _acquire_global_llm_slot_async() -> threading.BoundedSemaphore:
    while True:
        await _wait_for_global_cooldown_async()
        semaphore = _get_global_llm_semaphore()
        await asyncio.to_thread(semaphore.acquire)
        remaining = _get_global_cooldown_remaining()
        if remaining <= 0:
            return semaphore
        semaphore.release()


def apply_llm_network_settings(kwargs: dict[str, Any]) -> dict[str, Any]:
    kwargs.setdefault("max_retries", _resolve_sdk_max_retries())
    if _env_flag_enabled(LLM_TRUST_ENV_PROXY_ENV_VAR, default=False):
        return kwargs

    global _LLM_HTTP_CLIENT, _LLM_HTTP_ASYNC_CLIENT
    if _LLM_HTTP_CLIENT is None:
        _LLM_HTTP_CLIENT = httpx.Client(trust_env=False)
    if _LLM_HTTP_ASYNC_CLIENT is None:
        _LLM_HTTP_ASYNC_CLIENT = httpx.AsyncClient(trust_env=False)
    kwargs["http_client"] = _LLM_HTTP_CLIENT
    kwargs["http_async_client"] = _LLM_HTTP_ASYNC_CLIENT
    return kwargs


class TrackedLLM:
    def __init__(self, runnable: Any):
        self._runnable = runnable

    def bind_tools(self, tools: list[BaseTool]) -> Any:
        bound = self._runnable.bind_tools(tools)
        return TrackedLLM(bound)

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        request_id = get_active_request_id()
        input_tokens = _estimate_input_tokens(input)
        max_retries = _resolve_rate_limit_max_retries()
        for attempt in range(max_retries + 1):
            semaphore = _acquire_global_llm_slot_sync()
            try:
                try:
                    if config is None:
                        response = self._runnable.invoke(input, **kwargs)
                    else:
                        response = self._runnable.invoke(input, config=config, **kwargs)
                except Exception as exc:
                    if not _is_rate_limit_error(exc) or attempt >= max_retries:
                        raise
                    retry_delay = _compute_retry_delay(attempt, base_delay=0.0, max_delay=0.0, jitter=0.0)
                    global_delay = _register_global_rate_limit_cooldown()
                    delay = max(retry_delay, global_delay)
                    logger.warning(
                        "[LLM] rate limit encountered invoke attempt=%d/%d delay_sec=%.2f error=%s",
                        attempt + 1,
                        max_retries + 1,
                        delay,
                        exc,
                    )
                else:
                    output_tokens = _estimate_output_tokens(response)
                    _record_api_usage(request_id, input_tokens, output_tokens)
                    return response
            except Exception:
                raise
            finally:
                semaphore.release()
            time.sleep(delay)
        raise RuntimeError("unreachable")

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        request_id = get_active_request_id()
        input_tokens = _estimate_input_tokens(input)
        max_retries = _resolve_rate_limit_max_retries()
        for attempt in range(max_retries + 1):
            semaphore = await _acquire_global_llm_slot_async()
            try:
                try:
                    if config is None:
                        response = await self._runnable.ainvoke(input, **kwargs)
                    else:
                        response = await self._runnable.ainvoke(input, config=config, **kwargs)
                except Exception as exc:
                    if not _is_rate_limit_error(exc) or attempt >= max_retries:
                        raise
                    retry_delay = _compute_retry_delay(attempt, base_delay=0.0, max_delay=0.0, jitter=0.0)
                    global_delay = _register_global_rate_limit_cooldown()
                    delay = max(retry_delay, global_delay)
                    logger.warning(
                        "[LLM] rate limit encountered ainvoke attempt=%d/%d delay_sec=%.2f error=%s",
                        attempt + 1,
                        max_retries + 1,
                        delay,
                        exc,
                    )
                else:
                    output_tokens = _estimate_output_tokens(response)
                    _record_api_usage(request_id, input_tokens, output_tokens)
                    return response
            except Exception:
                raise
            finally:
                semaphore.release()
            await asyncio.sleep(delay)
        raise RuntimeError("unreachable")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runnable, name)


def wrap_tracked_llm(runnable: Any) -> TrackedLLM:
    return TrackedLLM(runnable)


@tool
def get_weather(location: str) -> str:
    """Placeholder tool for current ReAct loop."""
    return f"The weather in {location} is sunny (placeholder)."


def build_tools() -> list[BaseTool]:
    """Build tool registry; future RAG/Graph tools can be appended here."""
    return [get_weather]


def build_llm(model_name_override: str | None = None) -> ChatOpenAI:
    """Build a ChatOpenAI-compatible model from environment variables."""
    api_key = os.getenv("LLM_API_KEY")
    if not api_key:
        raise ValueError("Missing LLM_API_KEY environment variable.")

    base_url = os.getenv("LLM_BASE_URL")
    model_name = model_name_override or os.getenv("LLM_MODEL", "deepseek-v3.2")

    kwargs: dict[str, Any] = {
        "model": model_name,
        "api_key": api_key,
        "temperature": 0.3,
    }
    if base_url:
        kwargs["base_url"] = base_url
    apply_llm_network_settings(kwargs)

    return wrap_tracked_llm(ChatOpenAI(**kwargs))

def compile_graph() -> Any:
    """Compile and return a LangGraph chat application."""

    tools = build_tools()
    llm = build_llm()

    graph_builder: StateGraph[AgentState] = StateGraph(AgentState)

    llm_with_tools = None
    if tools:
        try:
            llm_with_tools = llm.bind_tools(tools)
        except Exception:
            logger.exception("Failed to bind tools. Falling back to plain chat mode.")

    if llm_with_tools is None:
        def chatbot(state: AgentState) -> AgentState:
            response = llm.invoke(state["messages"])
            return {"messages": [response]}

        graph_builder.add_node("chatbot", chatbot)
        graph_builder.set_entry_point("chatbot")
        graph_builder.add_edge("chatbot", END)
        return graph_builder.compile()

    def chatbot(state: AgentState) -> AgentState:
        response = llm_with_tools.invoke(state["messages"])
        return {"messages": [response]}

    graph_builder.add_node("chatbot", chatbot)
    graph_builder.add_node("tools", ToolNode(tools))
    graph_builder.set_entry_point("chatbot")
    graph_builder.add_conditional_edges(
        "chatbot",
        tools_condition,
        {"tools": "tools", "__end__": END},
    )
    graph_builder.add_edge("tools", "chatbot")
    return graph_builder.compile()
