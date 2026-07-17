"""Query expansion: turn one user query into several targeted search queries,
plus a topic category used to route specialized search providers."""

import hashlib
import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.core.cache import cache

from . import anthropic_client

logger = logging.getLogger(__name__)

CATEGORIES = ("general", "academic", "technical", "news")

_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {"type": "string"},
        },
        "category": {
            "type": "string",
            "enum": list(CATEGORIES),
        },
    },
    "required": ["queries", "category"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are a research planner. Given a user's research topic, produce diverse, "
    "specific web search queries that together give broad coverage of the topic: "
    "core definitions, recent developments, expert analysis, documentation or papers, "
    "and notable criticisms or open questions. Queries must stay faithful to the "
    "user's original intent. Also classify the topic: 'academic' for scientific/"
    "scholarly topics where research papers matter, 'technical' for software/"
    "engineering topics, 'news' for current events, otherwise 'general'."
)


@dataclass
class ExpandedQuery:
    queries: list[str] = field(default_factory=list)
    category: str = "general"


def expand_query(query: str) -> ExpandedQuery:
    """Return the original query plus model-generated related queries and a
    topic category.

    Results are cached; on any model failure we degrade gracefully to the
    original query so research can still proceed.
    """
    n = settings.RESEARCH["EXPANDED_QUERIES"]
    cache_key = "qexp2:" + hashlib.sha256(f"{n}:{query}".encode()).hexdigest()
    cached = cache.get(cache_key)
    if cached:
        return ExpandedQuery(**cached)

    queries = [query.strip()]
    category = "general"
    try:
        result = anthropic_client.complete_json(
            system=_SYSTEM,
            user_content=(
                f"Research topic: {query!r}\n"
                f"Generate exactly {n} distinct search queries and the category."
            ),
            json_schema=_SCHEMA,
            max_tokens=500,
        )
        for q in result.get("queries", []):
            q = str(q).strip()
            if q and q.lower() not in {x.lower() for x in queries}:
                queries.append(q)
        queries = queries[: n + 1]
        if result.get("category") in CATEGORIES:
            category = result["category"]
    except anthropic_client.LLMError as exc:
        logger.warning("Query expansion failed, using original query only: %s", exc)

    expanded = ExpandedQuery(queries=queries, category=category)
    cache.set(cache_key, expanded.__dict__, settings.RESEARCH["SEARCH_CACHE_TTL"])
    return expanded
