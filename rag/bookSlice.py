from __future__ import annotations

import re
from typing import Any

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


def slice_book_by_chapter(text: str) -> list[dict[str, Any]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    matches = list(CHAPTER_HEADING_PATTERN.finditer(normalized))
    if not matches:
        matches = list(SECTION_HEADING_PATTERN.finditer(normalized))

    if not matches:
        content = normalized.strip()
        return [
            {
                "chapter_index": 1,
                "title": "第1章 全文",
                "content": content,
                "word_count": _count_words(content),
            }
        ]

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

    return chunks
