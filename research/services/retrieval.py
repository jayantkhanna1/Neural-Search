"""Lexical BM25 retrieval over a session's document chunks.

Session knowledge bases are bounded (dozens of sources, hundreds of chunks),
so an in-process BM25 over the session's chunks is fast, dependency-free and
avoids running a separate vector store.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass

from ..models import DocumentChunk

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in into is it its of on or that the "
    "this to was were what when where which who will with how why do does did not "
    "you your we our they them their he she his her".split()
)

_K1 = 1.5
_B = 0.75


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS and len(t) > 1]


@dataclass
class RetrievedChunk:
    chunk: DocumentChunk
    score: float


def retrieve(session_id: int, query: str, top_k: int) -> list[RetrievedChunk]:
    """Rank the session's chunks against the query with BM25."""
    query_terms = tokenize(query)
    if not query_terms:
        return []

    chunks = list(
        DocumentChunk.objects.filter(session_id=session_id).select_related("source")
    )
    if not chunks:
        return []

    docs = [tokenize(c.text) for c in chunks]
    doc_lens = [len(d) for d in docs]
    avg_len = (sum(doc_lens) / len(doc_lens)) or 1.0
    n_docs = len(docs)

    # Document frequency per query term
    df = Counter()
    doc_counters = [Counter(d) for d in docs]
    for term in set(query_terms):
        df[term] = sum(1 for counter in doc_counters if term in counter)

    scored = []
    for chunk, counter, doc_len in zip(chunks, doc_counters, doc_lens):
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
            scored.append(RetrievedChunk(chunk=chunk, score=score))

    scored.sort(key=lambda r: r.score, reverse=True)
    return scored[:top_k]
