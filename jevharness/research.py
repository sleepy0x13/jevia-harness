"""Search, fetch, and cut pages into sections.

This module does no judging. It produces candidates — search hits, page text,
sections — and the agent loop hands them to Jev, which is the part that decides
what is worth reading. Keeping the two apart is what lets the expensive engine
stay out of the retrieval path entirely.
"""
from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .tools import MAX_FETCH_BYTES, FETCH_TIMEOUT, _is_public, safe_opener

SEARCH_URL = "https://html.duckduckgo.com/html/"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 JEVia/1.0"

RESULT_RE = re.compile(
    r'class="result__a"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>', re.S
)
SNIPPET_RE = re.compile(r'class="result__snippet"[^>]*>(?P<text>.*?)</a>', re.S)
TAGS = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.I | re.S)
ANY_TAG = re.compile(r"<[^>]+>")

SECTION_MIN = 220
SECTION_MAX = 1100


@dataclass
class Hit:
    """One search result, before anyone has decided whether it matters."""

    title: str
    url: str
    snippet: str = ""
    rank: int = 0

    @property
    def domain(self) -> str:
        return urllib.parse.urlparse(self.url).netloc

    def as_criterion(self) -> str:
        body = f"{self.title} — {self.snippet}".strip(" —")
        return body[:400] or self.url

    def to_dict(self) -> dict:
        return {"title": self.title, "url": self.url, "snippet": self.snippet,
                "domain": self.domain, "rank": self.rank}


@dataclass
class Section:
    """A slice of a page, small enough to judge on its own."""

    text: str
    url: str
    title: str
    index: int = 0

    def to_dict(self) -> dict:
        return {"url": self.url, "title": self.title, "index": self.index,
                "chars": len(self.text), "preview": self.text[:180]}


@dataclass
class Page:
    url: str
    title: str
    text: str
    ok: bool = True
    error: str = ""
    sections: List[Section] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"url": self.url, "title": self.title, "ok": self.ok,
                "error": self.error, "chars": len(self.text),
                "sections": len(self.sections)}


def _strip(markup: str) -> str:
    return html.unescape(ANY_TAG.sub("", markup)).strip()


def _unwrap(href: str) -> str:
    """DuckDuckGo wraps results in a redirect; the real URL is in ?uddg=."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        query = urllib.parse.parse_qs(parsed.query)
        target = (query.get("uddg") or [""])[0]
        if target:
            return urllib.parse.unquote(target)
    return href


def search(query: str, *, limit: int = 10, timeout: float = 20.0) -> List[Hit]:
    """Search the open web: DuckDuckGo first, Bing when it comes back empty."""
    return search_ddg(query, limit=limit, timeout=timeout) or search_bing(query, limit=limit, timeout=timeout)


def search_ddg(query: str, *, limit: int = 10, timeout: float = 20.0) -> List[Hit]:
    """DuckDuckGo's HTML endpoint. Candidates, ranked as the engine ranked them."""
    query = (query or "").strip()
    if not query:
        return []
    data = urllib.parse.urlencode({"q": query}).encode()
    request = urllib.request.Request(
        SEARCH_URL, data=data,
        headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_FETCH_BYTES).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - a failed search is not a failed run
        return []

    titles = list(RESULT_RE.finditer(body))
    snippets = [_strip(m.group("text")) for m in SNIPPET_RE.finditer(body)]
    hits: List[Hit] = []
    seen = set()
    for index, match in enumerate(titles):
        url = _unwrap(match.group("href"))
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        hits.append(
            Hit(
                title=_strip(match.group("title"))[:200],
                url=url,
                snippet=(snippets[index] if index < len(snippets) else "")[:400],
                rank=len(hits),
            )
        )
        if len(hits) >= limit:
            break
    return hits


BING_URL = "https://www.bing.com/search"
BING_RE = re.compile(
    r'<li class="b_algo".*?<h2[^>]*>\s*<a[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>'
    r'(?P<rest>.*?)</li>', re.S)
BING_SNIPPET = re.compile(r'<p[^>]*>(?P<text>.*?)</p>', re.S)


def search_bing(query: str, *, limit: int = 10, timeout: float = 20.0) -> List[Hit]:
    """The fallback engine, for when DuckDuckGo answers with nothing."""
    url = BING_URL + "?" + urllib.parse.urlencode({"q": query, "count": limit})
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_FETCH_BYTES).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return []
    hits: List[Hit] = []
    for match in BING_RE.finditer(body):
        link = _unwrap_bing(html.unescape(match.group("href")))
        if not link.startswith("http") or any(h.url == link for h in hits):
            continue
        snippet = BING_SNIPPET.search(match.group("rest"))
        hits.append(Hit(title=_strip(match.group("title"))[:200], url=link,
                        snippet=_strip(snippet.group("text"))[:400] if snippet else "",
                        rank=len(hits)))
        if len(hits) >= limit:
            break
    return hits


def _unwrap_bing(url: str) -> str:
    """Bing wraps results in a click-tracker whose ``u`` is base64 of the target."""
    if "bing.com/ck/" not in url:
        return url
    import base64
    u = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("u", [""])[0]
    if u.startswith("a1"):
        u = u[2:]
    try:
        return base64.urlsafe_b64decode(u + "=" * (-len(u) % 4)).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def snippet_sections(terms: Sequence[str], hits: Sequence[Hit]) -> List[Section]:
    """The result snippets themselves, as sections Jev can keep or drop."""
    lines = [f"{h.title} ({h.domain}): {h.snippet}" for h in hits if h.snippet]
    sections: List[Section] = []
    chunk: List[str] = []
    size = 0
    for line in lines:
        if chunk and size + len(line) > SECTION_MAX:
            sections.append(chunk)
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        sections.append(chunk)
    where = "https://duckduckgo.com/?" + urllib.parse.urlencode({"q": terms[0] if terms else ""})
    return [Section(text="\n".join(c), url=where, title="Search results · " + " / ".join(terms), index=i)
            for i, c in enumerate(sections)]


def fetch(url: str, *, timeout: float = FETCH_TIMEOUT) -> Page:
    """Fetch one page as readable text. Never raises."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return Page(url, "", "", ok=False, error="unsupported scheme")
    if not parsed.hostname or not _is_public(parsed.hostname):
        return Page(url, "", "", ok=False, error="private or unresolvable host")
    request = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "text/html,text/plain,application/json"}
    )
    try:
        with safe_opener().open(request, timeout=timeout) as response:
            final = urllib.parse.urlparse(response.geturl())
            if final.hostname and not _is_public(final.hostname):
                return Page(url, "", "", ok=False, error="redirected to a private host")
            raw = response.read(MAX_FETCH_BYTES)
            charset = response.headers.get_content_charset() or "utf-8"
    except Exception as exc:  # noqa: BLE001
        return Page(url, "", "", ok=False, error=f"{type(exc).__name__}")
    body = raw.decode(charset, errors="replace")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    title = _strip(title_match.group(1))[:200] if title_match else parsed.netloc
    text = ANY_TAG.sub("\n", TAGS.sub(" ", body))
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    page = Page(url=url, title=title, text=text)
    page.sections = sectionize(page)
    return page


def fetch_all(urls: Sequence[str], *, workers: int = 4) -> List[Page]:
    """Fetch in parallel; latency is wall-clock, not the sum."""
    if not urls:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(urls))) as pool:
        return list(pool.map(fetch, urls))


def sectionize(page: Page, *, limit: int = 24) -> List[Section]:
    """Cut a page into pieces small enough for one yes/no each."""
    sections: List[Section] = []
    buffer: List[str] = []
    size = 0

    def flush() -> None:
        nonlocal buffer, size
        text = "\n".join(buffer).strip()
        if len(text) >= SECTION_MIN:
            sections.append(Section(text=text, url=page.url, title=page.title,
                                    index=len(sections)))
        buffer, size = [], 0

    for paragraph in page.text.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if size + len(paragraph) > SECTION_MAX and buffer:
            flush()
            if len(sections) >= limit:
                return sections
        buffer.append(paragraph)
        size += len(paragraph) + 1
    flush()
    return sections[:limit]
