from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from transformers import AutoModelForSequenceClassification, AutoTokenizer

MODEL_ID = os.getenv("RERANK_MODEL_ID", "BAAI/bge-reranker-v2-m3").strip() or "BAAI/bge-reranker-v2-m3"
MAX_LENGTH = max(32, int(os.getenv("RERANK_MAX_LENGTH", "512") or 512))
DEVICE_HINT = (os.getenv("RERANK_DEVICE", "auto").strip().lower() or "auto")


def _resolve_device() -> str:
    if DEVICE_HINT == "cpu":
        return "cpu"
    if DEVICE_HINT == "cuda" and torch.cuda.is_available():
        return "cuda"
    return "cuda" if torch.cuda.is_available() else "cpu"


DEVICE = _resolve_device()


class RerankRequest(BaseModel):
    model: str | None = None
    query: str
    documents: list[str] = Field(default_factory=list)
    top_n: int = 5


def _load_model() -> tuple[Any, Any]:
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, trust_remote_code=True)
    model = AutoModelForSequenceClassification.from_pretrained(MODEL_ID, trust_remote_code=True)
    model.eval()
    model.to(DEVICE)
    return tokenizer, model


@lru_cache(maxsize=1)
def _get_model_bundle() -> tuple[Any, Any]:
    return _load_model()


def _score_pairs(query: str, documents: list[str]) -> list[float]:
    tokenizer, model = _get_model_bundle()
    pairs = [[query, doc] for doc in documents]
    inputs = tokenizer(
        pairs,
        padding=True,
        truncation=True,
        max_length=MAX_LENGTH,
        return_tensors="pt",
    )
    inputs = {key: value.to(DEVICE) for key, value in inputs.items()}
    with torch.no_grad():
        logits = model(**inputs, return_dict=True).logits.view(-1)
    return logits.float().cpu().tolist()


app = FastAPI(title="story2memory-local-rerank")


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    try:
        _get_model_bundle()
    except Exception as exc:  # pragma: no cover - runtime path
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "status": "ok",
        "model": MODEL_ID,
        "device": DEVICE,
    }


@app.post("/rerank")
def rerank(request: RerankRequest) -> dict[str, Any]:
    query = str(request.query or "").strip()
    documents = [str(item or "") for item in request.documents if str(item or "").strip()]
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    if not documents:
        return {"model": MODEL_ID, "results": []}

    try:
        scores = _score_pairs(query, documents)
    except Exception as exc:  # pragma: no cover - runtime path
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    ranked = [
        {"index": index, "score": float(score)}
        for index, score in enumerate(scores)
    ]
    ranked.sort(key=lambda item: item["score"], reverse=True)
    top_n = max(1, min(int(request.top_n or 1), len(ranked)))
    return {
        "model": request.model or MODEL_ID,
        "results": ranked[:top_n],
    }
