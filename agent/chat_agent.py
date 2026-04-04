from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import logging
import re
import threading
import time
from typing import Mapping, Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from agent.prompt import SYSTEM_PROMPT, CONTENT_SEARCH_REWRITE_PROMPT, CONTENT_SEARCH_ROUTER_PROMPT
from agent.graph import (
    build_llm,
    clear_active_request_id,
    compile_graph,
    consume_request_metrics,
    start_request_metrics,
)
from agent.searchAgent import contentSearch, warm_search_runtime
from database.mysql_client import MySQLChatStore
from database.session_keys import build_qa_session_info

DEFAULT_HISTORY_WINDOW = 12
RECENT_MESSAGES_LIMIT = 5
SUMMARY_UPDATE_STEP = 10
RETRY_SEARCH_MARKERS = (
    "重新搜索",
    "重新检索",
    "重新搜",
    "重搜",
    "重查",
    "重新查",
    "记忆是错的",
    "记忆错了",
    "你记错了",
    "回答错了",
    "不是这个",
    "上一轮",
    "上一题",
    "上一个问题",
    "刚才那个问题",
)
FORCE_SEARCH_MARKERS = (
    "谁",
    "什么",
    "为什么",
    "为何",
    "过程",
    "剧情",
    "身份",
    "能力",
    "背景",
    "结局",
    "最后",
    "章节",
    "原文",
    "对白",
    "关系",
    "出现",
    "是不是",
    "有没有",
)
CASUAL_SKIP_MARKERS = ("你好", "谢谢", "再见", "在吗", "你是谁", "帮我", "总结一下聊天")

logger = logging.getLogger(__name__)
_PREWARM_THREAD_LOCK = threading.Lock()
_PREWARM_BOOK_IDS: set[int] = set()
_PREWARM_STARTED = False


@dataclass(frozen=True)
class ChatTurn:
    role: str
    content: str


class ChatAgent:
    """A reusable conversational wrapper around the compiled LangGraph app."""

    def __init__(self, history_window: int = DEFAULT_HISTORY_WINDOW):
        self.history_window = max(0, history_window)
        self._graph = compile_graph()
        self._llm = build_llm()
        self._invoke_lock = threading.Lock()
        self._store = MySQLChatStore()
        self._last_search_packet_by_session: dict[str, dict[str, object]] = {}
        self._tool_table: dict[str, BaseTool] = {
            contentSearch.name: contentSearch,
        }
        try:
            self._tool_router = self._llm.bind_tools(list(self._tool_table.values()))
        except Exception:
            logger.exception("Failed to bind retrieval tools. Falling back to graph-only chat mode.")
            self._tool_router = None

    def reply(
        self,
        user_input: str,
        *,
        book_id: int = 0,
        novel_title: str = "",
        chat_history: Sequence[Mapping[str, str] | ChatTurn] | None = None,
    ) -> str:
        request_id = f"agent-{int(time.time() * 1000)}"
        started_at = time.perf_counter()
        text = user_input.strip()
        if not text:
            return "请输入问题。"

        start_request_metrics(request_id)
        try:
            session_id, user_id, session_title = self._resolve_session_info(
                novel_title=novel_title,
                book_id=book_id,
            )
            use_store = self._store.ensure_session(
                session_id,
                user_id,
                session_title,
                book_id=int(book_id or 0) or None,
                session_kind="qa",
            )
            logger.info(
                "[ChatAgent][%s] start. session_id=%s use_store=%s novel_title=%s chars=%d",
                request_id,
                session_id,
                use_store,
                novel_title.strip() or "GLOBAL",
                len(text),
            )
            summary = self._store.get_summary(session_id) if use_store else ""
            recent_turns = self._store.get_recent_messages(session_id, RECENT_MESSAGES_LIMIT) if use_store else None

            messages = self._build_messages(
                user_input=text,
                novel_title=novel_title,
                summary=summary,
                recent_turns=recent_turns,
                chat_history=chat_history,
            )
            context_turns = recent_turns if recent_turns is not None else chat_history
            search_agent_elapsed_sec: float | None = None
            search_tool_name = ""
            try:
                reply, search_agent_elapsed_sec, search_tool_name = self._reply_with_content_search(
                    user_input=text,
                    novel_title=novel_title,
                    summary=summary,
                    chat_history=context_turns,
                    request_id=request_id,
                    session_id=session_id,
                )
            except Exception:
                logger.exception("[ChatAgent][%s] contentSearch route failed. fallback=graph", request_id)
                reply = None
            reply_text = str(reply).strip() if reply is not None else ""
            if reply_text:
                reply = reply_text
                total_tokens = self._estimate_token_count(reply)
                search_elapsed_text = (
                    f"{search_agent_elapsed_sec:.3f}" if search_agent_elapsed_sec is not None else "N/A"
                )
                logger.info(
                    "[ChatAgent][%s] contentSearch path used. reply_chars=%d elapsed_sec=%.3f search_tool=%s search_agent_total_elapsed_sec=%s",
                    request_id,
                    len(reply),
                    time.perf_counter() - started_at,
                    search_tool_name or "N/A",
                    search_elapsed_text,
                )
            else:
                self._last_search_packet_by_session.pop(session_id, None)
                with self._invoke_lock:
                    logger.info("[ChatAgent][%s] invoking fallback graph.", request_id)
                    result = self._graph.invoke({"messages": messages})

                result_messages = result.get("messages", [])
                reply = self._extract_reply(result_messages)
                total_tokens = self._extract_total_tokens(result_messages)
                if not reply:
                    reply = "我暂时没有生成有效回复，请换个问法试试。"
                logger.info(
                    "[ChatAgent][%s] fallback graph finished. reply_chars=%d tokens=%d elapsed_sec=%.3f",
                    request_id,
                    len(reply),
                    total_tokens,
                    time.perf_counter() - started_at,
                )

            if use_store:
                self._store.append_message(
                    session_id=session_id,
                    role="user",
                    content=text,
                    token_count=0,
                )
                self._store.append_message(
                    session_id=session_id,
                    role="assistant",
                    content=reply,
                    token_count=total_tokens,
                )
                self._trigger_summary_update(session_id, novel_title)
                logger.info("[ChatAgent][%s] persisted messages into store.", request_id)

            search_elapsed_text = (
                f"{search_agent_elapsed_sec:.3f}" if search_agent_elapsed_sec is not None else "N/A"
            )
            logger.info(
                "[ChatAgent][%s] done. total_elapsed_sec=%.3f search_agent_total_elapsed_sec=%s",
                request_id,
                time.perf_counter() - started_at,
                search_elapsed_text,
            )
            return reply
        finally:
            metrics = consume_request_metrics(request_id)
            clear_active_request_id()
            logger.info(
                "[ChatAgent][%s] API summary: calls=%d input_tokens=%d output_tokens=%d total_tokens=%d",
                request_id,
                metrics["calls"],
                metrics["input_tokens"],
                metrics["output_tokens"],
                metrics["total_tokens"],
            )

    def _build_messages(
        self,
        *,
        user_input: str,
        novel_title: str,
        summary: str = "",
        recent_turns: Sequence[Mapping[str, str] | ChatTurn] | None = None,
        chat_history: Sequence[Mapping[str, str] | ChatTurn] | None = None,
    ) -> list[BaseMessage]:
        turns = self._normalize_turns(recent_turns if recent_turns is not None else chat_history)
        if self.history_window and recent_turns is None:
            turns = turns[-self.history_window :]

        messages: list[BaseMessage] = [
            SystemMessage(content=self._build_system_prompt(novel_title, summary=summary))
        ]

        for turn in turns:
            lc_message = self._to_langchain_message(turn)
            if lc_message is not None:
                messages.append(lc_message)

        if not turns or turns[-1].role != "user" or turns[-1].content.strip() != user_input:
            messages.append(HumanMessage(content=user_input))

        return messages

    def _reply_with_content_search(
        self,
        *,
        user_input: str,
        novel_title: str,
        summary: str = "",
        chat_history: Sequence[Mapping[str, str] | ChatTurn] | None = None,
        request_id: str = "",
        session_id: str = "",
    ) -> tuple[str | None, float | None, str]:
        if self._tool_router is None:
            logger.info("[ChatAgent][%s] tool_router unavailable, skip contentSearch.", request_id)
            return None, None, ""
        if not novel_title.strip():
            logger.info("[ChatAgent][%s] no novel_title, skip contentSearch.", request_id)
            return None, None, ""

        history_text = self._format_turns_for_prompt(chat_history)
        summary_text = summary.strip() or "暂无历史摘要。"
        router_input = (
            f"当前书籍：{novel_title}\n"
            f"历史摘要：{summary_text}\n"
            f"最近对话：\n{history_text}\n\n"
            f"用户最新问题：{user_input}\n"
            "请判断是否需要调用检索工具。"
        )
        with self._invoke_lock:
            logger.info("[ChatAgent][%s] invoking tool router.", request_id)
            route_result = self._tool_router.invoke(
                [
                    SystemMessage(content=CONTENT_SEARCH_ROUTER_PROMPT),
                    HumanMessage(content=router_input),
                ]
            )

        tool_calls = getattr(route_result, "tool_calls", None) or []
        if not tool_calls:
            if self._should_force_content_search(user_input):
                logger.info("[ChatAgent][%s] router skipped tool, force contentSearch by heuristic.", request_id)
                tool = self._tool_table.get(contentSearch.name)
                if tool is not None:
                    args = {
                        "query": self._resolve_search_query(
                            user_input=user_input,
                            router_query="",
                            summary=summary,
                            chat_history=chat_history,
                        ),
                        "novel_title": novel_title,
                        "request_id": request_id,
                    }
                    tool_started_at = time.perf_counter()
                    tool_output = tool.invoke(args)
                    tool_elapsed_sec = time.perf_counter() - tool_started_at
                    tool_text = self._stringify_content(tool_output)
                    if tool_text:
                        packet = self._extract_search_packet(tool_text)
                        if packet is not None:
                            if session_id:
                                self._last_search_packet_by_session[session_id] = dict(packet)
                            rewritten = self._rewrite_search_packet(
                                user_input=user_input,
                                novel_title=novel_title,
                                summary=summary,
                                chat_history=chat_history,
                                packet=packet,
                                request_id=request_id,
                            )
                            if rewritten:
                                return rewritten, tool_elapsed_sec, contentSearch.name
                            fallback_answer = self._fallback_search_packet_answer(packet)
                            if fallback_answer:
                                return fallback_answer, tool_elapsed_sec, contentSearch.name
                        return tool_text, tool_elapsed_sec, contentSearch.name
            logger.info("[ChatAgent][%s] router decided no tool call.", request_id)
            return None, None, ""

        tool_call = tool_calls[0]
        tool_name = str(tool_call.get("name", "") or "")
        tool = self._tool_table.get(tool_name)
        if tool is None:
            logger.warning("[ChatAgent][%s] router selected unknown tool=%s", request_id, tool_name)
            return None, None, tool_name

        raw_args = tool_call.get("args", {}) or {}
        args = dict(raw_args) if isinstance(raw_args, Mapping) else {}
        args["query"] = self._resolve_search_query(
            user_input=user_input,
            router_query=args.get("query", ""),
            summary=summary,
            chat_history=chat_history,
        )
        args["novel_title"] = novel_title
        args["request_id"] = request_id
        logger.info(
            "[ChatAgent][%s] invoking tool=%s args=%s",
            request_id,
            tool_name,
            args,
        )

        tool_started_at = time.perf_counter()
        tool_output = tool.invoke(args)
        tool_elapsed_sec = time.perf_counter() - tool_started_at
        tool_text = self._stringify_content(tool_output)
        if not tool_text:
            logger.warning("[ChatAgent][%s] tool=%s returned empty response.", request_id, tool_name)
            return None, tool_elapsed_sec, tool_name

        if tool_name == contentSearch.name:
            packet = self._extract_search_packet(tool_text)
            if packet is not None:
                if session_id:
                    self._last_search_packet_by_session[session_id] = dict(packet)
                rewritten = self._rewrite_search_packet(
                    user_input=user_input,
                    novel_title=novel_title,
                    summary=summary,
                    chat_history=chat_history,
                    packet=packet,
                    request_id=request_id,
                )
                if rewritten:
                    logger.info(
                        "[ChatAgent][%s] tool=%s packet rewritten. response_chars=%d tool_elapsed_sec=%.3f",
                        request_id,
                        tool_name,
                        len(rewritten),
                        tool_elapsed_sec,
                    )
                    return rewritten, tool_elapsed_sec, tool_name
                fallback_answer = self._fallback_search_packet_answer(packet)
                if fallback_answer:
                    logger.info(
                        "[ChatAgent][%s] tool=%s packet fallback used. response_chars=%d tool_elapsed_sec=%.3f",
                        request_id,
                        tool_name,
                        len(fallback_answer),
                        tool_elapsed_sec,
                    )
                    return fallback_answer, tool_elapsed_sec, tool_name

        # 检索工具返回结果后直接回传，避免在主路径增加额外改写延迟。
        logger.info(
            "[ChatAgent][%s] tool=%s completed. response_chars=%d tool_elapsed_sec=%.3f",
            request_id,
            tool_name,
            len(tool_text),
            tool_elapsed_sec,
        )
        return tool_text, tool_elapsed_sec, tool_name

    def get_last_search_packet(self, novel_title: str = "", *, book_id: int = 0) -> dict[str, object]:
        session_id, _, _ = self._resolve_session_info(
            novel_title=novel_title,
            book_id=book_id,
        )
        return dict(self._last_search_packet_by_session.get(session_id, {}) or {})

    @staticmethod
    def _format_turns_for_prompt(
        chat_history: Sequence[Mapping[str, str] | ChatTurn] | None,
        limit: int = 6,
    ) -> str:
        turns = ChatAgent._normalize_turns(chat_history)
        if not turns:
            return "（无）"

        lines = []
        for turn in turns[-max(1, limit) :]:
            lines.append(f"{turn.role}: {turn.content}")
        return "\n".join(lines)

    @staticmethod
    def _looks_like_retry_search_instruction(text: str) -> bool:
        normalized = re.sub(r"\s+", "", str(text or ""))
        if not normalized:
            return False
        if len(normalized) > 32:
            return False
        return any(marker in normalized for marker in RETRY_SEARCH_MARKERS)

    @staticmethod
    def _should_force_content_search(text: str) -> bool:
        normalized = str(text or "").strip()
        if not normalized:
            return False
        if any(marker in normalized for marker in CASUAL_SKIP_MARKERS):
            return False
        return any(marker in normalized for marker in FORCE_SEARCH_MARKERS) or normalized.endswith(("？", "?"))

    @classmethod
    def _find_latest_contextual_user_query(
        cls,
        chat_history: Sequence[Mapping[str, str] | ChatTurn] | None,
        *,
        current_user_input: str,
    ) -> str:
        normalized_current = str(current_user_input or "").strip()
        for turn in reversed(cls._normalize_turns(chat_history)):
            if turn.role != "user":
                continue
            candidate = turn.content.strip()
            if not candidate or candidate == normalized_current:
                continue
            if cls._looks_like_retry_search_instruction(candidate):
                continue
            return candidate
        return ""

    @classmethod
    def _resolve_search_query(
        cls,
        *,
        user_input: str,
        router_query: object,
        summary: str,
        chat_history: Sequence[Mapping[str, str] | ChatTurn] | None,
    ) -> str:
        del summary
        raw_user_query = str(user_input or "").strip()
        routed_query = cls._stringify_content(router_query).strip()
        contextual_user_query = cls._find_latest_contextual_user_query(
            chat_history,
            current_user_input=raw_user_query,
        )

        if routed_query and not cls._looks_like_retry_search_instruction(routed_query):
            return routed_query
        if cls._looks_like_retry_search_instruction(raw_user_query) and contextual_user_query:
            return contextual_user_query
        if routed_query:
            return routed_query
        return raw_user_query

    @staticmethod
    def _build_system_prompt(novel_title: str, summary: str = "") -> str:
        summary_text = summary.strip() if summary.strip() else "暂无历史摘要。"
        if novel_title:
            return (
                f"{SYSTEM_PROMPT}\n"
                f"当前书籍上下文：{novel_title}。若用户问题与这本书相关，请优先围绕该作品回答。\n"
                f"以下是之前的对话摘要：{summary_text}"
            )
        return (
            f"{SYSTEM_PROMPT}\n"
            "当前未选择书籍上下文。你可以先给通用回答，并提醒用户选择书籍获取更精准分析。\n"
            f"以下是之前的对话摘要：{summary_text}"
        )

    @staticmethod
    def _normalize_turns(
        chat_history: Sequence[Mapping[str, str] | ChatTurn] | None,
    ) -> list[ChatTurn]:
        if not chat_history:
            return []

        normalized: list[ChatTurn] = []
        for turn in chat_history:
            if isinstance(turn, ChatTurn):
                role = turn.role.strip().lower()
                content = turn.content.strip()
            elif isinstance(turn, Mapping):
                role = str(turn.get("role", "")).strip().lower()
                content = str(turn.get("content", "")).strip()
            else:
                continue

            if role in {"user", "assistant", "system"} and content:
                normalized.append(ChatTurn(role=role, content=content))
        return normalized

    @staticmethod
    def _to_langchain_message(turn: ChatTurn) -> BaseMessage | None:
        if turn.role == "assistant":
            return AIMessage(content=turn.content)
        if turn.role == "system":
            return SystemMessage(content=turn.content)
        if turn.role == "user":
            return HumanMessage(content=turn.content)
        return None

    @staticmethod
    def _extract_reply(messages: Sequence[BaseMessage]) -> str:
        for message in reversed(messages):
            if isinstance(message, AIMessage):
                text = ChatAgent._stringify_content(message.content)
                if text:
                    return text
        return ""

    @staticmethod
    def _stringify_content(content: object) -> str:
        if isinstance(content, str):
            return content.strip()

        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item.strip())
                    continue

                if isinstance(item, Mapping):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
            return "\n".join(part for part in parts if part)

        return str(content).strip()

    @staticmethod
    def _extract_search_packet(raw: str) -> dict[str, object] | None:
        payload = str(raw or "").strip()
        if not payload:
            return None
        try:
            parsed = json.loads(payload)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    @staticmethod
    def _fallback_search_packet_answer(packet: Mapping[str, object]) -> str:
        for key in ("answer", "rendered_answer", "draft_answer"):
            value = packet.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _rewrite_search_packet(
        self,
        *,
        user_input: str,
        novel_title: str,
        summary: str,
        chat_history: Sequence[Mapping[str, str] | ChatTurn] | None,
        packet: Mapping[str, object],
        request_id: str,
    ) -> str:
        packet_brief = {
            "status": packet.get("status"),
            "route_type": packet.get("route_type"),
            "confidence": packet.get("confidence"),
            "overall_summary": packet.get("overall_summary"),
            "subqueries": list(packet.get("subqueries", []) or [])[:3],
            "draft_answer": packet.get("draft_answer"),
            "answer": packet.get("answer"),
            "citations": packet.get("citations"),
            "evidence": list(packet.get("evidence", []) or [])[:4],
        }
        history_text = self._format_turns_for_prompt(chat_history)
        summary_text = summary.strip() or "暂无历史摘要。"
        rewrite_input = (
            f"当前书籍：{novel_title or '未指定'}\n"
            f"历史摘要：{summary_text}\n"
            f"最近对话：\n{history_text}\n\n"
            f"用户问题：{user_input}\n"
            f"search_agent_packet_json:\n{json.dumps(packet_brief, ensure_ascii=False)}\n"
        )
        try:
            with self._invoke_lock:
                response = self._llm.invoke(
                    [
                        SystemMessage(content=CONTENT_SEARCH_REWRITE_PROMPT),
                        HumanMessage(content=rewrite_input),
                    ]
                )
            rewritten = self._stringify_content(getattr(response, "content", response))
            return rewritten.strip()
        except Exception:
            logger.exception("[ChatAgent][%s] failed to rewrite search packet.", request_id)
            return ""

    @staticmethod
    def _resolve_session_info(*, novel_title: str, book_id: int = 0) -> tuple[str, str, str]:
        return build_qa_session_info(novel_title=novel_title, book_id=book_id)

    @staticmethod
    def _estimate_token_count(text: str) -> int:
        return max(1, len(text.strip()) // 4)

    @staticmethod
    def _extract_total_tokens(messages: Sequence[BaseMessage]) -> int:
        for message in reversed(messages):
            if not isinstance(message, AIMessage):
                continue

            usage = message.usage_metadata
            if isinstance(usage, Mapping):
                total = usage.get("total_tokens")
                if isinstance(total, int):
                    return max(0, total)
                if isinstance(total, str) and total.isdigit():
                    return int(total)

            response_usage = message.response_metadata.get("token_usage", {})
            if isinstance(response_usage, Mapping):
                total = response_usage.get("total_tokens")
                if isinstance(total, int):
                    return max(0, total)
                if isinstance(total, str) and total.isdigit():
                    return int(total)

        return 0

    def _trigger_summary_update(self, session_id: str, novel_title: str) -> None:
        updater = threading.Thread(
            target=self._update_summary_if_needed,
            args=(session_id, novel_title),
            daemon=True,
        )
        updater.start()

    def _update_summary_if_needed(self, session_id: str, novel_title: str) -> None:
        try:
            last_summarized_msg_id = self._store.get_last_summarized_msg_id(session_id)
            latest_msg_id = self._store.get_latest_message_id(session_id)
            if latest_msg_id - last_summarized_msg_id <= SUMMARY_UPDATE_STEP:
                return

            new_rows = self._store.get_messages_after(session_id, last_summarized_msg_id)
            if not new_rows:
                return

            current_summary = self._store.get_summary(session_id)
            summary_prompt = self._build_summary_prompt(
                novel_title=novel_title,
                current_summary=current_summary,
                new_rows=new_rows,
            )
            summary_messages: list[BaseMessage] = [
                SystemMessage(
                    content=(
                        "你是对话摘要助手。请基于旧摘要和新增对话，输出新的精炼摘要。"
                        "保持事实准确、时间顺序清晰，长度控制在 8-12 句。"
                    )
                ),
                HumanMessage(content=summary_prompt),
            ]

            with self._invoke_lock:
                result = self._graph.invoke({"messages": summary_messages})

            new_summary = self._extract_reply(result.get("messages", []))
            if not new_summary:
                return

            final_msg_id = int(new_rows[-1].get("id", latest_msg_id) or latest_msg_id)
            self._store.update_summary(session_id, new_summary, final_msg_id)
        except Exception:
            logger.exception("Failed to update summary asynchronously: session_id=%s", session_id)
            return

    @staticmethod
    def _build_summary_prompt(
        *,
        novel_title: str,
        current_summary: str,
        new_rows: Sequence[Mapping[str, object]],
    ) -> str:
        title_block = f"书籍上下文：{novel_title}\n" if novel_title else ""
        old_summary = current_summary.strip() or "（无）"
        lines = []
        for row in new_rows:
            role = str(row.get("role", "user"))
            content = str(row.get("content", "")).strip()
            if content:
                lines.append(f"{role}: {content}")
        transcript = "\n".join(lines) if lines else "（无新增对话）"
        return (
            f"{title_block}"
            f"旧摘要：\n{old_summary}\n\n"
            f"新增对话：\n{transcript}\n\n"
            "请输出新的完整摘要。"
        )


@lru_cache(maxsize=1)
def get_chat_agent() -> ChatAgent:
    """Singleton accessor to avoid recompiling graph per request."""
    return ChatAgent()


def _run_agent_runtime_prewarm(book_ids: tuple[int, ...]) -> None:
    try:
        get_chat_agent()
    except Exception:
        logger.exception("Failed to warm outer chat agent runtime.")
    try:
        warm_search_runtime(book_ids)
    except Exception:
        logger.exception("Failed to warm search runtime.")


def schedule_agent_runtime_prewarm(book_ids: Sequence[int] = ()) -> None:
    normalized = tuple(sorted({int(book_id) for book_id in book_ids if int(book_id) > 0}))
    with _PREWARM_THREAD_LOCK:
        global _PREWARM_STARTED
        if normalized:
            _PREWARM_BOOK_IDS.update(normalized)
        if _PREWARM_STARTED and not normalized:
            return
        target_book_ids = tuple(sorted(_PREWARM_BOOK_IDS))
        _PREWARM_STARTED = True

    threading.Thread(
        target=_run_agent_runtime_prewarm,
        args=(target_book_ids,),
        daemon=True,
    ).start()
