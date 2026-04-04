from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent.hybridSearch as hybrid_search


def test_rerank_candidates_always_falls_back_without_rerank(monkeypatch):
    candidates = [
        {"id": 1, "fused_score": 0.9, "payload": {"chapter_summary": "alpha"}},
        {"id": 2, "fused_score": 0.8, "payload": {"chapter_summary": "beta"}},
    ]

    monkeypatch.setenv("RERANK_DISABLED", "0")

    def _unexpected_rerank_client():
        raise AssertionError("rerank client should not be used")

    monkeypatch.setattr(hybrid_search, "_get_rerank_client", _unexpected_rerank_client)

    ranked, rerank_mode = hybrid_search._rerank_candidates(
        query="test query",
        candidates=candidates,
        text_field="chapter_summary",
        top_n=2,
    )

    assert rerank_mode == "rerank_disabled"
    assert [item["id"] for item in ranked] == [1, 2]
    assert all(item["rerank_score"] is None for item in ranked)
    assert [item["rerank_rank"] for item in ranked] == [1, 2]


def test_warm_hybrid_runtime_skips_rerank_prewarm(monkeypatch):
    monkeypatch.setattr(hybrid_search, "get_qdrant_embedding_store", lambda: object())
    monkeypatch.setattr(hybrid_search, "_get_embedding_query_client", lambda: object())
    monkeypatch.setattr(hybrid_search, "_get_sparse_encoder", lambda: object())
    monkeypatch.setattr(hybrid_search, "_get_hybrid_filter_mode", lambda: "never")

    def _unexpected_rerank_client():
        raise AssertionError("rerank prewarm should be skipped")

    monkeypatch.setattr(hybrid_search, "_get_rerank_client", _unexpected_rerank_client)

    hybrid_search.warm_hybrid_runtime()


def test_sparse_score_dense_candidates_uses_token_overlap_when_sparse_disabled(monkeypatch):
    monkeypatch.delenv("HYBRID_SPARSE_RETRIEVAL_ENABLED", raising=False)

    def _unexpected_sparse_encoder():
        raise AssertionError("sparse encoder should not be used when sparse retrieval is disabled")

    monkeypatch.setattr(hybrid_search, "_get_sparse_encoder", _unexpected_sparse_encoder)

    hits, mode = hybrid_search._sparse_score_dense_candidates(
        query="刘备",
        dense_hits=[
            {
                "id": 1,
                "payload": {"chapter_summary": "刘备 关羽 张飞 桃园结义"},
            }
        ],
        text_field="chapter_summary",
        limit=5,
    )

    assert mode == "token_overlap_dense_pool"
    assert [item["id"] for item in hits] == [1]


def test_warm_hybrid_runtime_skips_sparse_prewarm_when_sparse_disabled(monkeypatch):
    monkeypatch.delenv("HYBRID_SPARSE_RETRIEVAL_ENABLED", raising=False)
    monkeypatch.setattr(hybrid_search, "get_qdrant_embedding_store", lambda: object())
    monkeypatch.setattr(hybrid_search, "_get_embedding_query_client", lambda: object())
    monkeypatch.setattr(hybrid_search, "_get_hybrid_filter_mode", lambda: "never")
    monkeypatch.setattr(hybrid_search, "_get_rerank_client", lambda: object())

    def _unexpected_sparse_encoder():
        raise AssertionError("sparse prewarm should be skipped when sparse retrieval is disabled")

    monkeypatch.setattr(hybrid_search, "_get_sparse_encoder", _unexpected_sparse_encoder)

    hybrid_search.warm_hybrid_runtime()
