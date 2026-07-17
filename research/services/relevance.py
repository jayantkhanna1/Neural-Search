"""Relevance filtering and source-quality ranking.

Pages that survived fetching are checked against the user's original query
with the Haiku model (batched, excerpt-only for token efficiency), and each
kept source receives a combined quality score used for ranking.
"""

import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

from django.conf import settings

from . import anthropic_client
from .scraper import FetchedPage

logger = logging.getLogger(__name__)

_EXCERPT_CHARS = 1500

_SCHEMA = {
    "type": "object",
    "properties": {
        "judgments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "relevant": {"type": "boolean"},
                    "score": {"type": "number"},
                },
                "required": ["id", "relevant", "score"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["judgments"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are a strict research curator. For each numbered document excerpt, decide "
    "whether it contains substantive, trustworthy information that is relevant to the "
    "research query. Mark spam, ads, link farms, paywalled stubs, error pages, and "
    "off-topic content as not relevant. Score relevance from 0.0 (unrelated) to 1.0 "
    "(highly relevant, high-quality)."
)

_HIGH_QUALITY_DOMAIN_HINTS = (
    ".edu", ".gov", ".org", "arxiv.org", "wikipedia.org", "nature.com",
    "acm.org", "ieee.org", "docs.", "documentation",
)


def score_relevance(query: str, pages: list[FetchedPage]) -> dict[str, float]:
    """Return {url: relevance_score} for every page, judged by the model.

    On model failure the batch degrades to a neutral pass-through score so a
    transient LLM outage doesn't discard already-fetched research.
    """
    scores: dict[str, float] = {}
    batch_size = settings.RESEARCH["RELEVANCE_BATCH_SIZE"]

    for start in range(0, len(pages), batch_size):
        batch = pages[start : start + batch_size]
        docs = []
        for i, page in enumerate(batch):
            excerpt = page.text[:_EXCERPT_CHARS].replace("\n", " ")
            docs.append(f"[{i}] TITLE: {page.title[:150]}\nURL: {page.url}\nEXCERPT: {excerpt}")
        prompt = (
            f"Research query: {query!r}\n\n"
            "Documents:\n\n" + "\n\n---\n\n".join(docs)
        )
        try:
            result = anthropic_client.complete_json(
                system=_SYSTEM,
                user_content=prompt,
                json_schema=_SCHEMA,
                max_tokens=800,
            )
            judged = {j["id"]: j for j in result.get("judgments", []) if isinstance(j, dict)}
            for i, page in enumerate(batch):
                j = judged.get(i)
                if j is None:
                    scores[page.url] = 0.0
                else:
                    score = max(0.0, min(1.0, float(j.get("score", 0.0))))
                    scores[page.url] = score if j.get("relevant") else min(score, 0.2)
        except (anthropic_client.LLMError, TypeError, ValueError) as exc:
            logger.warning("Relevance batch failed (%s); keeping batch with neutral score", exc)
            threshold = settings.RESEARCH["RELEVANCE_THRESHOLD"]
            for page in batch:
                scores[page.url] = threshold
    return scores


def quality_score(
    url: str,
    relevance: float,
    content_length: int,
    published_at: datetime | None = None,
) -> float:
    """Combine model relevance with cheap source-quality heuristics for ranking."""
    domain = urlparse(url).netloc.lower()
    bonus = 0.0
    if url.startswith("https://"):
        bonus += 0.02
    if any(hint in domain or hint in url.lower() for hint in _HIGH_QUALITY_DOMAIN_HINTS):
        bonus += 0.08
    # Freshness: recent publications get a small boost that decays over ~2
    # years; undated content is neither rewarded nor penalized.
    if published_at is not None:
        age_days = max(0.0, (datetime.now(timezone.utc) - published_at).days)
        bonus += 0.05 * max(0.0, 1.0 - age_days / 730.0)
    # Reward substantive articles without letting sheer length dominate.
    length_factor = min(content_length / 8000.0, 1.0) * 0.1
    return round(min(1.0, relevance * 0.8 + bonus + length_factor), 4)
