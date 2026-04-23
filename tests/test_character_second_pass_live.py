import json
import os
from pathlib import Path
import sys
import asyncio

import pymysql
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag.createCharacters import _load_active_character_items, _run_second_pass_merge_diagnostic_async


def _connect():
    return pymysql.connect(
        host="127.0.0.1",
        port=13306,
        user="story2memory",
        password="story2memory-local-db-pass",
        database="novel_cognition",
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def _count_active_characters(book_id: int) -> int:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM characters WHERE book_id=%s AND need_delete='no'",
                (int(book_id),),
            )
            return int((cur.fetchone() or {}).get("c") or 0)


def _group_names(groups):
    return [sorted({str(item.get("name") or "").strip() for item in group if str(item.get("name") or "").strip()}) for group in groups]


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_BOOK8_SECOND_PASS_TEST") != "1",
    reason="set RUN_LIVE_BOOK8_SECOND_PASS_TEST=1 to run live diagnostic",
)
def test_book8_second_pass_merge_diagnostic_live():
    os.environ["MYSQL_DSN"] = os.getenv(
        "TEST_MYSQL_DSN",
        "mysql+pymysql://story2memory:story2memory-local-db-pass@127.0.0.1:13306/novel_cognition",
    )
    before_count = _count_active_characters(8)
    focus_names = {
        "零",
        "零号",
        "雷娜塔·叶夫根尼·契切林",
        "鹿天铭",
        "鹿董事长",
        "鹿芒",
        "鹿姓男生",
        "黑王",
        "黑王尼德霍格",
        "霍诺利亚",
        "霍诺利亚公主",
        "须佐之男",
        "须佐之男命",
        "天照",
        "天照命",
        "月读",
        "月读命",
        "马突尔",
        "马突尔研究员",
        "龙马家主",
        "龙马弦一郎",
        "宫本",
        "宫本家主",
        "宫本志雄",
        "陈先生",
        "陈夫人",
        "陈小姐",
    }
    items = [item for item in _load_active_character_items(8) if str(item.get("name") or "").strip() in focus_names]
    result = asyncio.run(_run_second_pass_merge_diagnostic_async(8, items))
    after_count = _count_active_characters(8)

    candidate_name_groups = _group_names(result.get("candidate_groups") or [])
    resolved_name_groups = _group_names(result.get("resolved_groups") or [])
    finalized_names = sorted(str(item.get("name") or "").strip() for item in result.get("finalized_items") or [])

    positives = {
        "雷娜塔_零": any({"雷娜塔·叶夫根尼·契切林", "零"} <= set(group) for group in resolved_name_groups),
        "鹿天铭_鹿董事长": any({"鹿天铭", "鹿董事长"} <= set(group) for group in resolved_name_groups),
        "鹿芒_鹿姓男生": any({"鹿芒", "鹿姓男生"} <= set(group) for group in resolved_name_groups),
        "黑王": any({"黑王", "黑王尼德霍格"} <= set(group) for group in resolved_name_groups),
        "霍诺利亚": any({"霍诺利亚", "霍诺利亚公主"} <= set(group) for group in resolved_name_groups),
        "须佐之男": any({"须佐之男", "须佐之男命"} <= set(group) for group in resolved_name_groups),
        "天照": any({"天照", "天照命"} <= set(group) for group in resolved_name_groups),
        "月读": any({"月读", "月读命"} <= set(group) for group in resolved_name_groups),
        "马突尔": any({"马突尔", "马突尔研究员"} <= set(group) for group in resolved_name_groups),
        "龙马": any({"龙马家主", "龙马弦一郎"} <= set(group) for group in resolved_name_groups),
        "宫本家主": any({"宫本家主", "宫本志雄"} <= set(group) for group in resolved_name_groups),
    }
    negatives = {
        "零_零号": any({"零", "零号"} <= set(group) for group in resolved_name_groups),
        "宫本_宫本志雄": any({"宫本", "宫本志雄"} <= set(group) for group in resolved_name_groups),
    }

    print("book8_second_pass_before_count", before_count)
    print("book8_second_pass_after_count", after_count)
    print("book8_second_pass_focus_item_count", len(items))
    print("book8_second_pass_candidate_group_count", len(candidate_name_groups))
    print("book8_second_pass_resolved_group_count", len(resolved_name_groups))
    print("book8_second_pass_candidate_groups", json.dumps(candidate_name_groups, ensure_ascii=False))
    print("book8_second_pass_resolved_groups", json.dumps(resolved_name_groups, ensure_ascii=False))
    print("book8_second_pass_positive_checks", json.dumps(positives, ensure_ascii=False))
    print("book8_second_pass_negative_checks", json.dumps(negatives, ensure_ascii=False))
    print("book8_second_pass_finalized_names_sample", json.dumps(finalized_names[:80], ensure_ascii=False))

    assert before_count == after_count
    assert isinstance(result.get("finalized_items"), list)
