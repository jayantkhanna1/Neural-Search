"""HTTP API for the research assistant frontend."""

import json
import logging

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_http_methods

from .models import ChatMessage, ResearchSession, ResearchTask
from .services import chat as chat_service
from .services.anthropic_client import LLMError
from .tasks import run_research

logger = logging.getLogger(__name__)

MAX_QUERY_LENGTH = 1000
MAX_MESSAGE_LENGTH = 4000


def _json_body(request) -> dict:
    try:
        return json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return {}


def _error(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def _task_payload(task: ResearchTask) -> dict:
    return {
        "id": task.id,
        "query": task.query,
        "status": task.status,
        "status_label": task.get_status_display(),
        "stage_detail": task.stage_detail,
        "progress": task.progress,
        "expanded_queries": task.expanded_queries,
        "urls_found": task.urls_found,
        "sources_fetched": task.sources_fetched,
        "sources_kept": task.sources_kept,
        "error": task.error,
        "is_active": task.is_active,
        "created_at": task.created_at.isoformat(),
    }


def _session_payload(session: ResearchSession, include_detail: bool = True) -> dict:
    payload = {
        "id": session.id,
        "title": session.title,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
    }
    if include_detail:
        payload.update(
            {
                "summary": session.summary,
                "tasks": [_task_payload(t) for t in session.tasks.all()],
                "sources": [
                    {
                        "id": s.id,
                        "title": s.title or s.url,
                        "url": s.url,
                        "domain": s.domain,
                        "summary": s.summary,
                        "relevance_score": s.relevance_score,
                        "quality_score": s.quality_score,
                    }
                    for s in session.sources.order_by("-quality_score")
                ],
                "messages": [
                    {"role": m.role, "content": m.content, "created_at": m.created_at.isoformat()}
                    for m in session.messages.all()
                ],
                "is_researching": any(t.is_active for t in session.tasks.all()),
            }
        )
    return payload


def _start_research(session: ResearchSession, query: str) -> ResearchTask:
    task = ResearchTask.objects.create(session=session, query=query)
    run_research.delay(task.id)
    return task


def index(request):
    return render(request, "index.html")


@require_GET
def list_sessions(request):
    sessions = ResearchSession.objects.all()[:50]
    return JsonResponse({"sessions": [_session_payload(s, include_detail=False) for s in sessions]})


@require_http_methods(["POST"])
def create_session(request):
    query = str(_json_body(request).get("query", "")).strip()
    if not query:
        return _error("A research query is required.")
    if len(query) > MAX_QUERY_LENGTH:
        return _error(f"Query is too long (max {MAX_QUERY_LENGTH} characters).")

    session = ResearchSession.objects.create(title=query[:512])
    task = _start_research(session, query)
    logger.info("Created session %s with research task %s", session.id, task.id)
    return JsonResponse({"session": _session_payload(session)}, status=201)


@require_GET
def session_detail(request, session_id: int):
    session = get_object_or_404(ResearchSession, pk=session_id)
    return JsonResponse({"session": _session_payload(session)})


@require_http_methods(["POST"])
def add_research(request, session_id: int):
    session = get_object_or_404(ResearchSession, pk=session_id)
    query = str(_json_body(request).get("query", "")).strip()
    if not query:
        return _error("A research query is required.")
    if len(query) > MAX_QUERY_LENGTH:
        return _error(f"Query is too long (max {MAX_QUERY_LENGTH} characters).")

    task = _start_research(session, query)
    return JsonResponse({"task": _task_payload(task)}, status=201)


@require_http_methods(["POST"])
def send_message(request, session_id: int):
    session = get_object_or_404(ResearchSession, pk=session_id)
    message = str(_json_body(request).get("message", "")).strip()
    if not message:
        return _error("A message is required.")
    if len(message) > MAX_MESSAGE_LENGTH:
        return _error(f"Message is too long (max {MAX_MESSAGE_LENGTH} characters).")

    try:
        outcome = chat_service.respond(session, message)
    except LLMError as exc:
        logger.error("Chat failed for session %s: %s", session_id, exc)
        return _error(f"The assistant is unavailable right now: {exc}", status=503)

    task_payload = None
    if outcome.research_query:
        task = _start_research(session, outcome.research_query)
        task_payload = _task_payload(task)

    return JsonResponse(
        {
            "reply": outcome.reply,
            "context_sources": outcome.context_sources,
            "research_task": task_payload,
        }
    )
