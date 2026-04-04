from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag.character_profiles import _list_valid_characters
from rag.createCharacters import (
    _apply_generic_character_rewrite_result,
    _build_character_item_key,
    _finalize_character_aliases,
    _finalize_item_groups,
    _merge_group_records,
    _merge_same_canonical_character_items,
    _pick_preferred_character_name,
)


def test_list_valid_characters_keeps_only_non_deleted_rows():
    rows = [
        {"id": 1, "name": "路明非", "need_delete": "no"},
        {"id": 2, "name": "叔叔", "need_delete": "yes"},
        {"id": 3, "name": "", "need_delete": "no"},
    ]

    assert _list_valid_characters(rows) == [{"id": 1, "name": "路明非", "need_delete": "no"}]


def test_apply_generic_character_rewrite_result_rewrites_to_anchored_name():
    original = {
        "name": "叔叔",
        "aliases": ["叔叔"],
        "records": [[12, "训斥路明非并让他出来吃饭。"]],
    }

    rewritten = _apply_generic_character_rewrite_result(
        original,
        {
            "action": "rewrite",
            "canonical_name": "路明非的叔叔",
        },
    )

    assert rewritten == {
        "name": "路明非的叔叔",
        "aliases": ["路明非的叔叔"],
        "records": [[12, "训斥路明非并让他出来吃饭。"]],
    }


def test_apply_generic_character_rewrite_result_drops_unresolved_generic_name():
    original = {
        "name": "叔叔",
        "aliases": ["叔叔"],
        "records": [[12, "训斥路明非并让他出来吃饭。"]],
    }

    assert _apply_generic_character_rewrite_result(original, {"action": "drop"}) is None


def test_finalize_character_aliases_preserves_empty_aliases():
    finalized = _finalize_character_aliases(
        [
            {
                "name": "酒德麻衣",
                "aliases": [],
                "records": [[43, "作为执行小组成员行动。"]],
            }
        ]
    )

    assert finalized == [
        {
            "name": "酒德麻衣",
            "aliases": [],
            "records": [[43, "作为执行小组成员行动。"]],
        }
    ]


def test_pick_preferred_character_name_prefers_formal_name_over_short_alias():
    assert _pick_preferred_character_name("麻衣", ["酒德麻衣"]) == "酒德麻衣"
    assert _pick_preferred_character_name("薯片", ["苏恩曦", "老板娘"]) == "苏恩曦"


def test_pick_preferred_character_name_prefers_specific_name_over_descriptor_and_nickname():
    assert _pick_preferred_character_name("白衣孩子", ["康斯坦丁", "老唐"]) == "康斯坦丁"
    assert _pick_preferred_character_name("小魔鬼", ["路鸣泽"]) == "路鸣泽"


def test_finalize_item_groups_uses_llm_result_for_canonical_name_and_aliases(monkeypatch):
    class _FakeResponse:
        def __init__(self, content: str) -> None:
            self.content = content

    class _FakeLLM:
        async def ainvoke(self, prompt: str) -> _FakeResponse:
            assert "酒德麻衣" in prompt
            assert "麻衣队长" in prompt
            return _FakeResponse(
                """
                {
                  "canonical_name": "酒德麻衣",
                  "aliases": [],
                  "dropped_candidates": [
                    {"text": "麻衣", "reason": "简称"},
                    {"text": "麻衣队长", "reason": "名字与职位混合噪声"},
                    {"text": "麻衣（队长）", "reason": "括号职位噪声"}
                  ],
                  "reason": "酒德麻衣是最完整正式的人名"
                }
                """
            )

    monkeypatch.setattr("rag.createCharacters.build_llm", lambda model=None: _FakeLLM())

    finalized = _finalize_item_groups(
        [
            [
                {
                    "_item_id": "item-1",
                    "name": "麻衣",
                    "aliases": ["麻衣队长", "麻衣（队长）"],
                    "records": [[43, "负责执行任务。"]],
                },
                {
                    "_item_id": "item-2",
                    "name": "酒德麻衣",
                    "aliases": ["麻衣"],
                    "records": [[44, "与苏恩曦保持联络。"]],
                },
            ]
        ]
    )

    assert finalized == [
        {
            "name": "酒德麻衣",
            "aliases": [],
            "records": [[43, "负责执行任务。"], [44, "与苏恩曦保持联络。"]],
        }
    ]


def test_build_character_item_key_uses_name_when_aliases_are_empty():
    left = _build_character_item_key({"name": "酒德麻衣", "aliases": []})
    right = _build_character_item_key({"name": "苏恩曦", "aliases": []})

    assert left != right


def test_merge_same_canonical_character_items_merges_segmented_same_name_rows():
    merged = _merge_same_canonical_character_items(
        [
            {
                "name": "路明非",
                "aliases": [],
                "records": [[36, "在仓库中研究资料。"], [38, "提议外出吃拉面。"]],
            },
            {
                "name": "路明非",
                "aliases": [],
                "records": [[49, "成为学院狙杀目标。"]],
            },
            {
                "name": "路明非",
                "aliases": [],
                "records": [[55, "得知苏茜与兰斯洛特订婚。"]],
            },
        ]
    )

    assert merged == [
        {
            "name": "路明非",
            "aliases": [],
            "records": [
                [36, "在仓库中研究资料。"],
                [38, "提议外出吃拉面。"],
                [49, "成为学院狙杀目标。"],
                [55, "得知苏茜与兰斯洛特订婚。"],
            ],
        }
    ]


def test_merge_group_records_sorts_records_by_numeric_chapter_order():
    merged = _merge_group_records(
        [
            {
                "name": "路明非",
                "aliases": [],
                "records": [[10, "chapter 10"], [2, "chapter 2"]],
            },
            {
                "name": "路明非",
                "aliases": [],
                "records": [[100, "chapter 100"], [36, "chapter 36"]],
            },
        ]
    )

    assert merged == [
        [2, "chapter 2"],
        [10, "chapter 10"],
        [36, "chapter 36"],
        [100, "chapter 100"],
    ]
