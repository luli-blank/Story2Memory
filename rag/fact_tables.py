from __future__ import annotations

import json
import re
from typing import Any


def _parse_json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    return None


def _strip_name_wrappers(text: str) -> str:
    cleaned = str(text or "").strip()
    if cleaned.startswith("<") and cleaned.endswith(">") and len(cleaned) > 2:
        cleaned = cleaned[1:-1].strip()
    if cleaned.startswith("《") and cleaned.endswith("》") and len(cleaned) > 2:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def canonicalize_entity_name(value: Any) -> str:
    text = _strip_name_wrappers(str(value or "").strip())
    if " - " in text:
        head = text.split(" - ", 1)[0].strip()
        if head:
            text = head
    return text.strip()


def normalize_entity_name(value: Any) -> str:
    text = canonicalize_entity_name(value)
    text = re.sub(r"\s+", "", text).lower()
    return text


def _dedupe_list(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item or item in seen:
            continue
        result.append(item)
        seen.add(item)
    return result


def _normalize_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized = [canonicalize_entity_name(item) for item in value]
    normalized = [item for item in normalized if item]
    return _dedupe_list(normalized)


def _normalize_alias_entries(
    value: Any,
    *,
    standard_name_keys: tuple[str, ...],
    alias_keys: tuple[str, ...],
    type_keys: tuple[str, ...] = ("type", "kind"),
) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            alias = canonicalize_entity_name(item)
            if not alias:
                continue
            signature = ("", alias, "")
            if signature in seen:
                continue
            normalized.append({"standard_name": "", "alias": alias, "type": ""})
            seen.add(signature)
            continue

        standard_name = ""
        for key in standard_name_keys:
            standard_name = canonicalize_entity_name(item.get(key))
            if standard_name:
                break
        alias = ""
        for key in alias_keys:
            alias = canonicalize_entity_name(item.get(key))
            if alias:
                break
        alias_type = ""
        for key in type_keys:
            alias_type = str(item.get(key) or "").strip()
            if alias_type:
                break
        if not alias:
            continue
        signature = (standard_name, alias, alias_type)
        if signature in seen:
            continue
        normalized.append(
            {
                "standard_name": standard_name,
                "alias": alias,
                "type": alias_type,
            }
        )
        seen.add(signature)
    return normalized


def _parse_chapter_entity_index(value: Any) -> dict[str, Any]:
    parsed = _parse_json_value(value)
    if not isinstance(parsed, dict):
        return {
            "character_index": {
                "mentioned_characters": [],
                "active_characters": [],
                "aliases_or_appellations": [],
                "candidate_focus_characters": [],
                "key_dialogue_speakers": [],
            },
            "faction_index": {
                "mentioned_factions": [],
                "active_factions": [],
                "faction_aliases_or_titles": [],
                "candidate_focus_factions": [],
            },
        }
    character_index = parsed.get("character_index", parsed.get("entity_index", {}).get("character_index"))
    faction_index = parsed.get("faction_index", parsed.get("entity_index", {}).get("faction_index"))
    if not isinstance(character_index, dict):
        character_index = {}
    if not isinstance(faction_index, dict):
        faction_index = {}
    return {
        "character_index": {
            "mentioned_characters": _normalize_str_list(character_index.get("mentioned_characters")),
            "active_characters": _normalize_str_list(character_index.get("active_characters")),
            "aliases_or_appellations": _normalize_alias_entries(
                character_index.get("aliases_or_appellations"),
                standard_name_keys=("standard_name", "character_name", "name"),
                alias_keys=("alias", "appellation", "title"),
            ),
            "candidate_focus_characters": _normalize_str_list(
                character_index.get("candidate_focus_characters")
            ),
            "key_dialogue_speakers": _normalize_str_list(character_index.get("key_dialogue_speakers")),
        },
        "faction_index": {
            "mentioned_factions": _normalize_str_list(faction_index.get("mentioned_factions")),
            "active_factions": _normalize_str_list(faction_index.get("active_factions")),
            "faction_aliases_or_titles": _normalize_alias_entries(
                faction_index.get("faction_aliases_or_titles"),
                standard_name_keys=("standard_name", "faction_name", "organization_name", "name"),
                alias_keys=("alias", "title", "appellation", "organization_alias"),
            ),
            "candidate_focus_factions": _normalize_str_list(
                faction_index.get("candidate_focus_factions")
            ),
        },
    }


def _normalize_plot_metadata(value: Any) -> dict[str, Any]:
    parsed = _parse_json_value(value)
    return parsed if isinstance(parsed, dict) else {}


def _parse_chapter_range(value: Any) -> tuple[int | None, int | None]:
    text = str(value or "").strip()
    if not text:
        return None, None
    matched = re.match(r"^\s*(\d+)\s*[-~—]\s*(\d+)\s*$", text)
    if matched:
        return int(matched.group(1)), int(matched.group(2))
    matched = re.match(r"^\s*(\d+)\s*$", text)
    if matched:
        chapter = int(matched.group(1))
        return chapter, chapter
    return None, None


class _FactTableBuilder:
    def __init__(self, conn, book_id: int):
        self.conn = conn
        self.book_id = int(book_id)
        self._entities: dict[tuple[str, str], dict[str, Any]] = {}
        self._aliases: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self._chapter_mentions: list[dict[str, Any]] = []
        self._plot_rows: list[dict[str, Any]] = []
        self._chapter_rows: list[dict[str, Any]] = []
        self._entity_id_by_key: dict[tuple[str, str], int] = {}

    def rebuild(self) -> dict[str, int]:
        self._load_source_rows()
        self._collect_entities_from_chapters()
        self._collect_entities_from_plots()
        self._rewrite_tables()
        return self._populate_fact_tables()

    def _load_source_rows(self) -> None:
        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, chapter_index, plot_id, volume_id, `character`
                FROM book_chapters
                WHERE book_id = %s
                ORDER BY chapter_index ASC, id ASC
                """,
                (self.book_id,),
            )
            self._chapter_rows = list(cursor.fetchall() or [])
            cursor.execute(
                """
                SELECT id, plot_id, volume_id, metadata
                FROM book_plots
                WHERE book_id = %s
                ORDER BY plot_id ASC, id ASC
                """,
                (self.book_id,),
            )
            self._plot_rows = list(cursor.fetchall() or [])

    def _register_entity(
        self,
        *,
        entity_type: str,
        name: str,
        chapter_index: int | None = None,
        plot_id: int | None = None,
        volume_id: int | None = None,
        is_core: bool = False,
    ) -> tuple[str, str]:
        canonical_name = canonicalize_entity_name(name)
        normalized_name = normalize_entity_name(name)
        if not canonical_name or not normalized_name:
            raise ValueError("Entity name cannot be empty.")
        key = (entity_type, normalized_name)
        record = self._entities.get(key)
        if record is None:
            record = {
                "entity_type": entity_type,
                "canonical_name": canonical_name,
                "normalized_name": normalized_name,
                "display_name": canonical_name,
                "first_chapter_index": chapter_index,
                "first_plot_id": plot_id,
                "first_volume_index": volume_id,
                "last_chapter_index": chapter_index,
                "last_plot_id": plot_id,
                "is_core": 1 if is_core else 0,
                "extra_json": None,
            }
            self._entities[key] = record
            return key
        if chapter_index is not None:
            if record["first_chapter_index"] is None or chapter_index < int(record["first_chapter_index"]):
                record["first_chapter_index"] = chapter_index
            if record["last_chapter_index"] is None or chapter_index > int(record["last_chapter_index"]):
                record["last_chapter_index"] = chapter_index
        if plot_id is not None:
            if record["first_plot_id"] is None or plot_id < int(record["first_plot_id"]):
                record["first_plot_id"] = plot_id
            if record["last_plot_id"] is None or plot_id > int(record["last_plot_id"]):
                record["last_plot_id"] = plot_id
        if volume_id is not None and record["first_volume_index"] is None:
            record["first_volume_index"] = volume_id
        if is_core:
            record["is_core"] = 1
        return key

    def _register_alias(
        self,
        *,
        entity_key: tuple[str, str],
        alias: str,
        alias_type: str,
        chapter_index: int | None,
    ) -> None:
        alias_text = canonicalize_entity_name(alias)
        if not alias_text:
            return
        alias_norm = normalize_entity_name(alias)
        if not alias_norm:
            return
        signature = (entity_key[0], entity_key[1], alias_norm, alias_type)
        row = self._aliases.get(signature)
        if row is None:
            self._aliases[signature] = {
                "entity_key": entity_key,
                "alias": alias_text,
                "normalized_alias": alias_norm,
                "alias_type": alias_type,
                "first_seen_chapter_index": chapter_index,
                "last_seen_chapter_index": chapter_index,
            }
            return
        if chapter_index is not None:
            if row["first_seen_chapter_index"] is None or chapter_index < int(row["first_seen_chapter_index"]):
                row["first_seen_chapter_index"] = chapter_index
            if row["last_seen_chapter_index"] is None or chapter_index > int(row["last_seen_chapter_index"]):
                row["last_seen_chapter_index"] = chapter_index

    def _collect_entities_from_chapters(self) -> None:
        for row in self._chapter_rows:
            chapter_id = int(row.get("id") or 0)
            chapter_index = int(row.get("chapter_index") or 0)
            plot_id = int(row.get("plot_id") or 0) or None
            volume_id = int(row.get("volume_id") or 0) or None
            index_data = _parse_chapter_entity_index(row.get("character"))
            character_index = index_data["character_index"]
            faction_index = index_data["faction_index"]

            chapter_mentions_map: dict[tuple[str, str], dict[str, Any]] = {}

            def mark(entity_type: str, name: str, flag: str) -> None:
                key = self._register_entity(
                    entity_type=entity_type,
                    name=name,
                    chapter_index=chapter_index,
                    plot_id=plot_id,
                    volume_id=volume_id,
                    is_core=flag == "is_focus_candidate",
                )
                entry = chapter_mentions_map.setdefault(
                    key,
                    {
                        "chapter_id": chapter_id,
                        "entity_key": key,
                        "is_mentioned": 0,
                        "is_active": 0,
                        "is_dialogue_speaker": 0,
                        "is_focus_candidate": 0,
                        "source_aliases": [],
                    },
                )
                entry[flag] = 1

            for name in character_index["mentioned_characters"]:
                mark("character", name, "is_mentioned")
            for name in character_index["active_characters"]:
                mark("character", name, "is_active")
            for name in character_index["key_dialogue_speakers"]:
                mark("character", name, "is_dialogue_speaker")
            for name in character_index["candidate_focus_characters"]:
                mark("character", name, "is_focus_candidate")

            for alias_row in character_index["aliases_or_appellations"]:
                standard_name = alias_row.get("standard_name") or alias_row.get("alias")
                if not standard_name:
                    continue
                key = self._register_entity(
                    entity_type="character",
                    name=standard_name,
                    chapter_index=chapter_index,
                    plot_id=plot_id,
                    volume_id=volume_id,
                )
                self._register_alias(
                    entity_key=key,
                    alias=alias_row.get("alias") or "",
                    alias_type=str(alias_row.get("type") or "").strip(),
                    chapter_index=chapter_index,
                )
                entry = chapter_mentions_map.setdefault(
                    key,
                    {
                        "chapter_id": chapter_id,
                        "entity_key": key,
                        "is_mentioned": 0,
                        "is_active": 0,
                        "is_dialogue_speaker": 0,
                        "is_focus_candidate": 0,
                        "source_aliases": [],
                    },
                )
                alias_name = canonicalize_entity_name(alias_row.get("alias"))
                if alias_name and alias_name not in entry["source_aliases"]:
                    entry["source_aliases"].append(alias_name)

            for name in faction_index["mentioned_factions"]:
                mark("faction", name, "is_mentioned")
            for name in faction_index["active_factions"]:
                mark("faction", name, "is_active")
            for name in faction_index["candidate_focus_factions"]:
                mark("faction", name, "is_focus_candidate")

            for alias_row in faction_index["faction_aliases_or_titles"]:
                standard_name = alias_row.get("standard_name") or alias_row.get("alias")
                if not standard_name:
                    continue
                key = self._register_entity(
                    entity_type="faction",
                    name=standard_name,
                    chapter_index=chapter_index,
                    plot_id=plot_id,
                    volume_id=volume_id,
                )
                self._register_alias(
                    entity_key=key,
                    alias=alias_row.get("alias") or "",
                    alias_type=str(alias_row.get("type") or "").strip(),
                    chapter_index=chapter_index,
                )
                entry = chapter_mentions_map.setdefault(
                    key,
                    {
                        "chapter_id": chapter_id,
                        "entity_key": key,
                        "is_mentioned": 0,
                        "is_active": 0,
                        "is_dialogue_speaker": 0,
                        "is_focus_candidate": 0,
                        "source_aliases": [],
                    },
                )
                alias_name = canonicalize_entity_name(alias_row.get("alias"))
                if alias_name and alias_name not in entry["source_aliases"]:
                    entry["source_aliases"].append(alias_name)

            self._chapter_mentions.extend(chapter_mentions_map.values())

    def _collect_entities_from_plots(self) -> None:
        for row in self._plot_rows:
            plot_id = int(row.get("plot_id") or 0)
            volume_id = int(row.get("volume_id") or 0) or None
            metadata = _normalize_plot_metadata(row.get("metadata"))
            key_entities = metadata.get("key_entities", {}) if isinstance(metadata.get("key_entities"), dict) else {}
            protagonists = set(_normalize_str_list(key_entities.get("protagonists")))
            important_characters = set(_normalize_str_list(key_entities.get("important_characters")))
            organizations = set(_normalize_str_list(key_entities.get("organizations")))
            all_characters = _normalize_str_list(key_entities.get("characters_who_appear"))
            all_factions = _normalize_str_list(key_entities.get("factions_who_appear")) or list(organizations)

            for name in all_characters:
                self._register_entity(
                    entity_type="character",
                    name=name,
                    plot_id=plot_id,
                    volume_id=volume_id,
                    is_core=name in protagonists or name in important_characters,
                )
            for name in all_factions:
                self._register_entity(
                    entity_type="faction",
                    name=name,
                    plot_id=plot_id,
                    volume_id=volume_id,
                    is_core=name in organizations,
                )

            for item in metadata.get("character_presence", []) if isinstance(metadata.get("character_presence"), list) else []:
                if not isinstance(item, dict):
                    continue
                if not canonicalize_entity_name(item.get("name")):
                    continue
                self._register_entity(
                    entity_type="character",
                    name=item.get("name"),
                    plot_id=plot_id,
                    volume_id=volume_id,
                    is_core=True,
                )
                for alias in _normalize_str_list(item.get("aliases")):
                    key = ("character", normalize_entity_name(item.get("name")))
                    if key in self._entities:
                        self._register_alias(
                            entity_key=key,
                            alias=alias,
                            alias_type="plot_alias",
                            chapter_index=None,
                        )

            for item in metadata.get("faction_presence", []) if isinstance(metadata.get("faction_presence"), list) else []:
                if not isinstance(item, dict):
                    continue
                if not canonicalize_entity_name(item.get("name")):
                    continue
                self._register_entity(
                    entity_type="faction",
                    name=item.get("name"),
                    plot_id=plot_id,
                    volume_id=volume_id,
                    is_core=True,
                )
                for alias in _normalize_str_list(item.get("aliases")):
                    key = ("faction", normalize_entity_name(item.get("name")))
                    if key in self._entities:
                        self._register_alias(
                            entity_key=key,
                            alias=alias,
                            alias_type="plot_alias",
                            chapter_index=None,
                        )

            portrait_deltas = metadata.get("portrait_deltas", {}) if isinstance(metadata.get("portrait_deltas"), dict) else {}
            for item in portrait_deltas.get("characters", []) if isinstance(portrait_deltas.get("characters"), list) else []:
                if isinstance(item, dict) and canonicalize_entity_name(item.get("name")):
                    self._register_entity(entity_type="character", name=item.get("name"), plot_id=plot_id, volume_id=volume_id, is_core=True)
            for item in portrait_deltas.get("factions", []) if isinstance(portrait_deltas.get("factions"), list) else []:
                if isinstance(item, dict) and canonicalize_entity_name(item.get("name")):
                    self._register_entity(entity_type="faction", name=item.get("name"), plot_id=plot_id, volume_id=volume_id, is_core=True)

    def _rewrite_tables(self) -> None:
        with self.conn.cursor() as cursor:
            cursor.execute("DELETE FROM plot_entity_deltas WHERE book_id = %s", (self.book_id,))
            cursor.execute("DELETE FROM plot_facts WHERE book_id = %s", (self.book_id,))
            cursor.execute("DELETE FROM plot_interactions WHERE book_id = %s", (self.book_id,))
            cursor.execute("DELETE FROM plot_entity_presence WHERE book_id = %s", (self.book_id,))
            cursor.execute("DELETE FROM chapter_entity_mentions WHERE book_id = %s", (self.book_id,))
            cursor.execute("DELETE FROM book_entity_aliases WHERE book_id = %s", (self.book_id,))
            cursor.execute("DELETE FROM book_entities WHERE book_id = %s", (self.book_id,))

            for key, entity in sorted(self._entities.items(), key=lambda item: (item[1]["entity_type"], item[1]["normalized_name"])):
                cursor.execute(
                    """
                    INSERT INTO book_entities
                    (
                        book_id,
                        entity_type,
                        canonical_name,
                        normalized_name,
                        display_name,
                        first_chapter_index,
                        first_plot_id,
                        first_volume_index,
                        last_chapter_index,
                        last_plot_id,
                        is_core,
                        extra_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        self.book_id,
                        entity["entity_type"],
                        entity["canonical_name"],
                        entity["normalized_name"],
                        entity["display_name"],
                        entity["first_chapter_index"],
                        entity["first_plot_id"],
                        entity["first_volume_index"],
                        entity["last_chapter_index"],
                        entity["last_plot_id"],
                        entity["is_core"],
                        json.dumps(entity["extra_json"], ensure_ascii=False) if entity["extra_json"] is not None else None,
                    ),
                )
                self._entity_id_by_key[key] = int(cursor.lastrowid)

            for alias in self._aliases.values():
                entity_id = self._entity_id_by_key.get(alias["entity_key"])
                if not entity_id:
                    continue
                cursor.execute(
                    """
                    INSERT INTO book_entity_aliases
                    (
                        book_id,
                        entity_id,
                        alias,
                        normalized_alias,
                        alias_type,
                        first_seen_chapter_index,
                        last_seen_chapter_index
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        self.book_id,
                        entity_id,
                        alias["alias"],
                        alias["normalized_alias"],
                        alias["alias_type"] or None,
                        alias["first_seen_chapter_index"],
                        alias["last_seen_chapter_index"],
                    ),
                )

            for mention in self._chapter_mentions:
                entity_id = self._entity_id_by_key.get(mention["entity_key"])
                if not entity_id:
                    continue
                cursor.execute(
                    """
                    INSERT INTO chapter_entity_mentions
                    (
                        book_id,
                        chapter_id,
                        entity_id,
                        is_mentioned,
                        is_active,
                        is_dialogue_speaker,
                        is_focus_candidate,
                        source_aliases_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        self.book_id,
                        mention["chapter_id"],
                        entity_id,
                        mention["is_mentioned"],
                        mention["is_active"],
                        mention["is_dialogue_speaker"],
                        mention["is_focus_candidate"],
                        json.dumps(mention["source_aliases"], ensure_ascii=False),
                    ),
                )

    def _importance_for_entity(self, *, name: str, key_entities: dict[str, Any], entity_type: str) -> str | None:
        canonical = canonicalize_entity_name(name)
        if entity_type == "character":
            if canonical in _normalize_str_list(key_entities.get("protagonists")):
                return "protagonist"
            if canonical in _normalize_str_list(key_entities.get("important_characters")):
                return "important"
        if entity_type == "faction":
            if canonical in _normalize_str_list(key_entities.get("organizations")):
                return "important"
        return None

    def _entity_id(self, entity_type: str, name: Any) -> int | None:
        normalized = normalize_entity_name(name)
        if not normalized:
            return None
        return self._entity_id_by_key.get((entity_type, normalized))

    def _populate_fact_tables(self) -> dict[str, int]:
        stats = {
            "entities": len(self._entity_id_by_key),
            "aliases": 0,
            "chapter_mentions": 0,
            "plot_entity_presence": 0,
            "plot_interactions": 0,
            "plot_facts": 0,
            "plot_entity_deltas": 0,
        }
        stats["aliases"] = len(self._aliases)
        stats["chapter_mentions"] = len(self._chapter_mentions)

        with self.conn.cursor() as cursor:
            for row in self._plot_rows:
                plot_row_id = int(row.get("id") or 0)
                metadata = _normalize_plot_metadata(row.get("metadata"))
                key_entities = metadata.get("key_entities", {}) if isinstance(metadata.get("key_entities"), dict) else {}

                for item in metadata.get("character_presence", []) if isinstance(metadata.get("character_presence"), list) else []:
                    if not isinstance(item, dict):
                        continue
                    entity_id = self._entity_id("character", item.get("name"))
                    if not entity_id:
                        continue
                    cursor.execute(
                        """
                        INSERT INTO plot_entity_presence
                        (
                            book_id,
                            plot_id_ref,
                            entity_id,
                            role_in_plot,
                            dialogue_presence,
                            importance,
                            major_actions_json,
                            status_change_json,
                            members_involved_json,
                            extra_json
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            self.book_id,
                            plot_row_id,
                            entity_id,
                            str(item.get("role_in_plot") or "").strip() or None,
                            str(item.get("dialogue_presence") or "").strip() or None,
                            self._importance_for_entity(name=item.get("name"), key_entities=key_entities, entity_type="character"),
                            json.dumps(item.get("major_actions") or [], ensure_ascii=False),
                            json.dumps([], ensure_ascii=False),
                            None,
                            json.dumps({"aliases": item.get("aliases") or []}, ensure_ascii=False),
                        ),
                    )
                    stats["plot_entity_presence"] += 1

                for item in metadata.get("faction_presence", []) if isinstance(metadata.get("faction_presence"), list) else []:
                    if not isinstance(item, dict):
                        continue
                    entity_id = self._entity_id("faction", item.get("name"))
                    if not entity_id:
                        continue
                    cursor.execute(
                        """
                        INSERT INTO plot_entity_presence
                        (
                            book_id,
                            plot_id_ref,
                            entity_id,
                            role_in_plot,
                            dialogue_presence,
                            importance,
                            major_actions_json,
                            status_change_json,
                            members_involved_json,
                            extra_json
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            self.book_id,
                            plot_row_id,
                            entity_id,
                            str(item.get("role_in_plot") or "").strip() or None,
                            None,
                            self._importance_for_entity(name=item.get("name"), key_entities=key_entities, entity_type="faction"),
                            json.dumps(item.get("major_actions") or [], ensure_ascii=False),
                            json.dumps(item.get("status_change") or [], ensure_ascii=False),
                            json.dumps(item.get("members_involved") or [], ensure_ascii=False),
                            json.dumps({"aliases": item.get("aliases") or []}, ensure_ascii=False),
                        ),
                    )
                    stats["plot_entity_presence"] += 1

                def insert_interaction(item: dict[str, Any], *, reader_sensitive: bool) -> None:
                    participants = _normalize_str_list(item.get("participants"))
                    if not participants:
                        return
                    actor_id = self._entity_id("character", participants[0]) or self._entity_id("faction", participants[0])
                    target_id = None
                    if len(participants) > 1:
                        target_id = self._entity_id("character", participants[1]) or self._entity_id("faction", participants[1])
                    chapter_start, chapter_end = _parse_chapter_range(item.get("chapter_range"))
                    cursor.execute(
                        """
                        INSERT INTO plot_interactions
                        (
                            book_id,
                            plot_id_ref,
                            actor_entity_id,
                            target_entity_id,
                            interaction_type,
                            importance,
                            summary,
                            chapter_range_start,
                            chapter_range_end,
                            is_reader_sensitive,
                            participants_json,
                            tags_json
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            self.book_id,
                            plot_row_id,
                            actor_id,
                            target_id,
                            str(item.get("type") or item.get("tag") or "interaction").strip(),
                            str(item.get("importance") or "").strip() or ("reader_sensitive" if reader_sensitive else None),
                            str(item.get("summary") or "").strip(),
                            chapter_start,
                            chapter_end,
                            1 if reader_sensitive else 0,
                            json.dumps(participants, ensure_ascii=False),
                            json.dumps([str(item.get("tag") or "").strip()] if item.get("tag") else [], ensure_ascii=False),
                        ),
                    )

                for item in metadata.get("interaction_highlights", []) if isinstance(metadata.get("interaction_highlights"), list) else []:
                    if isinstance(item, dict):
                        insert_interaction(item, reader_sensitive=False)
                        stats["plot_interactions"] += 1
                for item in metadata.get("reader_sensitive_moments", []) if isinstance(metadata.get("reader_sensitive_moments"), list) else []:
                    if isinstance(item, dict):
                        insert_interaction(item, reader_sensitive=True)
                        stats["plot_interactions"] += 1

                plot_facts = metadata.get("plot_facts", {}) if isinstance(metadata.get("plot_facts"), dict) else {}
                fact_mapping = {
                    "foreshadowing": "foreshadowing",
                    "world_rules": "world_rule",
                    "major_rewards": "major_reward",
                    "major_losses": "major_loss",
                }
                for source_key, fact_type in fact_mapping.items():
                    for content in _normalize_str_list(plot_facts.get(source_key)):
                        cursor.execute(
                            """
                            INSERT INTO plot_facts
                            (
                                book_id,
                                plot_id_ref,
                                fact_type,
                                subject_entity_id,
                                content,
                                importance,
                                extra_json
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                self.book_id,
                                plot_row_id,
                                fact_type,
                                None,
                                content,
                                None,
                                None,
                            ),
                        )
                        stats["plot_facts"] += 1

                portrait_deltas = metadata.get("portrait_deltas", {}) if isinstance(metadata.get("portrait_deltas"), dict) else {}
                for item in portrait_deltas.get("characters", []) if isinstance(portrait_deltas.get("characters"), list) else []:
                    if not isinstance(item, dict):
                        continue
                    entity_id = self._entity_id("character", item.get("name"))
                    if not entity_id:
                        continue
                    for field_name, delta_type in (
                        ("status_change", "character_status"),
                        ("items", "character_item"),
                        ("skills", "character_skill"),
                        ("relationship_updates", "character_relationship"),
                        ("speech_style_notes", "character_speech_style"),
                    ):
                        values = _normalize_str_list(item.get(field_name))
                        if not values:
                            continue
                        cursor.execute(
                            """
                            INSERT INTO plot_entity_deltas
                            (
                                book_id,
                                plot_id_ref,
                                entity_id,
                                delta_type,
                                payload_json
                            )
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            (
                                self.book_id,
                                plot_row_id,
                                entity_id,
                                delta_type,
                                json.dumps({"name": canonicalize_entity_name(item.get("name")), "values": values}, ensure_ascii=False),
                            ),
                        )
                        stats["plot_entity_deltas"] += 1

                for item in portrait_deltas.get("factions", []) if isinstance(portrait_deltas.get("factions"), list) else []:
                    if not isinstance(item, dict):
                        continue
                    entity_id = self._entity_id("faction", item.get("name"))
                    if not entity_id:
                        continue
                    for field_name, delta_type in (
                        ("status_change", "faction_status"),
                        ("leadership_or_membership_updates", "faction_structure"),
                        ("alliances_or_hostilities", "faction_relationship"),
                        ("resources_or_territory_changes", "faction_resource"),
                    ):
                        values = _normalize_str_list(item.get(field_name))
                        if not values:
                            continue
                        cursor.execute(
                            """
                            INSERT INTO plot_entity_deltas
                            (
                                book_id,
                                plot_id_ref,
                                entity_id,
                                delta_type,
                                payload_json
                            )
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            (
                                self.book_id,
                                plot_row_id,
                                entity_id,
                                delta_type,
                                json.dumps({"name": canonicalize_entity_name(item.get("name")), "values": values}, ensure_ascii=False),
                            ),
                        )
                        stats["plot_entity_deltas"] += 1

        return stats


def rebuild_book_fact_tables(conn, book_id: int) -> dict[str, int]:
    builder = _FactTableBuilder(conn, book_id)
    return builder.rebuild()


def upsert_cover_asset(
    conn,
    *,
    book_id: int,
    storage_path: str,
    public_url: str,
    mime_type: str | None,
    source: str | None,
) -> int:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id
            FROM book_assets
            WHERE book_id = %s AND asset_type = 'cover'
            ORDER BY id DESC
            LIMIT 1
            """,
            (book_id,),
        )
        existing = cursor.fetchone() or {}
        asset_id = int(existing.get("id") or 0)
        if asset_id > 0:
            cursor.execute(
                """
                UPDATE book_assets
                SET storage_path = %s,
                    public_url = %s,
                    mime_type = %s,
                    source = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (storage_path, public_url, mime_type, source, asset_id),
            )
        else:
            cursor.execute(
                """
                INSERT INTO book_assets
                (
                    book_id,
                    asset_type,
                    storage_path,
                    public_url,
                    mime_type,
                    source
                )
                VALUES (%s, 'cover', %s, %s, %s, %s)
                """,
                (book_id, storage_path, public_url, mime_type, source),
            )
            asset_id = int(cursor.lastrowid or 0)
        return asset_id
