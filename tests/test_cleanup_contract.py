from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_stale_files_removed():
    for path in [
        "main.py",
        "config.py",
        "database/neo4j_client.py",
        "database/redis_client.py",
        "app/__init__.py",
        "agent/skills/retrieval_route_skill/sample_inputs.json",
    ]:
        assert not (REPO_ROOT / path).exists(), f"stale tracked file should be removed: {path}"


def test_unused_prompt_definitions_removed():
    for path, signature in [
        ("agent/prompt.py", "ROLEPLAY_CONTENT_SEARCH_REWRITE_PROMPT ="),
        ("agent/prompt.py", "SYSTEM_PROMPT_TEMPLATE ="),
        ("rag/prompt.py", "CHARACTER_PROFILE_FINAL_PROMPT ="),
        ("rag/prompt.py", "CHARACTER_RELATION_SUMMARY_PROMPT ="),
        ("rag/prompt.py", "CHARACTER_RELATION_HISTORY_PROMPT ="),
    ]:
        lines = _read(path).splitlines()
        assert not any(line.startswith(signature) for line in lines), f"unused prompt should be removed: {signature}"


def test_search_agent_legacy_helpers_removed():
    content = _read("agent/searchAgent.py")
    for symbol in [
        "def _should_enter_recovery(",
        "def _run_recovery_mode(",
        "def _build_rendered_answer(",
        "def _run_parallel_entry_tools(",
        "def _resolve_subquery_fixed_path(",
        "plan_multi_search_route, plan_search_route",
    ]:
        assert symbol not in content, f"legacy searchAgent code should be removed: {symbol}"


def test_misc_dead_code_removed():
    assert 'f"{MULTI_QUERY_PLANNER_PROMPT}\\n\\n"' not in _read("agent/skills/retrieval_route_skill/route_skill.py")
    assert "total_parts = len(pieces)" not in _read("rag/bookSlice.py")
    assert "plot_segmentation_stats = PlotSegmentationEngine().run(book_id)" not in _read("rag/uploadBook.py")


def test_second_batch_stale_files_removed():
    for path in [
        "rag/storyTailor.py",
        "database/mysql/README.md",
        "docs/superpowers/specs/2026-03-31-open-source-docker-design.md",
        "docs/superpowers/plans/2026-03-31-open-source-dockerization.md",
        "agent/skills/retrieval_route_skill/demo.py",
    ]:
        assert not (REPO_ROOT / path).exists(), f"second-batch stale file should be removed: {path}"


def test_retrieval_route_skill_doc_no_longer_points_to_deleted_demo():
    assert "python agent/skills/retrieval_route_skill/demo.py" not in _read("agent/skills/retrieval_route_skill/SKILL.md")
