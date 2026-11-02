"""
Weekly topic clusters (spec §8.2): embed title + start of description with a
multilingual model (bge-m3) so English, Chinese and Vietnamese videos on the same
idea land together, then HDBSCAN (no k to guess, keeps noise). An LLM may only
NAME a cluster; it never decides membership.

Embeddings are computed per run and not stored: they follow the 30-day limit of
the text they come from (R3) without a table to clean.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class TopicVideo:
    video_id: str
    channel_id: str
    language: str | None
    published_at: datetime
    text: str
    s1: float | None = None
    baseline_approx: bool | None = None


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class BgeM3Embedder:
    """BAAI/bge-m3 through sentence-transformers, on this machine (no API cost).
    Needs `pip install -e ".[embeddings]"` (torch, ~2 GB) - spec question Q6."""

    def __init__(self, name: str = "BAAI/bge-m3"):
        from sentence_transformers import SentenceTransformer   # optional dependency
        self.name = name
        self._model = SentenceTransformer(name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._model.encode(texts, normalize_embeddings=True, batch_size=32).tolist()


def video_text(title: str | None, description: str | None, chars: int) -> str:
    return " ".join(p for p in ((title or "").strip(), (description or "").strip()[:chars]) if p)


def cluster_labels(videos: list[TopicVideo], embedder: Embedder, *,
                   min_cluster_size: int) -> dict[str, int]:
    """{video_id: cluster key}; -1 is HDBSCAN noise. Vectors are L2-normalised, so
    euclidean distance orders pairs like cosine distance."""
    import numpy as np
    from sklearn.cluster import HDBSCAN

    if len(videos) < min_cluster_size:
        return {v.video_id: -1 for v in videos}
    x = np.asarray(embedder.embed([v.text for v in videos]), dtype=float)
    x /= np.clip(np.linalg.norm(x, axis=1, keepdims=True), 1e-12, None)
    labels = HDBSCAN(min_cluster_size=min_cluster_size).fit_predict(x)
    return {v.video_id: int(k) for v, k in zip(videos, labels)}


def pair_agreement(labels: dict[str, int], pairs) -> dict:
    """R-03 acceptance: share of same-idea, different-language pairs (a, b) that end
    up in the same (non-noise) cluster. Pairs with a video missing from the run are
    reported, not counted."""
    together, counted, missing = 0, 0, []
    for a, b in pairs:
        if a not in labels or b not in labels:
            missing.append([a, b])
            continue
        counted += 1
        together += labels[a] == labels[b] != -1
    return {"pairs": counted, "same_cluster": together,
            "rate": round(together / counted, 3) if counted else None, "missing": missing}
