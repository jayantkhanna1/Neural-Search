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
1. Query expansion (Claude Haiku) ─ generates 5 related search queries
   │
   ▼
2. Background research (Celery task)
   ├─ Web search per query (DuckDuckGo, Google fallback, cached, throttled)
   ├─ Polite fetching (robots.txt, per-domain rate limiting, HTTP retries)
   └─ Main-content extraction (nav/ads/boilerplate stripped)
   │
   ▼
3. Relevance filtering (Claude Haiku, batched) ─ drops spam, duplicates,
   off-topic and low-quality pages; ranks kept sources by quality
   │
   ▼
4. Summarization (Claude Haiku)
   ├─ per-source summaries
   └─ consolidated research brief (key findings, insights, references)
   │
   ▼
5. Chat with research (RAG)
   ├─ sources chunked + indexed, retrieved with BM25 per question
   ├─ follow-up questions answered from the knowledge base
   └─ "research X" in chat launches additional background research
      into the same session (tool use)
```

## Project structure

```
neural_search/          Django project (settings, celery app, urls)
research/
  models.py             ResearchSession, ResearchTask, Source, DocumentChunk, ChatMessage
  tasks.py              Celery research pipeline with staged progress reporting
  views.py / urls.py    JSON API + frontend page
  services/
    anthropic_client.py Anthropic SDK wrapper (retries, structured outputs)
    query_expansion.py  Haiku-based query expansion (cached)
    search.py           Search providers with caching, throttling, fallback
    scraper.py          robots.txt, per-domain rate limits, content extraction
    relevance.py        Haiku relevance filtering + quality ranking
    summarizer.py       Per-source + consolidated summaries
    chunking.py         Overlapping text chunking for RAG
    retrieval.py        BM25 retrieval over session chunks
    chat.py             RAG chat with a tool for launching more research
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
| GET    | `/api/sessions/<id>/`             | Session status, progress, summary, sources, chat history |
| POST   | `/api/sessions/<id>/research/`    | `{query}` → additional research in a session |
| POST   | `/api/sessions/<id>/chat/`        | `{message}` → RAG chat reply              |

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
