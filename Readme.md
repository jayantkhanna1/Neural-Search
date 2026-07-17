# Neural Search — AI Research Assistant

An AI-powered research assistant built with Django, Celery and the Anthropic API.
You give it a topic; it expands the query with Claude Haiku, performs deep
background web research, filters and ranks the sources, summarizes the findings,
and then lets you **chat with the gathered knowledge** (RAG) — including asking
it to research additional topics inside the same conversation.

## How it works

```
User query
   │
   ▼
1. Query expansion (Claude Haiku) ─ related search queries + topic category
   │
   ▼
2. Iterative background research (Celery task, up to N rounds)
   ├─ Web search per query (Tavily / Brave / DuckDuckGo / Google chain,
   │  arXiv for academic topics; cached, throttled)
   ├─ Parallel polite fetching (robots.txt, per-domain rate limiting,
   │  HTTP retries, PDF extraction, optional JS rendering)
   ├─ Content extraction (trafilatura + fallback; publication dates)
   ├─ Relevance filtering (Claude Haiku, batched) ─ drops spam, duplicates,
   │  off-topic pages; quality ranking includes freshness
   ├─ Indexing: contextual chunking (+ optional Voyage embeddings)
   ├─ Per-source summaries (sync or Message Batches API at 50% cost)
   └─ Gap analysis: model reviews coverage, generates follow-up queries,
      loops for another round if material is missing
   │
   ▼
3. Finalization
   ├─ consolidated research brief (key findings, insights, references)
   └─ key-claim extraction with cross-source corroboration + confidence
   │
   ▼
4. Chat with research (RAG)
   ├─ hybrid retrieval: BM25 + semantic embeddings fused with RRF
   ├─ answers carry real per-passage citations (Anthropic citations API)
   └─ "research X" in chat launches additional background research
      into the same session (tool use)
```

Every LLM call in the research rounds runs under a per-task token budget;
if it's exhausted the task finalizes gracefully with what was gathered.

## Project structure

```
neural_search/          Django project (settings, celery app, urls)
research/
  models.py             ResearchSession, ResearchTask, Source, DocumentChunk, ChatMessage
  tasks.py              Celery research pipeline with staged progress reporting
  views.py / urls.py    JSON API + frontend page
  services/
    anthropic_client.py Anthropic SDK wrapper (retries, structured outputs,
                        token budgeting, Message Batches)
    query_expansion.py  Haiku query expansion + topic categorization (cached)
    search.py           Tavily/Brave/DuckDuckGo/Google + arXiv providers
    scraper.py          robots.txt, parallel-safe rate limits, trafilatura,
                        PDFs, publication dates, optional Playwright
    relevance.py        Haiku relevance filtering + quality/freshness ranking
    gap_analysis.py     Coverage review driving the iterative research loop
    summarizer.py       Per-source + consolidated summaries (batch-capable)
    claims.py           Key-claim extraction with cross-source corroboration
    chunking.py         Overlapping text chunking for RAG
    contextualize.py    Situating line per chunk (contextual retrieval,
                        prompt-cached document)
    embeddings.py       Optional Voyage AI embeddings (REST)
    retrieval.py        Hybrid BM25 + semantic retrieval with RRF fusion
    chat.py             RAG chat with citations + research-launch tool
templates/index.html    Single-page frontend
static/                 CSS + JS for the frontend
```

## Setup

Requirements: Python 3.11+, Redis.

```bash
pip install -r requirements.txt
cp .env.example .env          # then set ANTHROPIC_API_KEY
python manage.py migrate
```

Run the three processes:

```bash
redis-server                                          # broker/cache
celery -A neural_search worker --loglevel=info        # research worker
python manage.py runserver                            # web app -> http://127.0.0.1:8000
```

By default SQLite is used; set `DB_NAME`/`DB_USER`/... for PostgreSQL.

## API

| Method | Path                              | Purpose                                   |
|--------|-----------------------------------|-------------------------------------------|
| POST   | `/api/research/`                  | `{query}` → create session + start research |
| GET    | `/api/sessions/`                  | List sessions                             |
| GET    | `/api/sessions/<id>/`             | Session status, progress, summary, claims, sources, chat history |
| GET    | `/api/sessions/<id>/events/`      | Server-Sent Events stream of session state while researching |
| POST   | `/api/sessions/<id>/research/`    | `{query}` → additional research in a session |
| POST   | `/api/sessions/<id>/chat/`        | `{message}` → RAG chat reply with citations |

## Reliability notes

- **Anthropic API**: SDK-level retries with backoff; structured outputs
  (JSON schema) for machine-read responses; typed error handling; graceful
  degradation (e.g. query expansion failure falls back to the original query).
- **Web politeness**: robots.txt honored (cached per domain), per-domain
  request delays, global search-provider throttling, HTTP retries for
  429/5xx with `Retry-After` support.
- **Celery**: late acks, per-task time limits, retry with exponential backoff
  for infrastructure errors, idempotency guard against duplicate delivery.
- **Deduplication**: URLs deduped per session; content deduped by normalized
  content hash (catches mirrors).
- **Caching**: search results and expanded queries cached (Redis when
  configured), robots.txt cached 24h.
- **Token efficiency**: relevance judged on excerpts in batches; summaries
  built from bounded excerpts; the final brief consolidates per-source
  summaries instead of raw pages.

## Tests

```bash
python manage.py test
```
