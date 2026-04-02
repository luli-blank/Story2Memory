from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reflex_app.frontend.bookshelf import bookshelf_view


def test_bookshelf_view_builds_without_invalid_grid_columns():
    bookshelf_view()
