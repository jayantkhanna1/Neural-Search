"""Per-source and consolidated summarization using the Haiku model.

Per-source summaries can optionally run through the Message Batches API
(50% cost) when RESEARCH_USE_BATCH_SUMMARIES is enabled — appropriate for
this pipeline, which favors thoroughness over speed.
"""

import logging

from django.conf import settings

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


def _source_prompt(query: str, title: str, url: str, content: str) -> str:
    return (
        f"Research query: {query!r}\n"
        f"Source: {title} ({url})\n\n"
        f"Document:\n{content[:_SOURCE_EXCERPT_CHARS]}"
    )


def summarize_source(query: str, title: str, url: str, content: str) -> str:
    """Summarize one source. Returns an empty string on failure (non-fatal)."""
    try:
        return anthropic_client.complete(
            system=_SOURCE_SYSTEM,
            user_content=_source_prompt(query, title, url, content),
            max_tokens=400,
        ).strip()
    except anthropic_client.LLMError as exc:
        logger.warning("Source summary failed for %s: %s", url, exc)
        return ""


def summarize_sources(query: str, sources, progress_callback=None) -> None:
    """Fill in ``source.summary`` for every source in the list, in place.

    Uses the Batches API (50% cost) when enabled and there is enough work to
    justify the batch turnaround; individual failures fall back to sync calls.
    ``sources`` are model instances with title/url/content/summary attributes;
    callers persist them.
    """
    pending = [s for s in sources if not s.summary]
    if not pending:
        return

    if settings.RESEARCH["USE_BATCH_SUMMARIES"] and len(pending) >= 4:
        requests_params = [
            (
                f"src-{s.id}",
                {
                    "model": settings.ANTHROPIC_MODEL,
                    "max_tokens": 400,
                    "system": _SOURCE_SYSTEM,
                    "messages": [
                        {
                            "role": "user",
                            "content": _source_prompt(query, s.title, s.url, s.content),
                        }
                    ],
                },
            )
            for s in pending
        ]
        try:
            results = anthropic_client.run_batch(requests_params)
        except anthropic_client.LLMError as exc:
            logger.warning("Batch summarization failed, falling back to sync: %s", exc)
            results = {}
        for s in pending:
            s.summary = results.get(f"src-{s.id}", "").strip()

    still_pending = [s for s in pending if not s.summary]
    for i, s in enumerate(still_pending):
        if progress_callback:
            progress_callback(i, len(still_pending))
        s.summary = summarize_source(query, s.title, s.url, s.content)


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
