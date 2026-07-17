"""Web search providers with caching, throttling and graceful fallback.

Primary provider is DuckDuckGo's HTML endpoint (no API key required);
googlesearch-python is used as a fallback. Search results are cached so
repeated or overlapping research runs don't re-hit the providers.
"""

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_search_lock = threading.Lock()
_last_search_at = 0.0
_SEARCH_MIN_INTERVAL = 2.5  # seconds between provider hits, politeness


@dataclass
class SearchResult:
    url: str
    title: str = ""
    snippet: str = ""


def _throttle():
    global _last_search_at
    with _search_lock:
        wait = _SEARCH_MIN_INTERVAL - (time.monotonic() - _last_search_at)
        if wait > 0:
            time.sleep(wait)
        _last_search_at = time.monotonic()


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


def _search_google(query: str, max_results: int) -> list[SearchResult]:
    from googlesearch import search as google_search

    _throttle()
    urls = list(google_search(query, num_results=max_results))
    return [SearchResult(url=u) for u in urls if isinstance(u, str) and u.startswith("http")]


def search_web(query: str, max_results: int | None = None) -> list[SearchResult]:
    """Search the web for a query, trying providers in order with caching."""
    max_results = max_results or settings.RESEARCH["RESULTS_PER_QUERY"]
    cache_key = "search:" + hashlib.sha256(f"{max_results}:{query}".encode()).hexdigest()
    cached = cache.get(cache_key)
    if cached is not None:
        return [SearchResult(**r) for r in cached]

    results: list[SearchResult] = []
    for provider in (_search_duckduckgo, _search_google):
        try:
            results = provider(query, max_results)
            if results:
                break
        except Exception as exc:  # provider failures should never kill the run
            logger.warning("Search provider %s failed for %r: %s", provider.__name__, query, exc)

    if results:
        cache.set(
            cache_key,
            [r.__dict__ for r in results],
            settings.RESEARCH["SEARCH_CACHE_TTL"],
        )
    return results
