"""Chat-with-research (RAG) flow.

Each user turn retrieves the most relevant chunks from the session's knowledge
base and answers with the Haiku model. The model is also given a tool that lets
it launch additional background research into the same session when the user
asks for information the knowledge base doesn't cover.
"""

import logging
from dataclasses import dataclass, field

from django.conf import settings

from ..models import ChatMessage, ResearchSession
from . import anthropic_client, retrieval

logger = logging.getLogger(__name__)

_RESEARCH_TOOL = {
    "name": "start_background_research",
    "description": (
        "Launch a new background web-research task that will expand this session's "
        "knowledge base. Use it when the user explicitly asks to research, search "
        "for, or look up additional information, or when their question clearly "
        "cannot be answered from the provided context. Research takes several "
        "minutes, so also tell the user it has started."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "A focused web-search research topic.",
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

_SYSTEM = (
    "You are a research assistant chatting about a knowledge base of web sources "
    "gathered for the user. Answer using the provided context excerpts; when you "
    "use one, mention its source (title or domain). If the context does not cover "
    "the question, say so plainly — do not invent facts — and, if the user wants "
    "new information gathered, use the start_background_research tool. Keep "
    "answers focused and readable."
)


@dataclass
class ChatOutcome:
    reply: str
    research_query: str | None = None
    context_sources: list[dict] = field(default_factory=list)


def _build_context(session: ResearchSession, user_message: str) -> tuple[str, list[dict]]:
    hits = retrieval.retrieve(session.id, user_message, settings.RESEARCH["RAG_TOP_K"])
    if not hits:
        return "(The knowledge base has no relevant excerpts for this question yet.)", []
    blocks, sources = [], []
    seen_urls = set()
    for hit in hits:
        src = hit.chunk.source
        blocks.append(
            f"<excerpt source_title={src.title[:120]!r} source_url={src.url!r}>\n"
            f"{hit.chunk.text}\n</excerpt>"
        )
        if src.url not in seen_urls:
            seen_urls.add(src.url)
            sources.append({"title": src.title, "url": src.url})
    return "\n\n".join(blocks), sources


def _history(session: ResearchSession) -> list[dict]:
    turns = settings.RESEARCH["CHAT_HISTORY_TURNS"]
    messages = list(session.messages.order_by("-created_at")[:turns])[::-1]
    return [{"role": m.role, "content": m.content} for m in messages]


def respond(session: ResearchSession, user_message: str) -> ChatOutcome:
    """Answer one chat turn. Persists both the user and assistant messages."""
    ChatMessage.objects.create(session=session, role=ChatMessage.Role.USER, content=user_message)

    context, context_sources = _build_context(session, user_message)
    messages = _history(session)[:-1]  # history without the turn we just saved
    messages.append(
        {
            "role": "user",
            "content": (
                f"<knowledge_base_context>\n{context}\n</knowledge_base_context>\n\n"
                f"{user_message}"
            ),
        }
    )

    response = anthropic_client.chat(system=_SYSTEM, messages=messages, tools=[_RESEARCH_TOOL])

    research_query = None
    if response.stop_reason == "tool_use":
        tool_use = next(b for b in response.content if b.type == "tool_use")
        research_query = str(tool_use.input.get("query") or user_message).strip()
        # Let the model phrase its acknowledgement with the tool result in hand.
        messages.append({"role": "assistant", "content": response.content})
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": (
                            f"Background research on {research_query!r} has been started. "
                            "It will take a few minutes; new sources and an updated summary "
                            "will appear in this session automatically."
                        ),
                    }
                ],
            }
        )
        response = anthropic_client.chat(system=_SYSTEM, messages=messages, tools=[_RESEARCH_TOOL])

    reply = "".join(b.text for b in response.content if b.type == "text").strip()
    if not reply:
        reply = (
            f"I've started background research on {research_query!r}."
            if research_query
            else "I couldn't produce an answer for that — please try rephrasing."
        )

    ChatMessage.objects.create(session=session, role=ChatMessage.Role.ASSISTANT, content=reply)
    return ChatOutcome(reply=reply, research_query=research_query, context_sources=context_sources)
