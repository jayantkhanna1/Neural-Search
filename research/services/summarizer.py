"""Per-source and consolidated summarization using the Haiku model."""

import logging

from . import anthropic_client

logger = logging.getLogger(__name__)

_SOURCE_EXCERPT_CHARS = 7000
_MAX_SOURCES_IN_FINAL = 20

_SOURCE_SYSTEM = (
    "You summarize web sources for a research assistant. Produce a tight factual "
    "summary (3-6 bullet points) of the key information in the document that is "
    "relevant to the research query. No preamble, no meta-commentary."
)

_FINAL_SYSTEM = (
    "You are a research analyst. Consolidate the per-source summaries into one "
    "readable research brief in Markdown with these sections: a 2-3 sentence "
    "overview, '## Key findings' as bullet points, '## Notable insights' for "
    "surprising or important points, and '## Key references' listing the most "
    "useful sources as `[title](url)` items. Be factual and cite only the "
    "provided sources."
)


def summarize_source(query: str, title: str, url: str, content: str) -> str:
    """Summarize one source. Returns an empty string on failure (non-fatal)."""
    try:
        return anthropic_client.complete(
            system=_SOURCE_SYSTEM,
            user_content=(
                f"Research query: {query!r}\n"
                f"Source: {title} ({url})\n\n"
                f"Document:\n{content[:_SOURCE_EXCERPT_CHARS]}"
            ),
            max_tokens=400,
        ).strip()
    except anthropic_client.LLMError as exc:
        logger.warning("Source summary failed for %s: %s", url, exc)
        return ""


def consolidate(query: str, sources: list[dict]) -> str:
    """Build the final research brief from per-source summaries.

    ``sources`` items need: title, url, summary. They should be pre-sorted by
    quality; only the top slice is sent to keep token usage bounded.
    """
    top = [s for s in sources if s.get("summary")][:_MAX_SOURCES_IN_FINAL]
    if not top:
        return ""
    parts = [
        f"SOURCE: {s['title'] or s['url']}\nURL: {s['url']}\nSUMMARY:\n{s['summary']}"
        for s in top
    ]
    try:
        return anthropic_client.complete(
            system=_FINAL_SYSTEM,
            user_content=(
                f"Research query: {query!r}\n\n"
                f"Per-source summaries ({len(top)} sources):\n\n"
                + "\n\n=====\n\n".join(parts)
            ),
            max_tokens=1800,
        ).strip()
    except anthropic_client.LLMError as exc:
        logger.error("Consolidated summary failed: %s", exc)
        return ""
