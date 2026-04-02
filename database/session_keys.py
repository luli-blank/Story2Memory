from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

DEFAULT_USER_ID = "0"
GLOBAL_SESSION_ID = "0"
GLOBAL_SESSION_TITLE = "全局"
QA_SESSION_SCHEMA_VERSION = 2
COSPLAY_SESSION_SCHEMA_VERSION = 2


def build_qa_session_info(
    *,
    novel_title: str = "",
    book_id: int = 0,
    user_id: str = DEFAULT_USER_ID,
) -> tuple[str, str, str]:
    title = str(novel_title or "").strip()
    normalized_book_id = int(book_id or 0)
    if normalized_book_id > 0:
        session_key = f"story2memory:{user_id}:book:{normalized_book_id}:mode:qa:v{QA_SESSION_SCHEMA_VERSION}"
        session_id = str(uuid5(NAMESPACE_URL, session_key))
        return session_id, user_id, title or f"book:{normalized_book_id}"
    if not title:
        return GLOBAL_SESSION_ID, user_id, GLOBAL_SESSION_TITLE
    return build_legacy_qa_session_info(novel_title=title, user_id=user_id)


def build_legacy_qa_session_info(
    *,
    novel_title: str,
    user_id: str = DEFAULT_USER_ID,
) -> tuple[str, str, str]:
    title = str(novel_title or "").strip()
    if not title:
        return GLOBAL_SESSION_ID, user_id, GLOBAL_SESSION_TITLE
    session_key = f"story2memory:{user_id}:{title}"
    session_id = str(uuid5(NAMESPACE_URL, session_key))
    return session_id, user_id, title


def build_cosplay_session_info(
    *,
    book_id: int,
    novel_title: str,
    character_id: int,
    character_name: str,
    user_id: str = DEFAULT_USER_ID,
) -> tuple[str, str, str]:
    effective_title = str(novel_title or "").strip() or "未指定作品"
    session_key = (
        f"story2memory:{user_id}:book:{int(book_id)}:character:{int(character_id)}:"
        f"mode:cosplay:v{COSPLAY_SESSION_SCHEMA_VERSION}"
    )
    session_id = str(uuid5(NAMESPACE_URL, session_key))
    return session_id, user_id, f"角色扮演·{effective_title}·{character_name}"
