from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from rag.createCharacters import rebuild_characters_table
from rag.createPlotFactTables import rebuild_all_fact_tables
from rag.entity_qdrant_sync import sync_entity_collections


def rebuild_entity_tables_and_qdrant(book_id: int | None = None) -> dict[str, object]:
    character_stats = rebuild_characters_table(book_id)
    fact_table_stats = rebuild_all_fact_tables(book_id)
    qdrant_stats = sync_entity_collections(book_id=book_id)
    return {
        "characters": character_stats,
        **fact_table_stats,
        "qdrant": qdrant_stats,
    }


def main() -> None:
    print(json.dumps(rebuild_entity_tables_and_qdrant(), ensure_ascii=False))


if __name__ == "__main__":
    main()
