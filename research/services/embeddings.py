"""Optional semantic embeddings via the Voyage AI REST API.

Enabled when VOYAGE_API_KEY is set; every function degrades to None/no-op
without it so the rest of the system runs on pure BM25.
"""

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_API_URL = "https://api.voyageai.com/v1/embeddings"
_BATCH_SIZE = 96
_TIMEOUT = 60


def enabled() -> bool:
    return bool(settings.RESEARCH["VOYAGE_API_KEY"])


def _embed(texts: list[str], input_type: str) -> list[list[float]] | None:
    if not enabled() or not texts:
        return None
    vectors: list[list[float]] = []
    for start in range(0, len(texts), _BATCH_SIZE):
        batch = [t[:8000] for t in texts[start : start + _BATCH_SIZE]]
        try:
            response = requests.post(
                _API_URL,
                json={
                    "model": settings.RESEARCH["VOYAGE_MODEL"],
                    "input": batch,
                    "input_type": input_type,
                },
                headers={"Authorization": f"Bearer {settings.RESEARCH['VOYAGE_API_KEY']}"},
                timeout=_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json().get("data", [])
            if len(data) != len(batch):
                logger.warning("Voyage returned %d vectors for %d inputs", len(data), len(batch))
                return None
            vectors.extend(item["embedding"] for item in data)
        except (requests.RequestException, KeyError, ValueError) as exc:
            logger.warning("Embedding request failed (falling back to BM25 only): %s", exc)
            return None
    return vectors


def embed_documents(texts: list[str]) -> list[list[float]] | None:
    return _embed(texts, "document")


def embed_query(text: str) -> list[float] | None:
    vectors = _embed([text], "query")
    return vectors[0] if vectors else None


def cosine(a: list[float], b: list[float]) -> float:
    dot = norm_a = norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a**0.5 * norm_b**0.5)
