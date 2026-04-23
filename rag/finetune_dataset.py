from __future__ import annotations

import json
import random
from typing import Any

NON_NARRATIVE_SENTINEL = "非小说片段：无实质性叙事内容"


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _parse_json_object(raw_value: Any) -> dict[str, Any]:
    if isinstance(raw_value, dict):
        return raw_value
    if isinstance(raw_value, str):
        text = raw_value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def normalize_chapter_summary(chapter_row: dict[str, Any]) -> str:
    raw_payload = _parse_json_object(chapter_row.get("raw_summary_json"))
    return _normalize_text(raw_payload.get("chapter_summary")) or _normalize_text(
        chapter_row.get("chapter_summary")
    )


def is_chapter_row_usable(chapter_row: dict[str, Any], *, min_content_chars: int = 200) -> bool:
    content = _normalize_text(chapter_row.get("content"))
    if len(content) < max(1, int(min_content_chars)):
        return False

    chapter_summary = normalize_chapter_summary(chapter_row)
    if not chapter_summary or chapter_summary == NON_NARRATIVE_SENTINEL:
        return False

    return True


def build_user_prompt(record: dict[str, Any]) -> str:
    conditioning = record["conditioning"]
    prompt_parts = ["请根据以下摘要和上下文，写出当前章节片段的正文。", ""]

    if conditioning["volume_summary"]:
        prompt_parts.extend([f"卷级摘要：{conditioning['volume_summary']}", ""])
    if conditioning["plot_summary"]:
        prompt_parts.extend([f"情节摘要：{conditioning['plot_summary']}", ""])

    prompt_parts.extend(
        [
            f"章节摘要：{conditioning['chapter_summary'] or '无'}",
            "",
            f"上一段末尾：{conditioning['previous_text_tail'] or '无'}",
            "",
            "要求：",
            "1. 只输出正文，不要解释，不要列点。",
            "2. 内容必须与给定摘要一致。",
            "3. 保持小说叙事连贯，延续上一段衔接。",
        ]
    )
    return "\n".join(prompt_parts)


def build_raw_training_record(
    *,
    split: str,
    book_row: dict[str, Any],
    chapter_row: dict[str, Any],
    plot_row: dict[str, Any] | None,
    volume_row: dict[str, Any] | None,
    previous_text_tail: str,
) -> dict[str, Any]:
    chapter_summary = normalize_chapter_summary(chapter_row)
    plot_row = plot_row or {}
    volume_row = volume_row or {}

    record = {
        "record_type": "story_segment_sft",
        "split": split,
        "sample_id": f"{int(book_row.get('book_id') or 0)}:{int(chapter_row.get('chapter_index') or 0)}",
        "conditioning": {
            "volume_summary": _normalize_text(volume_row.get("volume_summary")),
            "plot_summary": _normalize_text(plot_row.get("plot_summary")),
            "chapter_summary": chapter_summary,
            "previous_text_tail": _normalize_text(previous_text_tail),
        },
        "target_text": _normalize_text(chapter_row.get("content")),
    }
    record["instruction"] = build_user_prompt(record)
    return record


def build_messages_training_record(raw_record: dict[str, Any]) -> dict[str, Any]:
    return {
        "messages": [
            {
                "role": "system",
                "content": "你是一位中文长篇小说写作助手。请在不偏离设定和摘要的前提下，输出连贯的章节正文。",
            },
            {"role": "user", "content": build_user_prompt(raw_record)},
            {"role": "assistant", "content": _normalize_text(raw_record.get("target_text"))},
        ]
    }


def resolve_book_splits(
    available_book_ids: list[int],
    *,
    train_book_ids: list[int] | None = None,
    validation_book_ids: list[int] | None = None,
    eval_ratio: float = 0.2,
    seed: int = 42,
) -> tuple[list[int], list[int]]:
    available = sorted({int(book_id) for book_id in available_book_ids if int(book_id) > 0})
    if not available:
        raise ValueError("No available books to split.")

    if train_book_ids is not None or validation_book_ids is not None:
        train_set = {int(book_id) for book_id in (train_book_ids or []) if int(book_id) > 0}
        validation_set = {int(book_id) for book_id in (validation_book_ids or []) if int(book_id) > 0}
        unknown = (train_set | validation_set) - set(available)
        if unknown:
            raise ValueError(f"Unknown book ids in split configuration: {sorted(unknown)}")
        overlap = train_set & validation_set
        if overlap:
            raise ValueError(f"Train/validation splits overlap: {sorted(overlap)}")

        if train_book_ids is None:
            train_set = set(available) - validation_set
        if validation_book_ids is None:
            validation_set = set(available) - train_set
        if not train_set:
            raise ValueError("Train split is empty.")
        return sorted(train_set), sorted(validation_set)

    if len(available) == 1 or eval_ratio <= 0:
        return available, []

    eval_count = int(round(len(available) * float(eval_ratio)))
    eval_count = max(1, min(len(available) - 1, eval_count))

    shuffled = list(available)
    random.Random(int(seed)).shuffle(shuffled)
    validation = sorted(shuffled[:eval_count])
    train = sorted(set(available) - set(validation))
    return train, validation
