"""Celery tasks: the background research pipeline.

The pipeline is an iterative deep-research loop:

  expand query ──▶ ┌─ round 1..N ────────────────────────────────┐
                   │ search ─▶ fetch (parallel) ─▶ filter ─▶     │
                   │ store + contextualize + embed ─▶ summarize  │
                   │            │                                │
                   │   gap analysis: enough coverage?            │
                   └──── no: follow-up queries ── yes: stop ─────┘
                   ──▶ consolidate ─▶ extract claims ─▶ done

Each stage persists progress on the ResearchTask for live UI updates. All
LLM calls inside the rounds run under a token budget; when it's exhausted the
pipeline finalizes gracefully with whatever was gathered. Stored sources and
summaries survive Celery retries, so a retried task resumes rather than
repeating completed LLM work.
"""

import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import DocumentChunk, ResearchSession, ResearchTask, Source
from .services import (
    anthropic_client,
    chunking,
    claims,
    contextualize,
    embeddings,
    gap_analysis,
    query_expansion,
    relevance,
    scraper,
    search,
    summarizer,
)

logger = logging.getLogger(__name__)


def _content_hash(text: str) -> str:
    # Hash a normalized prefix so near-identical mirrors dedupe cheaply.
    normalized = " ".join(text.lower().split())[:5000]
    return hashlib.sha256(normalized.encode()).hexdigest()


def _round_progress(round_no: int, total_rounds: int, fraction: float) -> int:
    """Map a within-round fraction (0..1) to overall progress (5..88)."""
    span = 83 / total_rounds
    return int(5 + span * (round_no - 1) + span * min(1.0, max(0.0, fraction)))


# ------------------------------------------------------------------ stages
def _collect_urls(
    task: ResearchTask,
    queries: list[str],
    category: str,
    round_no: int,
    total_rounds: int,
) -> list[search.SearchResult]:
    """Run every query through web search, deduplicating URLs across the session."""
    seen: set[str] = set(task.session.sources.values_list("url", flat=True))
    collected: list[search.SearchResult] = []
    max_urls = settings.RESEARCH["MAX_URLS_PER_TASK"]

    for i, query in enumerate(queries):
        task.set_stage(
            ResearchTask.Status.SEARCHING,
            f"Round {round_no}: searching ({i + 1}/{len(queries)}): {query}",
            progress=_round_progress(round_no, total_rounds, 0.2 * (i + 1) / len(queries)),
        )
        for result in search.search_web(query, category=category):
            url = result.url.split("#", 1)[0]
            if url in seen:
                continue
            seen.add(url)
            collected.append(result)
            if len(collected) >= max_urls:
                return collected
    return collected


def _fetch_pages(
    task: ResearchTask,
    results: list[search.SearchResult],
    round_no: int,
    total_rounds: int,
) -> list[scraper.FetchedPage]:
    """Fetch URLs concurrently (I/O bound; per-domain throttling stays polite)."""
    pages: list[scraper.FetchedPage] = []
    seen_hashes: set[str] = set(task.session.sources.values_list("content_hash", flat=True))
    done = 0

    with ThreadPoolExecutor(max_workers=settings.RESEARCH["FETCH_CONCURRENCY"]) as pool:
        futures = {pool.submit(scraper.fetch_page, r.url): r for r in results}
        for future in as_completed(futures):
            result = futures[future]
            done += 1
            task.set_stage(
                ResearchTask.Status.FETCHING,
                f"Round {round_no}: reading sources ({done}/{len(results)})",
                progress=_round_progress(
                    round_no, total_rounds, 0.2 + 0.4 * done / len(results)
                ),
            )
            try:
                page = future.result()
            except Exception as exc:  # a single bad URL must not sink the round
                logger.warning("Fetch crashed for %s: %s", result.url, exc)
                continue
            if page is None:
                continue
            digest = _content_hash(page.text)
            if digest in seen_hashes:  # duplicate/mirrored content
                continue
            seen_hashes.add(digest)
            if not page.title and result.title:
                page.title = result.title
            pages.append(page)

    task.sources_fetched += len(pages)
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
        quality_score=relevance.quality_score(
            page.url, rel, len(page.text), page.published_at
        ),
        published_at=page.published_at,
    )

    texts = chunking.chunk_text(page.text)
    contexts = contextualize.contextualize_chunks(page.title, page.url, page.text, texts)

    vectors = None
    if embeddings.enabled():
        vectors = embeddings.embed_documents(
            [f"{ctx}\n{txt}" if ctx else txt for ctx, txt in zip(contexts, texts)]
        )

    DocumentChunk.objects.bulk_create(
        [
            DocumentChunk(
                session=task.session,
                source=source,
                index=i,
                text=text,
                context=contexts[i],
                embedding=vectors[i] if vectors else None,
            )
            for i, text in enumerate(texts)
        ]
    )
    return source


def _run_round(
    task: ResearchTask,
    queries: list[str],
    category: str,
    round_no: int,
    total_rounds: int,
) -> list[Source]:
    """One search → fetch → filter → store → summarize round. Returns kept sources."""
    results = _collect_urls(task, queries, category, round_no, total_rounds)
    task.urls_found += len(results)
    task.save(update_fields=["urls_found"])
    if not results:
        return []

    pages = _fetch_pages(task, results, round_no, total_rounds)
    if not pages:
        return []

    task.set_stage(
        ResearchTask.Status.FILTERING,
        f"Round {round_no}: checking {len(pages)} sources for relevance",
        progress=_round_progress(round_no, total_rounds, 0.65),
    )
    scores = relevance.score_relevance(task.query, pages)
    threshold = settings.RESEARCH["RELEVANCE_THRESHOLD"]
    kept = [(p, scores.get(p.url, 0.0)) for p in pages if scores.get(p.url, 0.0) >= threshold]
    kept.sort(key=lambda item: item[1], reverse=True)
    if not kept:
        return []

    task.set_stage(
        ResearchTask.Status.FILTERING,
        f"Round {round_no}: indexing {len(kept)} sources for retrieval",
        progress=_round_progress(round_no, total_rounds, 0.72),
    )
    with transaction.atomic():
        sources = [_store_source(task, page, rel) for page, rel in kept]
    task.sources_kept += len(sources)
    task.save(update_fields=["sources_kept"])

    def _summary_progress(i, total):
        task.set_stage(
            ResearchTask.Status.SUMMARIZING,
            f"Round {round_no}: summarizing source {i + 1}/{total}",
            progress=_round_progress(round_no, total_rounds, 0.75 + 0.25 * (i + 1) / total),
        )

    task.set_stage(
        ResearchTask.Status.SUMMARIZING,
        f"Round {round_no}: summarizing {len(sources)} sources",
        progress=_round_progress(round_no, total_rounds, 0.75),
    )
    summarizer.summarize_sources(task.query, sources, progress_callback=_summary_progress)
    for source in sources:
        source.save(update_fields=["summary"])
    return sources


def _run_rounds(task: ResearchTask):
    """The iterative research loop, run under the task token budget."""
    total_rounds = max(1, settings.RESEARCH["MAX_ROUNDS"])

    task.set_stage(ResearchTask.Status.EXPANDING, "Generating related search queries", 3)
    expanded = query_expansion.expand_query(task.query)
    queries = expanded.queries
    task.expanded_queries = list(queries)
    task.save(update_fields=["expanded_queries"])

    searched: list[str] = []
    for round_no in range(1, total_rounds + 1):
        new_sources = _run_round(task, queries, expanded.category, round_no, total_rounds)
        searched.extend(queries)
        task.rounds_completed = round_no
        task.save(update_fields=["rounds_completed"])

        if round_no == 1 and not new_sources and not task.session.sources.exists():
            raise RuntimeError(
                "No usable sources were found. Try rewording the query or retry later."
            )
        if round_no >= total_rounds:
            break

        # Should we keep digging? Ask the model what's still missing.
        task.set_stage(
            ResearchTask.Status.SEARCHING,
            f"Round {round_no}: reviewing coverage for gaps",
            progress=_round_progress(round_no, total_rounds, 1.0),
        )
        summaries = list(
            task.sources.exclude(summary="").values_list("summary", flat=True)
        )
        report = gap_analysis.analyze(task.query, summaries, searched)
        if report.sufficient or not report.followup_queries:
            logger.info("Task %s: coverage sufficient after round %d", task.pk, round_no)
            break
        logger.info(
            "Task %s: round %d gaps (%s) -> %s",
            task.pk, round_no, report.missing[:120], report.followup_queries,
        )
        task.expanded_queries = task.expanded_queries + report.followup_queries
        task.save(update_fields=["expanded_queries"])
        queries = report.followup_queries


def _finalize(task: ResearchTask):
    """Consolidate summaries, extract claims, refresh the session summary.

    Runs outside the token budget so a budget-exhausted run still produces
    its deliverables.
    """
    sources = list(task.sources.order_by("-quality_score"))
    if sources:
        task.set_stage(ResearchTask.Status.SUMMARIZING, "Writing consolidated summary", 90)
        task.summary = summarizer.consolidate(
            task.query,
            [{"title": s.title, "url": s.url, "summary": s.summary} for s in sources],
        )

    session = task.session
    session_sources = list(session.sources.order_by("-quality_score"))
    if session_sources:
        task.set_stage(ResearchTask.Status.SUMMARIZING, "Verifying key claims", 96)
        session.claims = claims.extract_claims(
            session.title,
            [{"title": s.title, "url": s.url, "summary": s.summary} for s in session_sources],
        )

    _refresh_session_summary(session, task)


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
        with anthropic_client.track_usage(settings.RESEARCH["TASK_TOKEN_BUDGET"]) as tracker:
            try:
                _run_rounds(task)
            except anthropic_client.BudgetExceededError as exc:
                logger.warning("Task %s hit its token budget: %s", task_id, exc)
                task.stage_detail = "Token budget reached — finalizing with gathered sources"
                task.save(update_fields=["stage_detail"])
        task.tokens_used = tracker.total_tokens

        _finalize(task)

        task.status = ResearchTask.Status.COMPLETED
        task.stage_detail = "Research complete"
        task.progress = 100
        task.finished_at = timezone.now()
        task.save(
            update_fields=[
                "summary", "status", "stage_detail", "progress",
                "finished_at", "tokens_used",
            ]
        )
        logger.info(
            "Research task %s completed: %d sources kept over %d round(s), %d tokens for %r",
            task.pk, task.sources_kept, task.rounds_completed, task.tokens_used, task.query,
        )

    except Exception as exc:
        logger.exception("Research task %s failed", task_id)
        task.status = ResearchTask.Status.FAILED
        task.error = str(exc)[:2000]
        task.finished_at = timezone.now()
        task.save(update_fields=["status", "error", "finished_at"])
        # Re-raise only infra-flavored errors so Celery retries them; stored
        # sources and summaries make the retry resume, not repeat.
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
    session.save(update_fields=["summary", "claims", "updated_at"])
