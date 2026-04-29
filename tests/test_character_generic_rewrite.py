from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag.character_profiles import _list_valid_characters
from rag.createCharacters import (
    _apply_generic_character_rewrite_result,
    _build_character_item_key,
    _build_second_pass_candidate_groups,
    _finalize_character_aliases,
    _finalize_item_groups,
    _merge_group_records,
    _merge_same_canonical_character_items,
    _pick_preferred_character_name,
    _run_character_rewrite_and_merge,
)


def test_list_valid_characters_keeps_only_non_deleted_rows():
    rows = [
        {"id": 1, "name": "赵明", "need_delete": "no"},
        {"id": 2, "name": "叔叔", "need_delete": "yes"},
        {"id": 3, "name": "", "need_delete": "no"},
    ]

    assert _list_valid_characters(rows) == [{"id": 1, "name": "赵明", "need_delete": "no"}]


def test_apply_generic_character_rewrite_result_rewrites_to_anchored_name():
    original = {
        "name": "叔叔",
        "aliases": ["叔叔"],
        "records": [[12, "训斥赵明并让他出来吃饭。"]],
    }

    rewritten = _apply_generic_character_rewrite_result(
        original,
        {
            "action": "rewrite",
            "canonical_name": "赵明的叔叔",
        },
    )

    assert rewritten == {
        "name": "赵明的叔叔",
        "aliases": ["赵明的叔叔"],
        "records": [[12, "训斥赵明并让他出来吃饭。"]],
    }


def test_apply_generic_character_rewrite_result_drops_unresolved_generic_name():
    original = {
        "name": "叔叔",
        "aliases": ["叔叔"],
        "records": [[12, "训斥赵明并让他出来吃饭。"]],
    }

    assert _apply_generic_character_rewrite_result(original, {"action": "drop"}) is None


def test_finalize_character_aliases_preserves_empty_aliases():
    finalized = _finalize_character_aliases(
        [
            {
                "name": "周青",
                "aliases": [],
                "records": [[43, "作为执行小组成员行动。"]],
            }
        ]
    )

    assert finalized == [
        {
            "name": "周青",
            "aliases": [],
            "records": [[43, "作为执行小组成员行动。"]],
        }
    ]


def test_pick_preferred_character_name_prefers_formal_name_over_short_alias():
    assert _pick_preferred_character_name("麻衣", ["周青"]) == "周青"
    assert _pick_preferred_character_name("薯片", ["林晚", "老板娘"]) == "林晚"


def test_pick_preferred_character_name_prefers_specific_name_over_descriptor_and_nickname():
    assert _pick_preferred_character_name("白衣孩子", ["沈岚", "老唐"]) == "沈岚"
    assert _pick_preferred_character_name("小魔鬼", ["赵泽"]) == "赵泽"


def test_finalize_item_groups_uses_llm_result_for_canonical_name_and_aliases(monkeypatch):
    class _FakeResponse:
        def __init__(self, content: str) -> None:
            self.content = content

    class _FakeLLM:
        async def ainvoke(self, prompt: str) -> _FakeResponse:
            assert "周青" in prompt
            assert "麻衣队长" in prompt
            return _FakeResponse(
                """
                {
                  "canonical_name": "周青",
                  "aliases": [],
                  "dropped_candidates": [
                    {"text": "麻衣", "reason": "简称"},
                    {"text": "麻衣队长", "reason": "名字与职位混合噪声"},
                    {"text": "麻衣（队长）", "reason": "括号职位噪声"}
                  ],
                  "reason": "周青是最完整正式的人名"
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
                    "name": "周青",
                    "aliases": ["麻衣"],
                    "records": [[44, "与林晚保持联络。"]],
                },
            ]
        ]
    )

    assert finalized == [
        {
            "name": "周青",
            "aliases": [],
            "records": [[43, "负责执行任务。"], [44, "与林晚保持联络。"]],
        }
    ]


def test_build_character_item_key_uses_name_when_aliases_are_empty():
    left = _build_character_item_key({"name": "周青", "aliases": []})
    right = _build_character_item_key({"name": "林晚", "aliases": []})

    assert left != right


def test_merge_same_canonical_character_items_merges_segmented_same_name_rows():
    merged = _merge_same_canonical_character_items(
        [
            {
                "name": "赵明",
                "aliases": [],
                "records": [[36, "在仓库中研究资料。"], [38, "提议外出吃拉面。"]],
            },
            {
                "name": "赵明",
                "aliases": [],
                "records": [[49, "成为学院狙杀目标。"]],
            },
            {
                "name": "赵明",
                "aliases": [],
                "records": [[55, "得知苏茜与兰斯洛特订婚。"]],
            },
        ]
    )

    assert merged == [
        {
            "name": "赵明",
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
                "name": "赵明",
                "aliases": [],
                "records": [[10, "chapter 10"], [2, "chapter 2"]],
            },
            {
                "name": "赵明",
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


def test_run_character_rewrite_and_merge_uses_single_asyncio_run(monkeypatch):
    class _FakeLLM:
        pass

    original_asyncio_run = __import__("asyncio").run
    run_calls: list[str] = []

    def _counting_run(coro):
        run_calls.append(getattr(coro, "__name__", coro.__class__.__name__))
        return original_asyncio_run(coro)

    async def _fake_rewrite_character_batches(llm_client, items):
        assert isinstance(llm_client, _FakeLLM)
        return items

    async def _fake_build_candidate_merge_groups(llm_client, book_id, items):
        assert isinstance(llm_client, _FakeLLM)
        assert book_id == 6
        return [items]

    async def _fake_finalize_item_groups_async(llm_client, groups):
        assert isinstance(llm_client, _FakeLLM)
        return [
            {
                "name": "罗杰·艾克罗伊德",
                "aliases": ["艾克罗伊德先生"],
                "records": [[1, "record"]],
            }
        ]

    monkeypatch.setattr("rag.createCharacters.build_llm", lambda model=None: _FakeLLM())
    monkeypatch.setattr("rag.createCharacters.asyncio.run", _counting_run)
    monkeypatch.setattr("rag.createCharacters._rewrite_character_batches", _fake_rewrite_character_batches)
    monkeypatch.setattr("rag.createCharacters._build_candidate_merge_groups", _fake_build_candidate_merge_groups)
    monkeypatch.setattr("rag.createCharacters._finalize_item_groups_async", _fake_finalize_item_groups_async)

    result = _run_character_rewrite_and_merge(
        6,
        [
            {
                "_item_id": "character-item-1",
                "name": "罗杰·艾克罗伊德",
                "aliases": ["艾克罗伊德先生"],
                "records": [[1, "record"]],
            }
        ],
    )

    assert result == [
        {
            "name": "罗杰·艾克罗伊德",
            "aliases": ["艾克罗伊德先生"],
            "records": [[1, "record"]],
        }
    ]
    assert len(run_calls) == 1


def test_build_second_pass_candidate_groups_buckets_rename_and_title_residuals():
    groups = _build_second_pass_candidate_groups(
        [
            {
                "_item_id": "item-1",
                "name": "雷娜塔·叶夫根尼·契切林",
                "aliases": [],
                "records": [[222, "她被零号宣布买下并改名为零，在风雪中回应。"]],
            },
            {
                "_item_id": "item-2",
                "name": "零",
                "aliases": [],
                "records": [[39, "她自称Zero。"]],
            },
            {
                "_item_id": "item-3",
                "name": "马突尔",
                "aliases": [],
                "records": [[230, "装备部研究员，提出方案A。"]],
            },
            {
                "_item_id": "item-4",
                "name": "马突尔研究员",
                "aliases": [],
                "records": [[529, "观测到高温反应。"]],
            },
        ]
    )

    signatures = {tuple(group) for group in groups}

    assert ("item-1", "item-2") in signatures
    assert ("item-3", "item-4") in signatures
