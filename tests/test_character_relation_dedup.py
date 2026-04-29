from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag.character_profiles import _group_relation_events, _save_relations


def test_group_relation_events_merges_same_target_name_even_if_id_missing():
    events = [
        {
            "target_character_id": 1,
            "target_character_name": "林夏",
            "relation_type": "ally",
            "polarity": "positive",
            "strength": "high",
            "summary": "第一次建立信任。",
            "chapter_start": 12,
            "chapter_end": 12,
            "evidence_chapters": [12],
            "volume_index": 1,
        },
        {
            "target_character_id": None,
            "target_character_name": "林夏",
            "relation_type": "ally",
            "polarity": "positive",
            "strength": "high",
            "summary": "后续继续合作。",
            "chapter_start": 24,
            "chapter_end": 24,
            "evidence_chapters": [24],
            "volume_index": 1,
        },
    ]

    grouped = _group_relation_events(events)

    assert len(grouped) == 1
    assert grouped[0]["target_character_name"] == "林夏"
    assert grouped[0]["target_character_id"] == 1
    assert grouped[0]["first_chapter_index"] == 12
    assert grouped[0]["last_chapter_index"] == 24
    assert len(grouped[0]["history_json"]) == 2


def test_save_relations_dedupes_same_target_name_before_insert():
    class FakeCursor:
        def __init__(self):
            self.inserts: list[tuple[object, ...]] = []
            self.deletes: list[tuple[object, ...]] = []

        def execute(self, sql, params=None):
            statement = str(sql)
            if "DELETE FROM character_relations" in statement:
                self.deletes.append(params)
                return
            if "INSERT INTO character_relations" in statement:
                self.inserts.append(params)
                return
            raise AssertionError(f"Unexpected SQL: {statement}")

    cursor = FakeCursor()
    character_row = {"id": 1, "name": "赵明"}
    relations = [
        {
            "target_character_id": 7,
            "target_character_name": "林夏",
            "summary": "建立稳定信任。",
            "structural_relation": ["同伴"],
            "action_relation": ["并肩行动"],
            "emotional_relation": ["信任"],
            "directionality": "mutual",
            "stability": "stable",
            "current_status": "关系稳定",
            "drivers": ["共同目标"],
            "history_json": [
                {
                    "chapter_start": 10,
                    "chapter_end": 10,
                    "relation_type": "ally",
                    "polarity": "positive",
                    "strength": "high",
                    "summary": "初次信任。",
                    "evidence_chapters": [10],
                }
            ],
            "first_chapter_index": 10,
            "last_chapter_index": 10,
        },
        {
            "target_character_id": None,
            "target_character_name": "林夏",
            "summary": "继续保持信任。",
            "structural_relation": ["同伴"],
            "action_relation": ["互相支援"],
            "emotional_relation": ["信任"],
            "directionality": "",
            "stability": "",
            "current_status": "",
            "drivers": ["生死经历"],
            "history_json": [
                {
                    "chapter_start": 20,
                    "chapter_end": 20,
                    "relation_type": "ally",
                    "polarity": "positive",
                    "strength": "high",
                    "summary": "再次合作。",
                    "evidence_chapters": [20],
                }
            ],
            "first_chapter_index": 20,
            "last_chapter_index": 20,
        },
    ]

    _save_relations(cursor, 1, character_row, "hash-v1", relations)

    assert cursor.deletes == [(1, 1)]
    assert len(cursor.inserts) == 1
    inserted = cursor.inserts[0]
    assert inserted[0] == 1
    assert inserted[1] == 1
    assert inserted[3] == 7
    assert inserted[4] == "林夏"
