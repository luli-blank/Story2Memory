from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reflex_app.frontend.character_archive import character_archive_view
from reflex_app.state import CharacterArchiveCard, NovelState


def _archive_item(item_id: int, record_count: int) -> CharacterArchiveCard:
    return CharacterArchiveCard(
        id=item_id,
        name=f"角色{item_id}",
        record_count=record_count,
        first_chapter_index=1,
        last_chapter_index=1,
    )


def test_character_archive_uses_default_threshold_of_20():
    state = NovelState()
    state.character_archive_items = [
        _archive_item(1, 20),
        _archive_item(2, 21),
        _archive_item(3, 50),
    ]

    assert state.character_archive_record_threshold == 20
    assert [item.id for item in state.character_archive_filtered_items] == [2, 3]


def test_character_archive_threshold_change_resets_page_and_filters_items():
    state = NovelState()
    state.character_archive_items = [
        _archive_item(1, 1),
        _archive_item(2, 5),
        _archive_item(3, 6),
        _archive_item(4, 12),
    ]
    state.character_archive_page = 3

    state.set_character_archive_record_threshold("5")

    assert state.character_archive_record_threshold == 5
    assert state.character_archive_page == 1
    assert [item.id for item in state.character_archive_filtered_items] == [3, 4]


def test_character_archive_pagination_uses_filtered_items():
    state = NovelState()
    state.character_archive_items = [_archive_item(item_id, 21) for item_id in range(1, 13)] + [
        _archive_item(13, 5),
        _archive_item(14, 20),
    ]

    assert state.character_archive_total_pages == 2
    assert [item.id for item in state.character_archive_page_items] == list(range(1, 11))

    state.character_archive_page = 2

    assert [item.id for item in state.character_archive_page_items] == [11, 12]


def test_character_archive_view_builds_with_threshold_select():
    character_archive_view()
