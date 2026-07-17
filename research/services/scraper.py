"""Polite page fetching and main-content extraction.

Responsibilities:
- respect robots.txt (cached per domain)
- per-domain rate limiting
- HTTP retries with backoff for transient failures
- strip navigation/ads/boilerplate and return readable text
"""

import logging
import threading
import time
from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.core.cache import cache
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .search import USER_AGENT

logger = logging.getLogger(__name__)

_domain_lock = threading.Lock()
_last_hit_per_domain: dict[str, float] = {}

_SKIP_EXTENSIONS = (
    ".pdf", ".zip", ".gz", ".tar", ".rar", ".exe", ".dmg", ".mp3", ".mp4",
    ".avi", ".mov", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".css", ".js", ".xml", ".woff", ".woff2",
)

_REMOVE_TAGS = (
    "script", "style", "noscript", "nav", "header", "footer", "aside",
    "form", "iframe", "svg", "button", "select", "input", "template",
)

_BOILERPLATE_MARKERS = (
    "cookie", "consent", "sidebar", "advert", "promo", "banner", "popup",
    "newsletter", "subscribe", "breadcrumb", "share", "social", "related-posts",
    "comments",
)


@dataclass
class FetchedPage:
    url: str
    title: str
    text: str


def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en"})
    return s


_http = _session()


def _robots_allows(url: str) -> bool:
    if not settings.RESEARCH["RESPECT_ROBOTS"]:
        return True
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    cache_key = f"robots:{origin}"
    rules = cache.get(cache_key)
    if rules is None:
        parser = robotparser.RobotFileParser()
        try:
            response = _http.get(f"{origin}/robots.txt", timeout=10)
            if response.status_code == 200:
                parser.parse(response.text.splitlines())
                rules = response.text
            else:
                rules = ""  # no robots file -> everything allowed
        except requests.RequestException:
            rules = ""  # unreachable robots -> assume allowed, stay polite via throttle
        cache.set(cache_key, rules, 24 * 3600)
    else:
        parser = robotparser.RobotFileParser()
        parser.parse(rules.splitlines())
    if not rules:
        return True
    return parser.can_fetch(USER_AGENT, url)


def _throttle_domain(domain: str):
    delay = settings.RESEARCH["PER_DOMAIN_DELAY"]
    with _domain_lock:
        last = _last_hit_per_domain.get(domain, 0.0)
        wait = delay - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        _last_hit_per_domain[domain] = time.monotonic()


def _extract_text(html: str) -> tuple[str, str]:
    """Return (title, readable_text) from raw HTML."""
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    for tag in soup.find_all(_REMOVE_TAGS):
        tag.decompose()

    # Note: decomposing a tag detaches its descendants (attrs becomes None),
    # so tags already collected by find_all must be skipped once decomposed.
    for tag in soup.find_all(attrs={"class": True}):
        if tag.decomposed or tag.attrs is None:
            continue
        classes = " ".join(tag.get("class") or []).lower()
        if any(marker in classes for marker in _BOILERPLATE_MARKERS):
            tag.decompose()
    for tag in soup.find_all(attrs={"id": True}):
        if tag.decomposed or tag.attrs is None:
            continue
        tag_id = (tag.get("id") or "").lower()
        if any(marker in tag_id for marker in _BOILERPLATE_MARKERS):
            tag.decompose()

    # Prefer semantic main-content containers when present.
    root = (
        soup.find("article")
        or soup.find("main")
        or soup.find(attrs={"role": "main"})
        or soup.body
        or soup
    )

    blocks = []
    for el in root.find_all(["p", "h1", "h2", "h3", "h4", "li", "pre", "blockquote", "td"]):
        text = el.get_text(" ", strip=True)
        if len(text) >= 30 or el.name.startswith("h"):
            blocks.append(text)

    text = "\n".join(dict.fromkeys(blocks))  # drop exact duplicate blocks, keep order
    if len(text) < settings.RESEARCH["MIN_CONTENT_CHARS"]:
        # Fallback: whole-document text for pages with unusual markup.
        text = root.get_text("\n", strip=True)
    return title, text[: settings.RESEARCH["MAX_CONTENT_CHARS"]]


def fetch_page(url: str) -> FetchedPage | None:
    """Fetch a URL politely and extract its main content. Returns None on failure."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.path.lower().endswith(_SKIP_EXTENSIONS):
        logger.debug("Skipping non-HTML resource %s", url)
        return None
    if not _robots_allows(url):
        logger.info("robots.txt disallows %s", url)
        return None

    _throttle_domain(parsed.netloc)
    try:
        response = _http.get(url, timeout=settings.RESEARCH["FETCH_TIMEOUT"])
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.info("Fetch failed for %s: %s", url, exc)
        return None

    content_type = response.headers.get("Content-Type", "")
    if "html" not in content_type and "text" not in content_type:
        return None

    try:
        title, text = _extract_text(response.text)
    except Exception as exc:
        logger.warning("Content extraction failed for %s: %s", url, exc)
        return None

    if len(text) < settings.RESEARCH["MIN_CONTENT_CHARS"]:
        return None
    return FetchedPage(url=url, title=title, text=text)
