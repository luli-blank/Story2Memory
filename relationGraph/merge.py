from __future__ import annotations

import hashlib
import re
from typing import Any


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).lower()


def build_graph_key(book_id: int, character_id: int | None, name: str) -> str:
    if int(character_id or 0) > 0:
        return f"char:{int(book_id)}:{int(character_id)}"
    digest = hashlib.sha1(normalize_name(name).encode("utf-8")).hexdigest()[:12]
    return f"stub:{int(book_id)}:{digest}"


def build_edge_key(book_id: int, source_graph_key: str, target_graph_key: str) -> str:
    return f"edge:{int(book_id)}:{source_graph_key}:{target_graph_key}"


def build_event_key(edge_key: str, history_row: dict[str, Any]) -> str:
    material = "|".join(
        [
            edge_key,
            str(int(history_row.get("chapter_start") or 0)),
            str(int(history_row.get("chapter_end") or 0)),
            str(history_row.get("relation_type") or "").strip(),
            str(history_row.get("summary") or "").strip(),
        ]
    )
    digest = hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]
    return f"event:{digest}"


def resolve_node_match(
    *,
    book_id: int,
    character_id: int | None,
    name: str,
    aliases: list[str],
    candidates: list[dict[str, Any]],
) -> tuple[str, bool]:
    normalized_aliases = {normalize_name(name), *(normalize_name(item) for item in aliases or [])}
    resolved_character_id = int(character_id or 0)

    if resolved_character_id > 0:
        for candidate in candidates:
            if int(candidate.get("book_id") or 0) != int(book_id):
                continue
            if int(candidate.get("character_id") or 0) == resolved_character_id:
                return str(candidate.get("graph_key") or ""), True

    stub_candidates: list[str] = []
    for candidate in candidates:
        if int(candidate.get("book_id") or 0) != int(book_id):
            continue
        if str(candidate.get("profile_status") or "") != "stub":
            continue
        candidate_name = normalize_name(str(candidate.get("name") or ""))
        candidate_aliases = {normalize_name(item) for item in candidate.get("aliases") or []}
        if candidate_name in normalized_aliases or normalize_name(name) in candidate_aliases or (normalized_aliases & candidate_aliases):
            stub_candidates.append(str(candidate.get("graph_key") or ""))

    unique_stub_candidates = sorted({item for item in stub_candidates if item})
    if len(unique_stub_candidates) == 1:
        return unique_stub_candidates[0], True

    return build_graph_key(book_id, resolved_character_id or None, name), False
