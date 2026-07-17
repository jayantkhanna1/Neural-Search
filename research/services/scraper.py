"""Polite page fetching and main-content extraction.

Responsibilities:
- respect robots.txt (cached per domain)
- per-domain rate limiting that is safe under concurrent fetching
- HTTP retries with backoff for transient failures
- content extraction via trafilatura (with a BeautifulSoup fallback)
- PDF text extraction (papers, whitepapers)
- publication-date extraction for freshness scoring
- optional Playwright rendering for JS-heavy pages
"""

import io
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
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
_next_slot_per_domain: dict[str, float] = {}

_SKIP_EXTENSIONS = (
    ".zip", ".gz", ".tar", ".rar", ".exe", ".dmg", ".mp3", ".mp4",
    ".avi", ".mov", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".css", ".js", ".woff", ".woff2",
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

_DATE_META_KEYS = (
    ("property", "article:published_time"),
    ("property", "og:published_time"),
    ("name", "date"),
    ("name", "publish-date"),
    ("name", "dc.date"),
    ("name", "dcterms.date"),
    ("itemprop", "datePublished"),
)


@dataclass
class FetchedPage:
    url: str
    title: str
    text: str
    published_at: datetime | None = None


def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=32)
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
        try:
            response = _http.get(f"{origin}/robots.txt", timeout=10)
            rules = response.text if response.status_code == 200 else ""
        except requests.RequestException:
            rules = ""  # unreachable robots -> assume allowed, stay polite via throttle
        cache.set(cache_key, rules, 24 * 3600)
    if not rules:
        return True
    parser = robotparser.RobotFileParser()
    parser.parse(rules.splitlines())
    return parser.can_fetch(USER_AGENT, url)


def _throttle_domain(domain: str):
    """Reserve the domain's next allowed slot, sleeping outside the lock so
    parallel fetches to different domains never block each other."""
    delay = settings.RESEARCH["PER_DOMAIN_DELAY"]
    with _domain_lock:
        now = time.monotonic()
        slot = max(now, _next_slot_per_domain.get(domain, 0.0))
        _next_slot_per_domain[domain] = slot + delay
    wait = slot - time.monotonic()
    if wait > 0:
        time.sleep(wait)


# ------------------------------------------------------------------ metadata
def _parse_date(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    # Try ISO-8601 first (covers most meta tags), then a couple of common forms.
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)
    except ValueError:
        pass
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
    if match:
        try:
            return datetime(*map(int, match.groups()), tzinfo=dt_timezone.utc)
        except ValueError:
            return None
    return None


def _extract_published_at(soup: BeautifulSoup) -> datetime | None:
    for attr, key in _DATE_META_KEYS:
        tag = soup.find("meta", attrs={attr: key})
        if tag:
            parsed = _parse_date(tag.get("content", ""))
            if parsed:
                return parsed
    time_tag = soup.find("time", attrs={"datetime": True})
    if time_tag:
        return _parse_date(time_tag.get("datetime", ""))
    return None


# ---------------------------------------------------------------- extraction
def _extract_with_soup(soup: BeautifulSoup) -> str:
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
        text = root.get_text("\n", strip=True)
    return text


def _extract_html(html: str) -> tuple[str, str, datetime | None]:
    """Return (title, readable_text, published_at) from raw HTML."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    published_at = _extract_published_at(soup)

    text = ""
    try:
        import trafilatura

        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
        ) or ""
        if not published_at:
            meta = trafilatura.extract_metadata(html)
            if meta and meta.date:
                published_at = _parse_date(meta.date)
            if meta and not title and meta.title:
                title = meta.title
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("trafilatura failed, falling back to soup extraction: %s", exc)

    if len(text) < settings.RESEARCH["MIN_CONTENT_CHARS"]:
        text = _extract_with_soup(soup)

    return title, text[: settings.RESEARCH["MAX_CONTENT_CHARS"]], published_at


def _extract_pdf(content: bytes, url: str) -> tuple[str, str]:
    """Return (title, text) from PDF bytes."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    title = ""
    if reader.metadata and reader.metadata.title:
        title = str(reader.metadata.title)
    pages = []
    for page in reader.pages[:60]:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # single bad page shouldn't sink the document
            continue
    text = "\n".join(pages)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not title:
        title = urlparse(url).path.rsplit("/", 1)[-1] or url
    return title, text[: settings.RESEARCH["MAX_CONTENT_CHARS"]]


def _render_with_browser(url: str) -> str | None:
    """Render a JS-heavy page with Playwright. Optional; returns HTML or None."""
    if not settings.RESEARCH["ENABLE_BROWSER"]:
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=USER_AGENT)
                page.goto(url, wait_until="networkidle", timeout=30_000)
                return page.content()
            finally:
                browser.close()
    except Exception as exc:
        logger.info("Browser rendering failed for %s: %s", url, exc)
        return None


# ------------------------------------------------------------------ fetching
def fetch_page(url: str) -> FetchedPage | None:
    """Fetch a URL politely and extract its main content. Returns None on failure.

    Thread-safe: designed to be called from a ThreadPoolExecutor. Does not
    touch the database.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.path.lower().endswith(_SKIP_EXTENSIONS):
        logger.debug("Skipping non-text resource %s", url)
        return None
    if not _robots_allows(url):
        logger.info("robots.txt disallows %s", url)
        return None

    _throttle_domain(parsed.netloc)
    try:
        response = _http.get(
            url,
            timeout=settings.RESEARCH["FETCH_TIMEOUT"],
            stream=True,
        )
        response.raise_for_status()
        content_type = (response.headers.get("Content-Type") or "").lower()
        is_pdf = "pdf" in content_type or parsed.path.lower().endswith(".pdf")
        max_bytes = settings.RESEARCH["MAX_PDF_BYTES"]
        body = response.raw.read(max_bytes + 1, decode_content=True)
        if len(body) > max_bytes:
            logger.info("Skipping oversized resource %s", url)
            return None
    except requests.RequestException as exc:
        logger.info("Fetch failed for %s: %s", url, exc)
        return None

    published_at = None
    try:
        if is_pdf:
            title, text = _extract_pdf(body, url)
        elif "html" in content_type or "text" in content_type or not content_type:
            html = body.decode(response.encoding or "utf-8", errors="replace")
            title, text, published_at = _extract_html(html)
            # JS-heavy page? Optionally render it in a real browser and retry.
            if len(text) < settings.RESEARCH["MIN_CONTENT_CHARS"]:
                rendered = _render_with_browser(url)
                if rendered:
                    title, text, published_at = _extract_html(rendered)
        else:
            return None
    except Exception as exc:
        logger.warning("Content extraction failed for %s: %s", url, exc)
        return None

    if len(text) < settings.RESEARCH["MIN_CONTENT_CHARS"]:
        return None
    return FetchedPage(url=url, title=title, text=text, published_at=published_at)
