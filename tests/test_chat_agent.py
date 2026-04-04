from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent.chat_agent as chat_agent_module


def test_get_last_search_packet_uses_book_scoped_session(monkeypatch):
    class DummyStore:
        pass

    class DummyLLM:
        def bind_tools(self, _tools):
            return object()

    monkeypatch.setattr(chat_agent_module, "compile_graph", lambda: object())
    monkeypatch.setattr(chat_agent_module, "build_llm", lambda: DummyLLM())
    monkeypatch.setattr(chat_agent_module, "MySQLChatStore", lambda: DummyStore())

    agent = chat_agent_module.ChatAgent()
    session_id, _, _ = agent._resolve_session_info(novel_title="三国演义", book_id=5)
    agent._last_search_packet_by_session[session_id] = {"answer": "ok"}

    packet = agent.get_last_search_packet("三国演义", book_id=5)

    assert packet == {"answer": "ok"}
