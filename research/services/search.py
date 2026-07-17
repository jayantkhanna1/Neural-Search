"""Web search providers with caching, throttling and graceful fallback.

Provider chain (first that yields results wins):
  1. Tavily        — when TAVILY_API_KEY is set (recommended: reliable + snippets)
  2. Brave Search  — when BRAVE_API_KEY is set
  3. DuckDuckGo    — HTML endpoint, no key required
  4. Google        — googlesearch-python scrape, last resort

Academic queries additionally get results from the arXiv API. All results are
cached so repeated or overlapping research runs don't re-hit the providers.
"""

import hashlib
import logging
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.core.cache import cache
from urllib.parse import parse_qs, unquote, urlparse

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_search_lock = threading.Lock()
_next_search_slot = 0.0
_SEARCH_MIN_INTERVAL = 2.5  # seconds between provider hits, politeness


@dataclass
class SearchResult:
    url: str
    title: str = ""
    snippet: str = ""


def _throttle():
    """Reserve the next allowed provider-hit slot; sleep outside the lock so
    concurrent callers queue up instead of serializing on the lock itself."""
    global _next_search_slot
    with _search_lock:
        now = time.monotonic()
        slot = max(now, _next_search_slot)
        _next_search_slot = slot + _SEARCH_MIN_INTERVAL
    wait = slot - time.monotonic()
    if wait > 0:
        time.sleep(wait)


# --------------------------------------------------------------------- Tavily
def _search_tavily(query: str, max_results: int) -> list[SearchResult]:
    api_key = settings.RESEARCH["TAVILY_API_KEY"]
    if not api_key:
        return []
    _throttle()
    response = requests.post(
        "https://api.tavily.com/search",
        json={"query": query, "max_results": max_results, "search_depth": "basic"},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=settings.RESEARCH["FETCH_TIMEOUT"],
    )
    response.raise_for_status()
    return [
        SearchResult(
            url=item.get("url", ""),
            title=item.get("title", ""),
            snippet=(item.get("content") or "")[:400],
        )
        for item in response.json().get("results", [])
        if item.get("url", "").startswith("http")
    ]


# ---------------------------------------------------------------------- Brave
def _search_brave(query: str, max_results: int) -> list[SearchResult]:
    api_key = settings.RESEARCH["BRAVE_API_KEY"]
    if not api_key:
        return []
    _throttle()
    response = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": max_results},
        headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
        timeout=settings.RESEARCH["FETCH_TIMEOUT"],
    )
    response.raise_for_status()
    items = (response.json().get("web") or {}).get("results", [])
    return [
        SearchResult(
            url=item.get("url", ""),
            title=item.get("title", ""),
            snippet=(item.get("description") or "")[:400],
        )
        for item in items
        if item.get("url", "").startswith("http")
    ]


# ----------------------------------------------------------------- DuckDuckGo
def _clean_ddg_url(href: str) -> str | None:
    """DuckDuckGo wraps results in a redirect URL; unwrap it."""
    if not href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [None])[0]
        return unquote(target) if target else None
    if parsed.scheme in ("http", "https"):
        return href
    return None


def _search_duckduckgo(query: str, max_results: int) -> list[SearchResult]:
    _throttle()
    response = requests.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers={"User-Agent": USER_AGENT},
        timeout=settings.RESEARCH["FETCH_TIMEOUT"],
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    results = []
    for item in soup.select("div.result"):
        link = item.select_one("a.result__a")
        if not link:
            continue
        url = _clean_ddg_url(link.get("href", ""))
        if not url:
            continue
        snippet_el = item.select_one(".result__snippet")
        results.append(
            SearchResult(
                url=url,
                title=link.get_text(" ", strip=True),
                snippet=snippet_el.get_text(" ", strip=True) if snippet_el else "",
            )
        )
        if len(results) >= max_results:
            break
    return results


# --------------------------------------------------------------------- Google
def _search_google(query: str, max_results: int) -> list[SearchResult]:
    from googlesearch import search as google_search

    _throttle()
    urls = list(google_search(query, num_results=max_results))
    return [SearchResult(url=u) for u in urls if isinstance(u, str) and u.startswith("http")]


# ---------------------------------------------------------------------- arXiv
def search_arxiv(query: str, max_results: int = 4) -> list[SearchResult]:
    """Query the arXiv API for papers; returns abstract-page URLs with the
    paper abstract as the snippet."""
    cache_key = "arxiv:" + hashlib.sha256(f"{max_results}:{query}".encode()).hexdigest()
    cached = cache.get(cache_key)
    if cached is not None:
        return [SearchResult(**r) for r in cached]

    try:
        _throttle()
        response = requests.get(
            "https://export.arxiv.org/api/query",
            params={
                "search_query": f"all:{query}",
                "max_results": max_results,
                "sortBy": "relevance",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=settings.RESEARCH["FETCH_TIMEOUT"],
        )
        response.raise_for_status()
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(response.text)
        results = []
        for entry in root.findall("atom:entry", ns):
            link = (entry.findtext("atom:id", "", ns) or "").strip()
            title = " ".join((entry.findtext("atom:title", "", ns) or "").split())
            abstract = " ".join((entry.findtext("atom:summary", "", ns) or "").split())
            if link.startswith("http"):
                results.append(SearchResult(url=link, title=title, snippet=abstract[:400]))
    except (requests.RequestException, ET.ParseError) as exc:
        logger.warning("arXiv search failed for %r: %s", query, exc)
        return []

    cache.set(cache_key, [r.__dict__ for r in results], settings.RESEARCH["SEARCH_CACHE_TTL"])
    return results


# ----------------------------------------------------------------- entrypoint
_PROVIDERS = (_search_tavily, _search_brave, _search_duckduckgo, _search_google)


def search_web(query: str, max_results: int | None = None, category: str = "general") -> list[SearchResult]:
    """Search the web for a query, trying providers in order with caching.

    ``category`` (from query expansion) routes academic queries through the
    arXiv API in addition to the general provider chain.
    """
    max_results = max_results or settings.RESEARCH["RESULTS_PER_QUERY"]
    cache_key = "search:" + hashlib.sha256(
        f"{max_results}:{category}:{query}".encode()
    ).hexdigest()
    cached = cache.get(cache_key)
    if cached is not None:
        return [SearchResult(**r) for r in cached]

    results: list[SearchResult] = []
    if category == "academic":
        results.extend(search_arxiv(query, max_results=min(4, max_results)))

    for provider in _PROVIDERS:
        try:
            general = provider(query, max_results)
        except Exception as exc:  # provider failures should never kill the run
            logger.warning("Search provider %s failed for %r: %s", provider.__name__, query, exc)
            continue
        if general:
            seen = {r.url for r in results}
            results.extend(r for r in general if r.url not in seen)
            break

    if results:
        cache.set(
            cache_key,
            [r.__dict__ for r in results],
            settings.RESEARCH["SEARCH_CACHE_TTL"],
        )
    return results
