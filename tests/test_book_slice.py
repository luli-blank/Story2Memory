from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag.bookSlice import apply_overlong_chapter_split, slice_book_by_chapter


def _segment(length: int, marker: str) -> str:
    return marker * (length - 1) + "。"


def test_apply_overlong_chapter_split_breaks_long_chapter_near_threshold():
    long_content = "".join(
        [
            _segment(3490, "甲"),
            _segment(3490, "乙"),
            _segment(3490, "丙"),
            _segment(1490, "丁"),
        ]
    )
    chapters = [
        {
            "chapter_index": 1,
            "title": "前言",
            "content": long_content,
            "word_count": len(long_content),
        }
    ]

    split = apply_overlong_chapter_split(chapters, max_chars=3500)

    assert len(split) == 4
    assert [item["chapter_index"] for item in split] == [1, 2, 3, 4]
    assert [item["title"] for item in split] == ["前言-1", "前言-2", "前言-3", "前言-4"]
    assert [item["word_count"] for item in split] == [3490, 3490, 3490, 1490]
    assert all(str(item["content"]).endswith("。") for item in split)


def test_slice_book_by_chapter_splits_single_untitled_body():
    long_text = "".join(
        [
            _segment(3490, "甲"),
            _segment(3490, "乙"),
            _segment(3490, "丙"),
            _segment(1490, "丁"),
        ]
    )

    chapters = slice_book_by_chapter(long_text)

    assert len(chapters) == 4
    assert [item["title"] for item in chapters] == ["第1章 全文-1", "第1章 全文-2", "第1章 全文-3", "第1章 全文-4"]
    assert [item["word_count"] for item in chapters] == [3490, 3490, 3490, 1490]


def test_slice_book_by_chapter_splits_prefix_and_preserves_heading_order():
    prefix = _segment(3490, "序") + _segment(3490, "章")
    body = "第1章 开始\n" + _segment(80, "文") + "\n第2章 继续\n" + _segment(60, "字")

    chapters = slice_book_by_chapter(prefix + "\n" + body)

    assert len(chapters) == 4
    assert [item["chapter_index"] for item in chapters] == [1, 2, 3, 4]
    assert [item["title"] for item in chapters] == ["前言-1", "前言-2", "第1章 开始", "第2章 继续"]
    assert [item["word_count"] for item in chapters[:2]] == [3490, 3490]
