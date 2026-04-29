import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag import character_profiles
from rag.character_profiles import (
    _aggregate_roleplay_relation_candidates,
    _aggregate_roleplay_style_batches,
)
from rag.prompt import (
    CHARACTER_ROLEPLAY_RELATION_BATCH_PROMPT,
    CHARACTER_ROLEPLAY_RELATION_SUMMARY_PROMPT,
)
from reflex_app.frontend.character_detail import character_detail_view
from reflex_app.state import NovelState


def test_aggregate_roleplay_style_batches_dedupes_and_weights():
    aggregated = _aggregate_roleplay_style_batches(
        [
            {
                "speech_style_signals": ["说话直白", "说话简短"],
                "style_samples": [
                    {"scene": "被上级催促时", "quote": "我马上去处理。"},
                ],
            },
            {
                "speech_style_signals": ["说话直白", "语气克制"],
                "style_samples": [
                    {"scene": "被上级催促时", "quote": "我马上去处理。"},
                    {"scene": "情绪有些恼火时", "quote": "你别闹了。"},
                ],
            },
        ]
    )

    assert aggregated["speech_style_candidates"][0]["text"] == "说话直白"
    assert aggregated["speech_style_candidates"][0]["occurrence_count"] == 2
    assert aggregated["style_samples"][0]["quote"] == "我马上去处理。"
    assert aggregated["style_samples"][0]["occurrence_count"] == 2


def test_aggregate_roleplay_relation_candidates_keeps_medium_and_fine_signals():
    aggregated = _aggregate_roleplay_relation_candidates(
        [
            {
                "emotional_relation_candidates": [
                    {
                        "target_character": "顾远",
                        "primary_relation_type": "朋友/兄弟",
                        "emotional_signals": ["信任", "依赖", "隐性爱慕"],
                        "interaction_signals": ["特殊关注", "主动联系"],
                        "explicitness": "implicit",
                        "confidence": "medium",
                        "intensity": "medium",
                        "chapter_start": 10,
                        "chapter_end": 18,
                        "evidence_chapters": [12],
                        "summary": "对顾远高度信任。",
                    },
                    {
                        "target_character": "路人甲",
                        "primary_relation_type": "",
                        "emotional_signals": ["一般合作"],
                        "interaction_signals": [],
                        "explicitness": "implicit",
                        "confidence": "low",
                        "intensity": "weak",
                        "chapter_start": 10,
                        "chapter_end": 18,
                        "evidence_chapters": [11],
                        "summary": "普通接触。",
                    },
                ]
            },
            {
                "emotional_relation_candidates": [
                    {
                        "target_character": "顾远",
                        "primary_relation_type": "爱慕/暧昧/恋爱",
                        "emotional_signals": ["牵挂", "隐性爱慕"],
                        "interaction_signals": ["区别对待"],
                        "explicitness": "implicit",
                        "confidence": "medium",
                        "intensity": "strong",
                        "chapter_start": 22,
                        "chapter_end": 30,
                        "evidence_chapters": [24],
                        "summary": "继续信任顾远。",
                    }
                ]
            },
        ]
    )

    assert len(aggregated) == 1
    assert aggregated[0]["target_character_name"] == "顾远"
    assert len(aggregated[0]["candidates"]) == 2
    assert aggregated[0]["candidates"][0]["primary_relation_type"] == "朋友/兄弟"
    assert aggregated[0]["candidates"][1]["primary_relation_type"] == "爱慕/暧昧/恋爱"


def test_normalize_character_profile_parses_roleplay_fields():
    profile = NovelState._normalize_character_profile(
        {
            "identity": {"summary": "测试角色", "aliases": ["测试"]},
            "speech_style": ["说话直接"],
            "style_samples": [{"scene": "被催促时", "quote": "我马上去。"}],
            "emotional_relations": [
                {
                    "target_character": "顾远",
                    "relation_summary": "对顾远高度信任。",
                    "primary_relation_type": "朋友/兄弟",
                    "secondary_emotional_tendencies": ["隐性爱慕"],
                    "intensity": "strong",
                    "current_status": "稳定信任",
                    "timeline": [
                        {
                            "chapter_start": 10,
                            "chapter_end": 18,
                            "summary": "建立信任",
                        }
                    ],
                }
            ],
        }
    )

    assert profile.speech_style == ["说话直接"]
    assert len(profile.style_samples) == 1
    assert profile.style_samples[0].scene == "被催促时"
    assert profile.style_samples[0].quote == "我马上去。"
    assert len(profile.emotional_relations) == 1
    assert profile.emotional_relations[0].target_character_name == "顾远"
    assert profile.emotional_relations[0].primary_relation_type == "朋友/兄弟"
    assert profile.emotional_relations[0].secondary_emotional_tendencies == ["隐性爱慕"]
    assert profile.emotional_relations[0].timeline[0].summary == "建立信任"


def test_character_detail_view_builds_with_roleplay_tabs():
    character_detail_view()


def test_roleplay_relation_prompts_are_neutral_in_tone():
    assert "隐性倾向" not in CHARACTER_ROLEPLAY_RELATION_BATCH_PROMPT
    assert "暧昧倾向持续" not in CHARACTER_ROLEPLAY_RELATION_BATCH_PROMPT
    assert "不预设任何关系倾向" in CHARACTER_ROLEPLAY_RELATION_BATCH_PROMPT
    assert "不要为了完整性而强行拔高为爱慕、暧昧或仇恨" in CHARACTER_ROLEPLAY_RELATION_SUMMARY_PROMPT


def test_roleplay_context_payload_avoids_hardcoding_romance_label():
    state = NovelState()
    state.current_book_id = 1
    state.current_novel = "示例小说"
    state.current_character_id = 1550
    state.current_character_name = "刘小雨"
    state.current_character_profile = NovelState._normalize_character_profile(
        {
            "identity": {"summary": "总部接线员", "aliases": ["刘小雨"]},
            "speech_style": ["说话直接"],
            "emotional_relations": [
                {
                    "target_character": "顾远",
                    "relation_summary": "长期特殊关注并保持信任，关系边界复杂。",
                    "primary_relation_type": "爱慕/暧昧/恋爱",
                    "secondary_emotional_tendencies": ["信任", "牵挂"],
                    "intensity": "medium",
                    "current_status": "仍保持特殊关注",
                    "timeline": [{"chapter_start": 22, "chapter_end": 65, "summary": "成为专属接线员"}],
                }
            ],
        }
    )

    payload = state._build_roleplay_context_payload()
    assert "顾远" in payload["persona_summary"]
    assert "爱慕/暧昧/恋爱" not in payload["persona_summary"]
    assert "未明确确认私人关系" in payload["persona_summary"]


def test_open_character_detail_refreshes_roleplay_greeting_after_snapshot(monkeypatch):
    state = NovelState()
    state.current_book_id = 1
    state.current_novel = "示例小说"
    state.current_character_id = 100
    state.current_character_name = "赵明"
    state.chat_mode = "roleplay"
    state.chat_messages = state._roleplay_chat_messages()

    def fake_get_character_archive_snapshot(book_id: int, character_id: int):
        assert book_id == 1
        assert character_id == 200
        return {
            "character": {
                "id": 200,
                "name": "林夏",
                "alias_preview": ["林夏旧名"],
                "record_count": 88,
                "first_chapter_index": 3,
                "last_chapter_index": 232,
            },
            "profile": {},
            "relations": [],
            "has_cached_result": True,
        }

    monkeypatch.setattr(
        character_profiles,
        "get_character_archive_snapshot",
        fake_get_character_archive_snapshot,
    )

    async def run_open_character_detail():
        async for _ in state.open_character_detail(200):
            pass

    asyncio.run(run_open_character_detail())

    assert state.current_character_name == "林夏"
    assert len(state.chat_messages) == 1
    assert "《林夏》" in state.chat_messages[0].content
    assert "《赵明》" not in state.chat_messages[0].content
