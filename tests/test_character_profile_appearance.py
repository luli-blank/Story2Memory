from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag.character_profiles import (
    _normalize_profile_chunk_json,
    _normalize_profile_json,
    _normalize_profile_volume_group_json,
)
from reflex_app.frontend.character_detail import _profile_tab_content
from reflex_app.state import CharacterProfileView, NovelState


def _escaped(text: str) -> str:
    return text.encode("unicode_escape").decode()


def test_normalize_profile_chunk_json_keeps_appearance_signals():
    normalized = _normalize_profile_chunk_json(
        {
            "appearance_signals": ["黑发", "身形高挑", "黑发"],
        },
        {"volume_index": 1, "volume_title": "第1卷", "chapter_start": 1, "chapter_end": 3},
    )

    assert normalized["appearance_signals"] == ["黑发", "身形高挑"]


def test_normalize_profile_volume_group_json_keeps_appearance_signals():
    normalized = _normalize_profile_volume_group_json(
        {
            "appearance_signals": ["深色长发", "神情冷冽", "深色长发"],
        },
        1,
        "第1卷",
    )

    assert normalized["appearance_signals"] == ["深色长发", "神情冷冽"]


def test_normalize_profile_json_keeps_appearance():
    normalized = _normalize_profile_json(
        {
            "identity": {"summary": "测试角色"},
            "appearance": ["黑发", "金色瞳孔", "黑发"],
        },
        {"aliases": ["测试"], "records": [(1, "初登场"), (8, "再登场")]},
        1,
        8,
        ["测试"],
    )

    assert normalized["appearance"] == ["黑发", "金色瞳孔"]


def test_normalize_character_profile_maps_appearance():
    profile = NovelState._normalize_character_profile(
        {
            "identity": {"summary": "测试角色", "aliases": ["测试"]},
            "appearance": ["黑发", "校服", "黑发"],
        }
    )

    assert profile.appearance == ["黑发", "校服"]


def test_profile_tab_content_places_appearance_after_identity_summary():
    component = _profile_tab_content(CharacterProfileView(appearance=["黑发", "校服"]))
    rendered = str(component)

    identity_pos = rendered.find(_escaped("身份概览"))
    appearance_pos = rendered.find(_escaped("外貌特征"))
    narrative_pos = rendered.find(_escaped("叙事定位"), identity_pos)

    assert identity_pos >= 0
    assert appearance_pos > identity_pos
    assert narrative_pos > appearance_pos
