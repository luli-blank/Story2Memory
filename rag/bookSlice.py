from __future__ import annotations

import re
from typing import Any

MAX_CHAPTER_SEGMENT_CHARS = 3500
SENTENCE_END_MARKERS = "。！？!?."

CHAPTER_HEADING_PATTERN = re.compile(
    r"^\s*(第[0-9零一二三四五六七八九十百千万两〇]+章[^\n\r]*)\s*$",
    re.MULTILINE,
)
SECTION_HEADING_PATTERN = re.compile(
    r"^\s*(第[0-9零一二三四五六七八九十百千万两〇]+节[^\n\r]*)\s*$",
    re.MULTILINE,
)


def _count_words(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def _find_raw_index_for_word_offset(text: str, target_words: int) -> int:
    if target_words <= 0:
        return 0
    counted = 0
    for index, char in enumerate(text):
        if char.isspace():
            continue
        counted += 1
        if counted >= target_words:
            return index + 1
    return len(text)


def _find_sentence_break_index(text: str, target_words: int) -> int:
    target_index = _find_raw_index_for_word_offset(text, target_words)
    if target_index <= 0 or target_index >= len(text):
        return target_index

    prev_positions = [text.rfind(marker, 0, target_index) for marker in SENTENCE_END_MARKERS]
    next_positions = [text.find(marker, target_index) for marker in SENTENCE_END_MARKERS]

    candidates: list[int] = []
    for position in prev_positions:
        if position >= 0:
            candidates.append(position + 1)
    for position in next_positions:
        if position >= 0:
            candidates.append(position + 1)

    if not candidates:
        return target_index

    best_index = min(candidates, key=lambda value: (abs(value - target_index), value))
    if best_index <= 0 or best_index >= len(text):
        return target_index
    return best_index


def _split_long_content(text: str, max_chars: int) -> list[str]:
    normalized = str(text or "").strip()
    if not normalized:
        return [""]

    segments: list[str] = []
    remaining = normalized
    while _count_words(remaining) > max_chars:
        split_index = _find_sentence_break_index(remaining, max_chars)
        left = remaining[:split_index].strip()
        right = remaining[split_index:].strip()
        if not left or not right:
            break
        segments.append(left)
        remaining = right

    segments.append(remaining)
    return [segment for segment in segments if segment.strip()]


def apply_overlong_chapter_split(
    chapters: list[dict[str, Any]],
    *,
    max_chars: int = MAX_CHAPTER_SEGMENT_CHARS,
) -> list[dict[str, Any]]:
    normalized_chapters: list[dict[str, Any]] = []

    for chapter in chapters:
        title = str(chapter.get("title") or "").strip() or "未命名章节"
        content = str(chapter.get("content") or "").strip()
        pieces = _split_long_content(content, max_chars=max_chars)
        if len(pieces) == 1:
            normalized_chapters.append(
                {
                    "chapter_index": len(normalized_chapters) + 1,
                    "title": title,
                    "content": pieces[0],
                    "word_count": _count_words(pieces[0]),
                }
            )
            continue

        total_parts = len(pieces)
        for part_index, piece in enumerate(pieces, start=1):
            normalized_chapters.append(
                {
                    "chapter_index": len(normalized_chapters) + 1,
                    "title": f"{title}-{part_index}",
                    "content": piece,
                    "word_count": _count_words(piece),
                }
            )

    return normalized_chapters


def slice_book_by_chapter(text: str) -> list[dict[str, Any]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    matches = list(CHAPTER_HEADING_PATTERN.finditer(normalized))
    if not matches:
        matches = list(SECTION_HEADING_PATTERN.finditer(normalized))

    if not matches:
        content = normalized.strip()
        return apply_overlong_chapter_split(
            [
                {
                    "chapter_index": 1,
                    "title": "第1章 全文",
                    "content": content,
                    "word_count": _count_words(content),
                }
            ]
        )

    chunks: list[dict[str, Any]] = []
    prefix = normalized[: matches[0].start()].strip()
    if prefix:
        chunks.append(
            {
                "chapter_index": 1,
                "title": "前言",
                "content": prefix,
                "word_count": _count_words(prefix),
            }
        )

    for index, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        content = normalized[start:end].strip()
        chunks.append(
            {
                "chapter_index": len(chunks) + 1,
                "title": title,
                "content": content,
                "word_count": _count_words(content),
            }
        )

    return apply_overlong_chapter_split(chunks)
