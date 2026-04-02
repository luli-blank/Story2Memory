from __future__ import annotations

import argparse
from collections import Counter
import re
from pathlib import Path


CHAPTER_PATTERN = re.compile(
    r"^\s*第([0-9零一二三四五六七八九十百千万两〇]+)章[^\n\r]*\s*$",
    re.MULTILINE,
)

_CN_NUM_MAP = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNIT_MAP = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def _cn_to_int(value: str) -> int | None:
    text = value.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)

    total = 0
    section = 0
    number = 0
    for ch in text:
        if ch in _CN_NUM_MAP:
            number = _CN_NUM_MAP[ch]
            continue
        unit = _CN_UNIT_MAP.get(ch)
        if unit is None:
            return None
        if unit == 10000:
            section = (section + number) * unit
            total += section
            section = 0
            number = 0
            continue
        if number == 0:
            number = 1
        section += number * unit
        number = 0
    return total + section + number


def clear_txt_after_chapter(
    file_path: str | Path,
    keep_chapter: int,
    output_path: str | Path | None = None,
    encoding: str = "utf-8",
) -> Path:
    if keep_chapter <= 0:
        raise ValueError("keep_chapter must be a positive integer.")

    source_path = Path(file_path)
    if source_path.suffix.lower() != ".txt":
        raise ValueError("Only .txt files are supported.")
    if not source_path.exists():
        raise FileNotFoundError(source_path)

    text = source_path.read_text(encoding=encoding)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    matches = list(CHAPTER_PATTERN.finditer(normalized))
    if not matches:
        target = Path(output_path) if output_path else source_path
        target.write_text(normalized, encoding=encoding)
        return target

    keep_index = None
    for index, match in enumerate(matches):
        chapter_no = _cn_to_int(match.group(1))
        if chapter_no == keep_chapter:
            keep_index = index
            break
    if keep_index is None and keep_chapter <= len(matches):
        keep_index = keep_chapter - 1
    if keep_index is None:
        keep_index = len(matches) - 1

    parts: list[str] = [normalized[: matches[0].start()]]
    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        if index <= keep_index:
            parts.append(normalized[match.start() : next_start])
        else:
            heading = normalized[match.start() : match.end()].rstrip()
            parts.append(f"{heading}\n\n")

    tailored_text = "".join(parts).rstrip() + "\n"
    target = Path(output_path) if output_path else source_path
    target.write_text(tailored_text, encoding=encoding)
    return target


def count_chapters(file_path: str | Path, encoding: str = "utf-8") -> int:
    source_path = Path(file_path)
    if source_path.suffix.lower() != ".txt":
        raise ValueError("Only .txt files are supported.")
    if not source_path.exists():
        raise FileNotFoundError(source_path)

    text = source_path.read_text(encoding=encoding)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return len(list(CHAPTER_PATTERN.finditer(normalized)))


def summarize_chapters(file_path: str | Path, encoding: str = "utf-8") -> tuple[int, dict[str, int]]:
    source_path = Path(file_path)
    if source_path.suffix.lower() != ".txt":
        raise ValueError("Only .txt files are supported.")
    if not source_path.exists():
        raise FileNotFoundError(source_path)

    text = source_path.read_text(encoding=encoding)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    chapter_keys = [f"第{match.group(1)}章" for match in CHAPTER_PATTERN.finditer(normalized)]
    counter = Counter(chapter_keys)
    duplicates = {name: count for name, count in counter.items() if count > 1}
    return len(chapter_keys), duplicates


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize chapter headings and detect duplicates.")
    parser.add_argument("--file", required=True, help="Path to input .txt file.")
    parser.add_argument("--encoding", default="utf-8", help="File encoding. Default: utf-8.")
    return parser


if __name__ == "__main__":
    args = _build_parser().parse_args()
    chapter_count, duplicates = summarize_chapters(
        file_path=args.file,
        encoding=args.encoding,
    )
    print(f"[storyTailor] chapter_count: {chapter_count}")
    print(f"[storyTailor] has_duplicates: {'yes' if duplicates else 'no'}")
    if duplicates:
        print("[storyTailor] duplicate_chapters:")
        for chapter_name, repeated_count in sorted(duplicates.items()):
            print(f"- {chapter_name}: {repeated_count}")
