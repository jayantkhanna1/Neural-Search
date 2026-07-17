"""Hybrid retrieval over a session's document chunks.

BM25 (lexical) is always available; when Voyage embeddings are enabled the
semantic ranking is fused with BM25 via Reciprocal Rank Fusion. Chunks are
indexed as context + text (contextual retrieval), so a chunk's model-written
situating sentence participates in matching.

Session knowledge bases are bounded (dozens of sources, hundreds of chunks),
so in-process scoring is fast and avoids running a separate vector store.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass

from ..models import DocumentChunk
from . import embeddings

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in into is it its of on or that the "
    "this to was were what when where which who will with how why do does did not "
    "you your we our they them their he she his her".split()
)

_K1 = 1.5
_B = 0.75
_RRF_K = 60


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS and len(t) > 1]


def index_text(chunk: DocumentChunk) -> str:
    return f"{chunk.context}\n{chunk.text}" if chunk.context else chunk.text


@dataclass
class RetrievedChunk:
    chunk: DocumentChunk
    score: float


def _bm25_ranking(chunks: list[DocumentChunk], query: str) -> list[tuple[int, float]]:
    """Return [(chunk_position, score)] sorted best-first."""
    query_terms = tokenize(query)
    if not query_terms:
        return []
    docs = [tokenize(index_text(c)) for c in chunks]
    doc_lens = [len(d) for d in docs]
    avg_len = (sum(doc_lens) / len(doc_lens)) or 1.0
    n_docs = len(docs)

    doc_counters = [Counter(d) for d in docs]
    df = Counter()
    for term in set(query_terms):
        df[term] = sum(1 for counter in doc_counters if term in counter)

    scored = []
    for pos, (counter, doc_len) in enumerate(zip(doc_counters, doc_lens)):
        score = 0.0
        for term in query_terms:
            tf = counter.get(term, 0)
            if not tf:
                continue
            idf = math.log(1 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * (tf * (_K1 + 1)) / (
                tf + _K1 * (1 - _B + _B * doc_len / avg_len)
            )
        if score > 0:
            scored.append((pos, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


def _semantic_ranking(chunks: list[DocumentChunk], query: str) -> list[tuple[int, float]]:
    """Return [(chunk_position, cosine)] best-first for chunks with embeddings."""
    query_vector = embeddings.embed_query(query)
    if query_vector is None:
        return []
    scored = [
        (pos, embeddings.cosine(query_vector, chunk.embedding))
        for pos, chunk in enumerate(chunks)
        if chunk.embedding
    ]
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


def retrieve(session_id: int, query: str, top_k: int) -> list[RetrievedChunk]:
    """Rank the session's chunks against the query.

    Uses Reciprocal Rank Fusion of BM25 and semantic rankings when both are
    available, otherwise pure BM25.
    """
    chunks = list(
        DocumentChunk.objects.filter(session_id=session_id).select_related("source")
    )
    if not chunks:
        return []

    bm25 = _bm25_ranking(chunks, query)
    semantic = _semantic_ranking(chunks, query) if embeddings.enabled() else []

    if not semantic:
        return [RetrievedChunk(chunk=chunks[pos], score=score) for pos, score in bm25[:top_k]]

    # Reciprocal Rank Fusion across the two rankings.
    fused: dict[int, float] = {}
    for ranking in (bm25, semantic):
        for rank, (pos, _score) in enumerate(ranking):
            fused[pos] = fused.get(pos, 0.0) + 1.0 / (_RRF_K + rank + 1)

    best = sorted(fused.items(), key=lambda item: item[1], reverse=True)[:top_k]
    return [RetrievedChunk(chunk=chunks[pos], score=score) for pos, score in best]
