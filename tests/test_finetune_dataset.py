from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag.finetune_dataset import (
    build_messages_training_record,
    build_raw_training_record,
    normalize_chapter_summary,
    resolve_book_splits,
)


def test_normalize_chapter_summary_prefers_raw_summary_json():
    summary = normalize_chapter_summary(
        {
            "chapter_summary": "fallback summary",
            "raw_summary_json": {
                "chapter_summary": "结构化摘要",
            },
        }
    )

    assert summary == "结构化摘要"


def test_build_raw_and_messages_training_record_include_multilevel_context():
    raw_record = build_raw_training_record(
        split="train",
        book_row={"book_id": 7, "book_title": "镜海纪事", "author": "匿名作者"},
        chapter_row={
            "chapter_index": 12,
            "chapter_title": "第12章 雨夜高架桥",
            "content": "正文内容",
            "word_count": 1234,
            "raw_summary_json": {
                "chapter_summary": "赵明在雨夜抵达高架桥，危机逼近。",
                "character": [{"name": "赵明", "description": "在桥上直面危机。"}],
                "organizations": [{"name": "星桥学院", "description": "远程介入当前局势。"}],
            },
        },
        plot_row={"plot_summary": "主角进入关键危机场景。"},
        volume_row={"volume_summary": "主角逐步进入镜海纪事世界。"},
        previous_text_tail="前文尾巴",
    )

    assert raw_record["sample_id"] == "7:12"
    assert raw_record["conditioning"]["volume_summary"] == "主角逐步进入镜海纪事世界。"
    assert raw_record["conditioning"]["chapter_summary"] == "赵明在雨夜抵达高架桥，危机逼近。"
    assert "前文尾巴" in raw_record["instruction"]

    messages_record = build_messages_training_record(raw_record)
    assert len(messages_record["messages"]) == 3
    assert messages_record["messages"][1]["role"] == "user"
    assert "章节摘要：赵明在雨夜抵达高架桥" in messages_record["messages"][1]["content"]
    assert "角色信息" not in messages_record["messages"][1]["content"]
    assert messages_record["messages"][2]["content"] == "正文内容"


def test_resolve_book_splits_keeps_book_level_holdout_disjoint():
    train_ids, validation_ids = resolve_book_splits([1, 2, 3, 4], eval_ratio=0.25, seed=7)

    assert sorted(train_ids + validation_ids) == [1, 2, 3, 4]
    assert set(train_ids).isdisjoint(validation_ids)
    assert len(validation_ids) == 1
