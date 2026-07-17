"""Gap analysis: decide whether another research round is needed and, if so,
which follow-up queries would fill the missing coverage."""

import logging
from dataclasses import dataclass, field

from django.conf import settings

from . import anthropic_client

logger = logging.getLogger(__name__)

_SCHEMA = {
    "type": "object",
    "properties": {
        "sufficient": {"type": "boolean"},
        "missing": {"type": "string"},
        "followup_queries": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["sufficient", "missing", "followup_queries"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are a research lead reviewing what has been gathered so far on a topic. "
    "Judge whether the collected material sufficiently answers the research query "
    "with breadth (major aspects covered) and depth (specifics, numbers, expert "
    "views). If material is missing, name the gaps and produce targeted web search "
    "queries that would fill them — queries must not repeat ground already covered."
)


@dataclass
class GapReport:
    sufficient: bool = True
    missing: str = ""
    followup_queries: list[str] = field(default_factory=list)


def analyze(query: str, source_summaries: list[str], already_searched: list[str]) -> GapReport:
    """Return a GapReport for the research so far. Fails safe to 'sufficient'
    so a model error never loops the pipeline."""
    n = settings.RESEARCH["GAP_QUERIES"]
    summaries = "\n\n---\n\n".join(s[:800] for s in source_summaries[:20])
    try:
        result = anthropic_client.complete_json(
            system=_SYSTEM,
            user_content=(
                f"Research query: {query!r}\n\n"
                f"Queries already searched: {already_searched}\n\n"
                f"Summaries of gathered sources:\n\n{summaries}\n\n"
                f"If coverage is insufficient, give at most {n} follow-up queries."
            ),
            json_schema=_SCHEMA,
            max_tokens=500,
        )
        searched_lower = {q.lower() for q in already_searched}
        followups = [
            str(q).strip()
            for q in result.get("followup_queries", [])
            if str(q).strip() and str(q).strip().lower() not in searched_lower
        ][:n]
        return GapReport(
            sufficient=bool(result.get("sufficient", True)),
            missing=str(result.get("missing", "")),
            followup_queries=followups,
        )
    except anthropic_client.LLMError as exc:
        logger.warning("Gap analysis failed; stopping after this round: %s", exc)
        return GapReport(sufficient=True)
