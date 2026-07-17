"""Chat-with-research (RAG) flow.

Each user turn retrieves the most relevant chunks from the session's knowledge
base and answers with the Haiku model. Retrieved chunks are passed as document
content blocks with citations enabled, so answers carry real per-passage
citations. The model also has a tool that launches additional background
research into the same session when the user asks for new information.
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
        "cannot be answered from the provided documents. Research takes several "
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
    "gathered for the user. Answer using the provided documents; citations are "
    "attached automatically, so do not write out URLs yourself. If the documents "
    "do not cover the question, say so plainly — do not invent facts — and, if "
    "the user wants new information gathered, use the start_background_research "
    "tool. Keep answers focused and readable."
)


@dataclass
class ChatOutcome:
    reply: str
    research_query: str | None = None
    citations: list[dict] = field(default_factory=list)
    context_sources: list[dict] = field(default_factory=list)


def _build_documents(session: ResearchSession, user_message: str) -> tuple[list[dict], list[dict]]:
    """Return (document content blocks, per-document source metadata)."""
    hits = retrieval.retrieve(session.id, user_message, settings.RESEARCH["RAG_TOP_K"])
    documents, doc_sources = [], []
    for hit in hits:
        src = hit.chunk.source
        text = retrieval.index_text(hit.chunk)
        documents.append(
            {
                "type": "document",
                "source": {"type": "text", "media_type": "text/plain", "data": text},
                "title": (src.title or src.domain or src.url)[:255],
                "citations": {"enabled": True},
            }
        )
        doc_sources.append({"title": src.title or src.domain, "url": src.url})
    return documents, doc_sources


def _history(session: ResearchSession) -> list[dict]:
    turns = settings.RESEARCH["CHAT_HISTORY_TURNS"]
    messages = list(session.messages.order_by("-created_at")[:turns])[::-1]
    return [{"role": m.role, "content": m.content} for m in messages]


def _assemble_reply(response, doc_sources: list[dict]) -> tuple[str, list[dict]]:
    """Join text blocks, inserting [n] citation markers; return (text, citations)."""
    used: dict[int, int] = {}  # document_index -> citation number
    citations: list[dict] = []
    parts: list[str] = []

    for block in response.content:
        if block.type != "text":
            continue
        parts.append(block.text)
        markers = []
        for citation in getattr(block, "citations", None) or []:
            doc_index = getattr(citation, "document_index", None)
            if doc_index is None or doc_index >= len(doc_sources):
                continue
            if doc_index not in used:
                used[doc_index] = len(used) + 1
                citations.append({"n": used[doc_index], **doc_sources[doc_index]})
            marker = f"[{used[doc_index]}]"
            if marker not in markers:
                markers.append(marker)
        if markers:
            parts.append(" " + "".join(markers))

    return "".join(parts).strip(), citations


def respond(session: ResearchSession, user_message: str) -> ChatOutcome:
    """Answer one chat turn. Persists both the user and assistant messages."""
    ChatMessage.objects.create(session=session, role=ChatMessage.Role.USER, content=user_message)

    documents, doc_sources = _build_documents(session, user_message)
    messages = _history(session)[:-1]  # history without the turn we just saved

    content: list[dict] = list(documents)
    if not documents:
        content.append(
            {
                "type": "text",
                "text": "(The knowledge base has no relevant documents for this question yet.)",
            }
        )
    content.append({"type": "text", "text": user_message})
    messages.append({"role": "user", "content": content})

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

    reply, citations = _assemble_reply(response, doc_sources)
    if not reply:
        reply = (
            f"I've started background research on {research_query!r}."
            if research_query
            else "I couldn't produce an answer for that — please try rephrasing."
        )

    ChatMessage.objects.create(session=session, role=ChatMessage.Role.ASSISTANT, content=reply)
    return ChatOutcome(
        reply=reply,
        research_query=research_query,
        citations=citations,
        context_sources=doc_sources,
    )
