from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import logging
import threading
import time
from typing import Any, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from agent.graph import build_llm
from agent.hybridSearch import hybrid_retrieve_characters
from agent.prompt import COSPLAY_SEARCH_REWRITE_PROMPT, COSPLAY_TOOL_ROUTER_PROMPT, ROLEPLAY_SYSTEM_PROMPT_TEMPLATE
from database.mysql_client import MySQLChatStore
from rag.character_profiles import _connect as _connect_mysql

DEFAULT_USER_ID = "0"
SUMMARY_UPDATE_STEP = 10
RECENT_MESSAGES_LIMIT = 5
COSPLAY_SESSION_SCHEMA_VERSION = 2

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatTurn:
    role: str
    content: str


class CosplayAgent:
    def __init__(self) -> None:
        self._llm = build_llm()
        self._invoke_lock = threading.Lock()
        self._store = MySQLChatStore()
        self._last_trace_by_session: dict[str, dict[str, Any]] = {}

    def reply(
        self,
        user_input: str,
        *,
        book_id: int,
        novel_title: str,
        character_id: int,
        character_name: str,
        roleplay_context: Mapping[str, object],
        chat_history: Sequence[Mapping[str, str] | ChatTurn] | None = None,
    ) -> str:
        text = str(user_input or "").strip()
        if not text:
            return "请输入问题。"

        session_id, user_id, session_title = self._resolve_session_info(
            book_id=book_id,
            novel_title=novel_title,
            character_id=character_id,
            character_name=character_name,
        )
        use_store = self._store.ensure_session(session_id, user_id, session_title)
        summary = self._store.get_summary(session_id) if use_store else ""
        recent_turns = self._store.get_recent_messages(session_id, RECENT_MESSAGES_LIMIT) if use_store else None
        messages = self._build_messages(
            user_input=text,
            roleplay_context=roleplay_context,
            summary=summary,
            recent_turns=recent_turns,
            chat_history=chat_history,
        )

        route = self._route_tool(
            user_input=text,
            roleplay_context=roleplay_context,
            summary=summary,
            chat_history=recent_turns if recent_turns is not None else chat_history,
        )
        tool_name = str(route.get("tool") or "no_tool")
        tool_args: dict[str, Any] = {}
        tool_result = ""
        if tool_name == "plot_search":
            tool_args = {
                "query": str(route.get("query") or text),
                "book_id": int(book_id or 0),
                "novel_title": novel_title,
                "character_id": int(character_id or 0),
            }
            tool_result = self._plot_search(**tool_args)
        elif tool_name == "relation_search":
            tool_args = {
                "query": str(route.get("query") or text),
                "target_name": str(route.get("target_name") or "").strip(),
                "book_id": int(book_id or 0),
                "roleplay_context": roleplay_context,
            }
            tool_result = self._relation_search(**tool_args)

        if tool_result:
            reply = self._rewrite_tool_result(
                user_input=text,
                roleplay_context=roleplay_context,
                tool_name=tool_name,
                tool_result=tool_result,
            )
        else:
            with self._invoke_lock:
                response = self._llm.invoke(messages)
            reply = self._stringify_content(getattr(response, "content", response)) or "……"

        if use_store:
            self._store.append_message(session_id=session_id, role="user", content=text, token_count=0)
            self._store.append_message(session_id=session_id, role="assistant", content=reply, token_count=self._estimate_token_count(reply))
            self._trigger_summary_update(session_id, roleplay_context)

        self._last_trace_by_session[session_id] = {
            "tool": tool_name,
            "tool_args": tool_args,
            "tool_used": bool(tool_result),
            "system_prompt": getattr(messages[0], "content", "") if messages else "",
        }
        return reply

    def get_last_trace(
        self,
        *,
        book_id: int,
        novel_title: str,
        character_id: int,
        character_name: str,
    ) -> dict[str, Any]:
        session_id, _, _ = self._resolve_session_info(
            book_id=book_id,
            novel_title=novel_title,
            character_id=character_id,
            character_name=character_name,
        )
        return dict(self._last_trace_by_session.get(session_id, {}) or {})

    def _build_messages(
        self,
        *,
        user_input: str,
        roleplay_context: Mapping[str, object],
        summary: str = "",
        recent_turns: Sequence[Mapping[str, str] | ChatTurn] | None = None,
        chat_history: Sequence[Mapping[str, str] | ChatTurn] | None = None,
    ) -> list[BaseMessage]:
        turns = self._normalize_turns(recent_turns if recent_turns is not None else chat_history)
        style_samples = self._select_style_samples(user_input, roleplay_context.get("style_samples"))
        style_example_text = "\n".join(
            f"- 在{sample['scene']}时，会说：{sample['quote']}"
            for sample in style_samples
            if str(sample.get("scene") or "").strip() and str(sample.get("quote") or "").strip()
        ) or "暂无。"
        messages: list[BaseMessage] = [
            SystemMessage(
                content=ROLEPLAY_SYSTEM_PROMPT_TEMPLATE.format(
                    novel_title=str(roleplay_context.get("novel_title") or "未指定作品"),
                    character_name=str(roleplay_context.get("character_name") or "未命名角色"),
                    persona_summary=str(roleplay_context.get("persona_summary") or "暂无角色摘要。").strip(),
                )
                + f"\n以下是角色语言风格实例，请优先模仿其句式、语气和节奏，不要机械复述：\n{style_example_text}"
                + f"\n以下是本次角色对话的历史摘要：{summary.strip() or '暂无历史摘要。'}"
            )
        ]
        for turn in turns:
            lc_message = self._to_langchain_message(turn)
            if lc_message is not None:
                messages.append(lc_message)
        if not turns or turns[-1].role != "user" or turns[-1].content.strip() != user_input:
            messages.append(HumanMessage(content=user_input))
        return messages

    @staticmethod
    def _select_style_samples(user_input: str, style_samples: object, limit: int = 4) -> list[dict[str, Any]]:
        rows = [item for item in (style_samples or []) if isinstance(item, dict)]
        if not rows:
            return []
        query = str(user_input or "").strip()
        if not query:
            return rows[:limit]
        scored: list[tuple[int, dict[str, Any]]] = []
        for item in rows:
            scene = str(item.get("scene") or "").strip()
            quote = str(item.get("quote") or "").strip()
            score = 0
            if scene and any(token and token in scene for token in [query, *query.split()]):
                score += 2
            if quote and any(token and token in quote for token in [query, *query.split()]):
                score += 1
            scored.append((score, item))
        scored.sort(key=lambda entry: (-entry[0], str(entry[1].get("scene") or ""), str(entry[1].get("quote") or "")))
        return [item for _, item in scored[:limit]]

    def _route_tool(
        self,
        *,
        user_input: str,
        roleplay_context: Mapping[str, object],
        summary: str = "",
        chat_history: Sequence[Mapping[str, str] | ChatTurn] | None = None,
    ) -> dict[str, Any]:
        history_text = self._format_turns_for_prompt(chat_history)
        router_input = (
            f"角色名：{roleplay_context.get('character_name')}\n"
            f"角色扮演摘要：{str(roleplay_context.get('persona_summary') or '').strip()}\n"
            f"历史摘要：{summary.strip() or '暂无历史摘要。'}\n"
            f"最近对话：\n{history_text}\n\n"
            f"用户最新问题：{user_input}\n"
        )
        with self._invoke_lock:
            response = self._llm.invoke(
                [
                    SystemMessage(content=COSPLAY_TOOL_ROUTER_PROMPT),
                    HumanMessage(content=router_input),
                ]
            )
        parsed = self._extract_json_object(self._stringify_content(getattr(response, "content", response)))
        if not isinstance(parsed, dict):
            return {"tool": "no_tool", "query": user_input, "target_name": "", "reason": "router_failed"}
        tool_name = str(parsed.get("tool") or "no_tool").strip()
        if tool_name not in {"plot_search", "relation_search", "no_tool"}:
            tool_name = "no_tool"
        return {
            "tool": tool_name,
            "query": str(parsed.get("query") or user_input).strip(),
            "target_name": str(parsed.get("target_name") or "").strip(),
            "reason": str(parsed.get("reason") or "").strip(),
        }

    def _plot_search(self, *, query: str, book_id: int, novel_title: str, character_id: int) -> str:
        raw = hybrid_retrieve_characters.invoke(
            {
                "query": query,
                "user_query": query,
                "novel_title": novel_title,
                "book_id": book_id,
                "top_k": 8,
                "source_ids": [character_id],
            }
        )
        payload = self._extract_json_object(raw) or {}
        items = payload.get("results", []) if isinstance(payload.get("results"), list) else []
        compact = [
            {
                "chapter_index": item.get("chapter_index"),
                "record": item.get("record", ""),
            }
            for item in items
            if isinstance(item, dict)
        ]
        return json.dumps({"status": payload.get("status", "ok"), "results": compact}, ensure_ascii=False)

    def _relation_search(
        self,
        *,
        query: str,
        target_name: str,
        book_id: int,
        roleplay_context: Mapping[str, object],
    ) -> str:
        alias_lookup = self._load_book_alias_lookup(book_id)
        normalized_query = str(query or "").strip()
        matched_targets: set[str] = set()
        for name, aliases in alias_lookup.items():
            candidates = [name, *aliases]
            if any(candidate and candidate in normalized_query for candidate in candidates):
                matched_targets.add(name)
        if target_name:
            matched_targets.add(target_name)

        relations = [
            item for item in roleplay_context.get("relations", []) if isinstance(item, dict)
            and str(item.get("target_character_name") or "").strip() in matched_targets
        ]
        emotional_relations = [
            item for item in roleplay_context.get("emotional_relations", []) if isinstance(item, dict)
            and str(item.get("target_character") or "").strip() in matched_targets
        ]
        return json.dumps(
            {
                "matched_targets": sorted(matched_targets),
                "relations": relations,
                "emotional_relations": emotional_relations,
            },
            ensure_ascii=False,
        )

    def _rewrite_tool_result(
        self,
        *,
        user_input: str,
        roleplay_context: Mapping[str, object],
        tool_name: str,
        tool_result: str,
    ) -> str:
        rewrite_input = (
            f"角色名：{roleplay_context.get('character_name')}\n"
            f"角色扮演摘要：{str(roleplay_context.get('persona_summary') or '').strip()}\n"
            f"工具名：{tool_name}\n"
            f"用户问题：{user_input}\n"
            f"工具结果：{tool_result}\n"
        )
        with self._invoke_lock:
            response = self._llm.invoke(
                [
                    SystemMessage(content=COSPLAY_SEARCH_REWRITE_PROMPT),
                    HumanMessage(content=rewrite_input),
                ]
            )
        return self._stringify_content(getattr(response, "content", response)) or "……"

    @staticmethod
    def _resolve_session_info(
        *,
        book_id: int,
        novel_title: str,
        character_id: int,
        character_name: str,
    ) -> tuple[str, str, str]:
        user_id = DEFAULT_USER_ID
        effective_title = str(novel_title or "").strip() or "未指定作品"
        session_key = (
            f"story2memory:{user_id}:book:{int(book_id)}:character:{int(character_id)}:"
            f"mode:cosplay:v{COSPLAY_SESSION_SCHEMA_VERSION}"
        )
        session_id = str(uuid5(NAMESPACE_URL, session_key))
        return session_id, user_id, f"角色扮演·{effective_title}·{character_name}"

    @staticmethod
    def _normalize_turns(chat_history: Sequence[Mapping[str, str] | ChatTurn] | None) -> list[ChatTurn]:
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
    def _format_turns_for_prompt(chat_history: Sequence[Mapping[str, str] | ChatTurn] | None, limit: int = 6) -> str:
        turns = CosplayAgent._normalize_turns(chat_history)
        if not turns:
            return "（无）"
        return "\n".join(f"{turn.role}: {turn.content}" for turn in turns[-max(1, limit):])

    @staticmethod
    def _stringify_content(content: object) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item.strip())
                elif isinstance(item, Mapping):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
            return "\n".join(part for part in parts if part)
        return str(content).strip()

    @staticmethod
    def _extract_json_object(raw: str) -> dict[str, Any] | None:
        payload = str(raw or "").strip()
        if not payload:
            return None
        try:
            parsed = json.loads(payload)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    @staticmethod
    def _estimate_token_count(text: str) -> int:
        return max(1, len(text.strip()) // 4)

    @staticmethod
    @lru_cache(maxsize=32)
    def _load_book_alias_lookup(book_id: int) -> dict[str, list[str]]:
        lookup: dict[str, list[str]] = {}
        with _connect_mysql() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT name, aliases FROM characters WHERE book_id = %s", (int(book_id),))
                rows = cursor.fetchall() or []
        for row in rows:
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            aliases_raw = row.get("aliases")
            aliases = []
            if isinstance(aliases_raw, str):
                try:
                    parsed = json.loads(aliases_raw)
                    aliases = [str(item or "").strip() for item in parsed if str(item or "").strip()]
                except Exception:
                    aliases = []
            elif isinstance(aliases_raw, list):
                aliases = [str(item or "").strip() for item in aliases_raw if str(item or "").strip()]
            lookup[name] = aliases
        return lookup

    def _trigger_summary_update(self, session_id: str, roleplay_context: Mapping[str, object]) -> None:
        updater = threading.Thread(
            target=self._update_summary_if_needed,
            args=(session_id, roleplay_context),
            daemon=True,
        )
        updater.start()

    def _update_summary_if_needed(self, session_id: str, roleplay_context: Mapping[str, object]) -> None:
        try:
            last_summarized_msg_id = self._store.get_last_summarized_msg_id(session_id)
            latest_msg_id = self._store.get_latest_message_id(session_id)
            if latest_msg_id - last_summarized_msg_id <= SUMMARY_UPDATE_STEP:
                return
            new_rows = self._store.get_messages_after(session_id, last_summarized_msg_id)
            if not new_rows:
                return
            current_summary = self._store.get_summary(session_id)
            transcript = "\n".join(
                f"{row.get('role')}: {str(row.get('content') or '').strip()}"
                for row in new_rows
                if str(row.get("content") or "").strip()
            )
            prompt = (
                f"角色名：{roleplay_context.get('character_name')}\n"
                f"角色扮演摘要：{str(roleplay_context.get('persona_summary') or '').strip()}\n"
                f"旧摘要：{current_summary.strip() or '（无）'}\n\n"
                f"新增对话：\n{transcript}\n\n"
                "请输出新的精炼摘要，保持角色扮演语境。"
            )
            with self._invoke_lock:
                response = self._llm.invoke(
                    [
                        SystemMessage(content="你是角色扮演对话摘要助手。请输出新的完整摘要，控制在8-12句。"),
                        HumanMessage(content=prompt),
                    ]
                )
            new_summary = self._stringify_content(getattr(response, "content", response))
            if not new_summary:
                return
            final_msg_id = int(new_rows[-1].get("id", latest_msg_id) or latest_msg_id)
            self._store.update_summary(session_id, new_summary, final_msg_id)
        except Exception:
            logger.exception("Failed to update cosplay summary asynchronously: session_id=%s", session_id)


@lru_cache(maxsize=1)
def get_cosplay_agent() -> CosplayAgent:
    return CosplayAgent()
