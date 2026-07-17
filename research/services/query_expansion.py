"""Query expansion: turn one user query into several targeted search queries."""

import hashlib
import logging

from django.conf import settings
from django.core.cache import cache

from . import anthropic_client

logger = logging.getLogger(__name__)

_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {"type": "string"},
        }
    },
    "required": ["queries"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are a research planner. Given a user's research topic, produce diverse, "
    "specific web search queries that together give broad coverage of the topic: "
    "core definitions, recent developments, expert analysis, documentation or papers, "
    "and notable criticisms or open questions. Queries must stay faithful to the "
    "user's original intent. Return only the queries."
)


def expand_query(query: str) -> list[str]:
    """Return the original query plus model-generated related queries.

    Results are cached; on any model failure we degrade gracefully to the
    original query so research can still proceed.
    """
    n = settings.RESEARCH["EXPANDED_QUERIES"]
    cache_key = "qexp:" + hashlib.sha256(f"{n}:{query}".encode()).hexdigest()
    cached = cache.get(cache_key)
    if cached:
        return cached

    queries = [query.strip()]
    try:
        result = anthropic_client.complete_json(
            system=_SYSTEM,
            user_content=(
                f"Research topic: {query!r}\n"
                f"Generate exactly {n} distinct search queries."
            ),
            json_schema=_SCHEMA,
            max_tokens=500,
        )
        for q in result.get("queries", []):
            q = q.strip()
            if q and q.lower() not in {x.lower() for x in queries}:
                queries.append(q)
        queries = queries[: n + 1]
    except anthropic_client.LLMError as exc:
        logger.warning("Query expansion failed, using original query only: %s", exc)

    cache.set(cache_key, queries, settings.RESEARCH["SEARCH_CACHE_TTL"])
    return queries
