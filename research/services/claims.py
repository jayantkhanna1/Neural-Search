"""Claim extraction with cross-source corroboration.

After research completes, key factual claims are extracted from the per-source
summaries and each claim is tagged with the sources that support it, giving a
trust signal ("corroborated by N independent sources") per finding.
"""

import logging

from . import anthropic_client

logger = logging.getLogger(__name__)

_MAX_SOURCES = 20
_MAX_CLAIMS = 8

_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "supporting_source_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                },
                "required": ["claim", "supporting_source_ids", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["claims"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You verify research findings. From the numbered source summaries, extract the "
    "most important factual claims. For each claim list every source id whose "
    "summary supports it, and rate confidence: 'high' when multiple independent "
    "sources agree, 'medium' for a single solid source, 'low' when sources are "
    "thin or conflicting. Only include claims actually present in the summaries."
)


def extract_claims(query: str, sources: list[dict]) -> list[dict]:
    """Return [{claim, confidence, source_urls}] from per-source summaries.

    ``sources`` items need: title, url, summary. Failures return [] (non-fatal).
    """
    usable = [s for s in sources if s.get("summary")][:_MAX_SOURCES]
    if len(usable) == 0:
        return []
    numbered = "\n\n".join(
        f"[{i}] {s['title'] or s['url']}\n{s['summary'][:700]}" for i, s in enumerate(usable)
    )
    try:
        result = anthropic_client.complete_json(
            system=_SYSTEM,
            user_content=(
                f"Research query: {query!r}\n\n"
                f"Source summaries:\n\n{numbered}\n\n"
                f"Extract at most {_MAX_CLAIMS} key claims."
            ),
            json_schema=_SCHEMA,
            max_tokens=1200,
        )
    except anthropic_client.LLMError as exc:
        logger.warning("Claim extraction failed: %s", exc)
        return []

    claims = []
    for item in result.get("claims", [])[:_MAX_CLAIMS]:
        try:
            source_urls = [
                usable[i]["url"]
                for i in item.get("supporting_source_ids", [])
                if isinstance(i, int) and 0 <= i < len(usable)
            ]
            claims.append(
                {
                    "claim": str(item["claim"]).strip(),
                    "confidence": item.get("confidence", "low"),
                    "source_urls": source_urls,
                }
            )
        except (KeyError, TypeError):
            continue
    return [c for c in claims if c["claim"]]
