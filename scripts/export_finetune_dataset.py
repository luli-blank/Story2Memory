from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import pymysql
from dotenv import load_dotenv

from database.mysql_client import MySQLChatStore
from rag.finetune_dataset import (
    build_messages_training_record,
    build_raw_training_record,
    is_chapter_row_usable,
    resolve_book_splits,
)


def _load_runtime_env() -> None:
    load_dotenv(ROOT_DIR / ".env")
    override_path = os.getenv("STORY2MEMORY_ENV_OVERRIDE")
    if override_path:
        load_dotenv(override_path, override=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export story-writing finetune dataset from MySQL.")
    parser.add_argument(
        "--output-dir",
        default=str(ROOT_DIR / "output" / "finetune_dataset"),
        help="Directory for exported JSONL files.",
    )
    parser.add_argument("--book-ids", default="", help="Optional comma-separated book ids to export.")
    parser.add_argument(
        "--train-book-ids",
        default="",
        help="Optional comma-separated train book ids. If set, split is explicit.",
    )
    parser.add_argument(
        "--validation-book-ids",
        default="",
        help="Optional comma-separated validation book ids. If set, split is explicit.",
    )
    parser.add_argument("--eval-ratio", type=float, default=0.2, help="Book-level validation ratio.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for book-level split.")
    parser.add_argument(
        "--previous-tail-chars",
        type=int,
        default=600,
        help="How many chars from the previous accepted segment to include as continuity context.",
    )
    parser.add_argument(
        "--min-content-chars",
        type=int,
        default=200,
        help="Skip chapter rows whose content is shorter than this threshold.",
    )
    return parser.parse_args()


def _parse_csv_ids(raw_value: str) -> list[int]:
    values: list[int] = []
    for token in str(raw_value or "").split(","):
        text = token.strip()
        if not text:
            continue
        values.append(int(text))
    return values


def _connect():
    _load_runtime_env()
    conn_cfg = MySQLChatStore._parse_mysql_dsn(str(os.getenv("MYSQL_DSN", "")).strip())
    if not conn_cfg:
        raise RuntimeError("Missing or invalid MYSQL_DSN.")
    try:
        return pymysql.connect(**conn_cfg)
    except pymysql.MySQLError as exc:
        host = str(conn_cfg.get("host") or "")
        port = int(conn_cfg.get("port") or 3306)
        raise RuntimeError(
            "Failed to connect to MySQL. "
            f"MYSQL_DSN points to {host}:{port}. "
            "If you are running from the host machine, make sure this host is reachable from the current network "
            "(for example use 127.0.0.1:13306 instead of the Docker service name `mysql` when appropriate)."
        ) from exc


def _load_books(book_ids: list[int] | None) -> list[dict[str, Any]]:
    with _connect() as conn:
        with conn.cursor() as cursor:
            if book_ids:
                placeholders = ", ".join(["%s"] * len(book_ids))
                cursor.execute(
                    f"""
                    SELECT id AS book_id, title AS book_title, author
                    FROM books
                    WHERE id IN ({placeholders})
                    ORDER BY id ASC
                    """,
                    tuple(book_ids),
                )
            else:
                cursor.execute(
                    """
                    SELECT id AS book_id, title AS book_title, author
                    FROM books
                    ORDER BY id ASC
                    """
                )
            return list(cursor.fetchall() or [])


def _load_book_rows(book_id: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    c.book_id,
                    c.chapter_index,
                    c.content,
                    c.chapter_summary,
                    c.raw_summary_json,
                    c.plot_id,
                    p.plot_summary,
                    v.volume_summary
                FROM book_chapters c
                LEFT JOIN book_plots p
                    ON p.book_id = c.book_id
                   AND p.plot_id = c.plot_id
                LEFT JOIN book_volumes v
                    ON v.book_id = p.book_id
                   AND v.volume_index = p.volume_id
                WHERE c.book_id = %s
                  AND c.status = 'success'
                ORDER BY c.chapter_index ASC, c.id ASC
                """,
                (book_id,),
            )
            return list(cursor.fetchall() or [])


def _tail_text(text: str, max_chars: int) -> str:
    normalized = str(text or "").strip()
    if max_chars <= 0:
        return ""
    if len(normalized) <= max_chars:
        return normalized
    return normalized[-max_chars:]


def _export_split(
    *,
    split: str,
    output_dir: Path,
    books: list[dict[str, Any]],
    previous_tail_chars: int,
    min_content_chars: int,
) -> dict[str, Any]:
    raw_path = output_dir / f"{split}.raw.jsonl"
    messages_path = output_dir / f"{split}.messages.jsonl"
    sample_count = 0
    skipped_count = 0

    with raw_path.open("w", encoding="utf-8") as raw_file, messages_path.open("w", encoding="utf-8") as messages_file:
        for book in books:
            previous_accepted_content = ""
            for row in _load_book_rows(int(book["book_id"])):
                if not is_chapter_row_usable(row, min_content_chars=min_content_chars):
                    skipped_count += 1
                    continue
                raw_record = build_raw_training_record(
                    split=split,
                    book_row=book,
                    chapter_row=row,
                    plot_row=row,
                    volume_row=row,
                    previous_text_tail=_tail_text(previous_accepted_content, previous_tail_chars),
                )
                messages_record = build_messages_training_record(raw_record)
                raw_file.write(json.dumps(raw_record, ensure_ascii=False) + "\n")
                messages_file.write(json.dumps(messages_record, ensure_ascii=False) + "\n")
                previous_accepted_content = str(row.get("content") or "")
                sample_count += 1

    return {
        "split": split,
        "book_ids": [int(book["book_id"]) for book in books],
        "book_count": len(books),
        "sample_count": sample_count,
        "skipped_count": skipped_count,
        "raw_path": str(raw_path),
        "messages_path": str(messages_path),
    }


def main() -> int:
    args = _parse_args()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    requested_book_ids = _parse_csv_ids(args.book_ids)
    books = _load_books(requested_book_ids or None)
    if not books:
        raise RuntimeError("No books found for export.")

    available_book_ids = [int(book["book_id"]) for book in books]
    train_ids, validation_ids = resolve_book_splits(
        available_book_ids,
        train_book_ids=_parse_csv_ids(args.train_book_ids) if args.train_book_ids.strip() else None,
        validation_book_ids=_parse_csv_ids(args.validation_book_ids) if args.validation_book_ids.strip() else None,
        eval_ratio=args.eval_ratio,
        seed=args.seed,
    )

    book_map = {int(book["book_id"]): book for book in books}
    train_books = [book_map[book_id] for book_id in train_ids]
    validation_books = [book_map[book_id] for book_id in validation_ids]

    manifest = {
        "output_dir": str(output_dir),
        "split_strategy": {
            "type": "book_level_holdout",
            "eval_ratio": args.eval_ratio,
            "seed": args.seed,
            "requested_book_ids": requested_book_ids,
            "train_book_ids": train_ids,
            "validation_book_ids": validation_ids,
        },
        "export_config": {
            "previous_tail_chars": args.previous_tail_chars,
            "min_content_chars": args.min_content_chars,
        },
        "splits": [],
    }

    manifest["splits"].append(
        _export_split(
            split="train",
            output_dir=output_dir,
            books=train_books,
            previous_tail_chars=args.previous_tail_chars,
            min_content_chars=args.min_content_chars,
        )
    )
    if validation_books:
        manifest["splits"].append(
            _export_split(
                split="validation",
                output_dir=output_dir,
                books=validation_books,
                previous_tail_chars=args.previous_tail_chars,
                min_content_chars=args.min_content_chars,
            )
        )

    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
