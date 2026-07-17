"""Celery tasks: the background research pipeline.

Pipeline stages (each persisted to ResearchTask for live progress reporting):
  expanding -> searching -> fetching -> filtering -> summarizing -> completed

The pipeline favors thoroughness over speed: it walks every expanded query,
fetches sources politely (robots.txt + per-domain throttling), filters them
for relevance with the Haiku model, and only then summarizes and indexes.
"""

import hashlib
import logging
from urllib.parse import urlparse

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import DocumentChunk, ResearchSession, ResearchTask, Source
from .services import chunking, query_expansion, relevance, scraper, search, summarizer

logger = logging.getLogger(__name__)


def _content_hash(text: str) -> str:
    # Hash a normalized prefix so near-identical mirrors dedupe cheaply.
    normalized = " ".join(text.lower().split())[:5000]
    return hashlib.sha256(normalized.encode()).hexdigest()


def _collect_urls(task: ResearchTask, queries: list[str]) -> list[search.SearchResult]:
    """Run every query through web search, deduplicating URLs across queries."""
    seen: set[str] = set(task.session.sources.values_list("url", flat=True))
    collected: list[search.SearchResult] = []
    max_urls = settings.RESEARCH["MAX_URLS_PER_TASK"]

    for i, query in enumerate(queries):
        task.set_stage(
            ResearchTask.Status.SEARCHING,
            f"Searching ({i + 1}/{len(queries)}): {query}",
            progress=10 + int(15 * (i + 1) / len(queries)),
        )
        for result in search.search_web(query):
            url = result.url.split("#", 1)[0]
            if url in seen:
                continue
            seen.add(url)
            collected.append(result)
            if len(collected) >= max_urls:
                return collected
    return collected


def _fetch_pages(task: ResearchTask, results: list[search.SearchResult]) -> list[scraper.FetchedPage]:
    pages: list[scraper.FetchedPage] = []
    seen_hashes: set[str] = set(task.session.sources.values_list("content_hash", flat=True))

    for i, result in enumerate(results):
        task.set_stage(
            ResearchTask.Status.FETCHING,
            f"Reading source {i + 1}/{len(results)}: {urlparse(result.url).netloc}",
            progress=25 + int(35 * (i + 1) / len(results)),
        )
        page = scraper.fetch_page(result.url)
        if page is None:
            continue
        digest = _content_hash(page.text)
        if digest in seen_hashes:  # duplicate/mirrored content
            continue
        seen_hashes.add(digest)
        if not page.title and result.title:
            page.title = result.title
        pages.append(page)
        task.sources_fetched = len(pages)
        task.save(update_fields=["sources_fetched"])
    return pages


def _store_source(task: ResearchTask, page: scraper.FetchedPage, rel: float) -> Source:
    source = Source.objects.create(
        session=task.session,
        task=task,
        url=page.url[:2000],
        domain=urlparse(page.url).netloc[:255],
        title=page.title[:1024],
        content=page.text,
        content_hash=_content_hash(page.text),
        relevance_score=rel,
        quality_score=relevance.quality_score(page.url, rel, len(page.text)),
    )
    DocumentChunk.objects.bulk_create(
        [
            DocumentChunk(session=task.session, source=source, index=i, text=text)
            for i, text in enumerate(chunking.chunk_text(page.text))
        ]
    )
    return source


@shared_task(
    bind=True,
    max_retries=2,
    retry_backoff=30,
    retry_backoff_max=300,
    retry_jitter=True,
)
def run_research(self, task_id: int):
    """Execute one full research run for a ResearchTask."""
    try:
        task = ResearchTask.objects.select_related("session").get(pk=task_id)
    except ResearchTask.DoesNotExist:
        logger.error("ResearchTask %s no longer exists; dropping job", task_id)
        return

    if task.status == ResearchTask.Status.COMPLETED:
        return  # idempotency guard against duplicate delivery

    task.celery_task_id = self.request.id or ""
    task.started_at = timezone.now()
    task.error = ""
    task.save(update_fields=["celery_task_id", "started_at", "error"])

    try:
        # 1. Query expansion -----------------------------------------------
        task.set_stage(ResearchTask.Status.EXPANDING, "Generating related search queries", 5)
        queries = query_expansion.expand_query(task.query)
        task.expanded_queries = queries
        task.save(update_fields=["expanded_queries"])

        # 2. Web search ----------------------------------------------------
        results = _collect_urls(task, queries)
        task.urls_found = len(results)
        task.save(update_fields=["urls_found"])
        if not results:
            raise RuntimeError(
                "Web search returned no new results. Try rewording the query or retry later."
            )

        # 3. Fetch + extract -------------------------------------------------
        pages = _fetch_pages(task, results)
        if not pages:
            raise RuntimeError(
                "None of the search results could be fetched and read. Try again later."
            )

        # 4. Relevance filtering --------------------------------------------
        task.set_stage(
            ResearchTask.Status.FILTERING,
            f"Checking {len(pages)} sources for relevance",
            65,
        )
        scores = relevance.score_relevance(task.query, pages)
        threshold = settings.RESEARCH["RELEVANCE_THRESHOLD"]
        kept = [(p, scores.get(p.url, 0.0)) for p in pages if scores.get(p.url, 0.0) >= threshold]
        kept.sort(key=lambda item: item[1], reverse=True)
        if not kept:
            raise RuntimeError(
                "Sources were found but none passed the relevance filter for this query."
            )

        with transaction.atomic():
            sources = [_store_source(task, page, rel) for page, rel in kept]
        task.sources_kept = len(sources)
        task.save(update_fields=["sources_kept"])

        # 5. Summarization ----------------------------------------------------
        for i, source in enumerate(sources):
            task.set_stage(
                ResearchTask.Status.SUMMARIZING,
                f"Summarizing source {i + 1}/{len(sources)}",
                72 + int(20 * (i + 1) / len(sources)),
            )
            source.summary = summarizer.summarize_source(
                task.query, source.title, source.url, source.content
            )
            source.save(update_fields=["summary"])

        task.set_stage(ResearchTask.Status.SUMMARIZING, "Writing consolidated summary", 95)
        task.summary = summarizer.consolidate(
            task.query,
            [
                {"title": s.title, "url": s.url, "summary": s.summary}
                for s in sorted(sources, key=lambda s: s.quality_score, reverse=True)
            ],
        )

        # 6. Refresh the session-level summary across all completed runs.
        session = task.session
        _refresh_session_summary(session, task)

        task.status = ResearchTask.Status.COMPLETED
        task.stage_detail = "Research complete"
        task.progress = 100
        task.finished_at = timezone.now()
        task.save(
            update_fields=["summary", "status", "stage_detail", "progress", "finished_at"]
        )
        logger.info(
            "Research task %s completed: %s sources kept for %r",
            task.pk,
            task.sources_kept,
            task.query,
        )

    except Exception as exc:
        logger.exception("Research task %s failed", task_id)
        task.status = ResearchTask.Status.FAILED
        task.error = str(exc)[:2000]
        task.finished_at = timezone.now()
        task.save(update_fields=["status", "error", "finished_at"])
        # Re-raise only infra-flavored errors so Celery retries them.
        if not isinstance(exc, RuntimeError):
            raise self.retry(exc=exc)


def _refresh_session_summary(session: ResearchSession, latest_task: ResearchTask):
    """Keep the session summary in sync as research runs accumulate."""
    completed = list(
        session.tasks.filter(status=ResearchTask.Status.COMPLETED).exclude(summary="")
    )
    parts = [t.summary for t in completed] + ([latest_task.summary] if latest_task.summary else [])
    if not parts:
        session.summary = latest_task.summary or session.summary
    elif len(parts) == 1:
        session.summary = parts[0]
    else:
        merged = summarizer.consolidate(
            session.title,
            [
                {"title": f"Research run: {t.query}", "url": "", "summary": t.summary}
                for t in completed + [latest_task]
                if t.summary
            ],
        )
        session.summary = merged or "\n\n---\n\n".join(parts)
    session.save(update_fields=["summary", "updated_at"])
