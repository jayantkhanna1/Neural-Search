"""Contextual retrieval: generate a short situating line for each chunk.

One model call per source produces context lines for all of its chunks at
once. The full document is sent with a cache_control breakpoint so that, for
long documents, repeated calls in the same run hit the prompt cache instead
of re-paying for the document tokens.
"""

import logging

from django.conf import settings

from . import anthropic_client

logger = logging.getLogger(__name__)

_CHUNK_PREVIEW_CHARS = 240
_MAX_CHUNKS_PER_CALL = 40

_SCHEMA = {
    "type": "object",
    "properties": {
        "contexts": {
            "type": "array",
            "items": {"type": "string"},
        }
    },
    "required": ["contexts"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You situate document excerpts for a retrieval system. For each numbered "
    "excerpt, write ONE short sentence that states what the excerpt is about and "
    "how it fits in the overall document (who/what it concerns, which section or "
    "aspect). Output exactly one context sentence per excerpt, in order."
)


def contextualize_chunks(title: str, url: str, document: str, chunks: list[str]) -> list[str]:
    """Return one situating sentence per chunk (empty strings on failure)."""
    if not settings.RESEARCH["CONTEXTUAL_CHUNKS"] or not chunks:
        return [""] * len(chunks)

    chunks_to_do = chunks[:_MAX_CHUNKS_PER_CALL]
    previews = "\n".join(
        f"[{i}] {c[:_CHUNK_PREVIEW_CHARS].replace(chr(10), ' ')}"
        for i, c in enumerate(chunks_to_do)
    )
    # Document first with a cache breakpoint, varying request part after it.
    content = [
        {
            "type": "text",
            "text": f"<document title={title!r} url={url!r}>\n{document}\n</document>",
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": (
                f"Write one context sentence for each of these {len(chunks_to_do)} "
                f"excerpts from the document above:\n\n{previews}"
            ),
        },
    ]
    try:
        result = anthropic_client.complete_json(
            system=_SYSTEM,
            user_content=content,
            json_schema=_SCHEMA,
            max_tokens=80 * len(chunks_to_do),
        )
        contexts = [str(c).strip() for c in result.get("contexts", [])]
    except anthropic_client.LLMError as exc:
        logger.warning("Chunk contextualization failed for %s: %s", url, exc)
        contexts = []

    # Pad/truncate to match the chunk list exactly.
    contexts = contexts[: len(chunks_to_do)] + [""] * (len(chunks_to_do) - len(contexts))
    return contexts + [""] * (len(chunks) - len(chunks_to_do))
