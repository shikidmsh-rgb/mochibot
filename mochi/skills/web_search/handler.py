"""Web search skill using bounded Bing HTML requests."""

import asyncio
import logging
import re
import time
from collections import OrderedDict
from html.parser import HTMLParser

import httpx

from mochi.skills.base import Skill, SkillContext, SkillResult

log = logging.getLogger(__name__)

_MAX_QUERY_LEN = 500
_DEFAULT_MAX_RESULTS = 5
_SEARCH_TIMEOUT_S = 10
_SEARCH_MAX_RESPONSE_BYTES = 1024 * 1024
_BING_SEARCH_URL = "https://www.bing.com/search"
_CACHE_TTL_S = 300
_CACHE_SIZE = 256


# ---------------------------------------------------------------------------
# TTL-bounded LRU cache
# ---------------------------------------------------------------------------

class _TtlCache:
    """Simple TTL + size-bounded LRU cache."""

    def __init__(self, max_size: int = 256, ttl_s: int = 300):
        self._max_size = max_size
        self._ttl_s = ttl_s
        self._store: OrderedDict[str, tuple[float, str]] = OrderedDict()

    def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        inserted_at, value = entry
        if time.monotonic() - inserted_at > self._ttl_s:
            del self._store[key]
            return None
        self._store.move_to_end(key)
        return value

    def put(self, key: str, value: str) -> None:
        if key in self._store:
            del self._store[key]
        self._store[key] = (time.monotonic(), value)
        while len(self._store) > self._max_size:
            self._store.popitem(last=False)


_cache = _TtlCache(max_size=_CACHE_SIZE, ttl_s=_CACHE_TTL_S)


# ---------------------------------------------------------------------------
# Search via Bing HTML
# ---------------------------------------------------------------------------

_ASCII_LETTER_RE = re.compile(r"[A-Za-z]")
def _uses_english_search(query: str) -> bool:
    return bool(_ASCII_LETTER_RE.search(query)) and all(
        not char.isalpha() or char.isascii()
        for char in query
    )


def _bing_request_options(
    query: str,
    max_results: int,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    english = _uses_english_search(query)
    headers = {
        "Accept-Language": "en-US,en;q=0.9" if english else "zh-CN,zh;q=0.9",
        "User-Agent": (
            "Mozilla/5.0"
            if english
            else (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            )
        ),
    }
    params = {"q": query, "count": str(max_results)}
    cookies: dict[str, str] = {}
    if english:
        params["ensearch"] = "1"
        cookies["SRCHHPGUSR"] = "SRCHLANG=EN"
    return headers, params, cookies


def _single_line(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", "".join(parts)).strip()


class _BingSearchParser(HTMLParser):
    """Extract organic results from Bing result cards."""

    def __init__(self, max_results: int) -> None:
        super().__init__(convert_charrefs=True)
        self.max_results = max_results
        self.results: list[tuple[str, str, str]] = []
        self._result_depth = 0
        self._h2_depth = 0
        self._in_title_link = False
        self._in_snippet = False
        self._snippet_finished = False
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []
        self._href = ""

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attrs_by_name = dict(attrs)
        if tag == "li":
            classes = (attrs_by_name.get("class") or "").split()
            if self._result_depth:
                self._result_depth += 1
            elif "b_algo" in classes and len(self.results) < self.max_results:
                self._result_depth = 1
            return
        if not self._result_depth:
            return
        if tag == "h2":
            self._h2_depth += 1
        elif tag == "a" and self._h2_depth and not self._in_title_link:
            self._in_title_link = True
            self._href = attrs_by_name.get("href") or ""
        elif tag == "p" and not self._snippet_finished:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if not self._result_depth:
            return
        if tag == "a" and self._in_title_link:
            self._in_title_link = False
        elif tag == "h2" and self._h2_depth:
            self._h2_depth -= 1
        elif tag == "p" and self._in_snippet:
            self._in_snippet = False
            self._snippet_finished = True
        elif tag == "li":
            self._result_depth -= 1
            if not self._result_depth:
                self._finish_result()

    def handle_data(self, data: str) -> None:
        if self._in_title_link:
            self._title_parts.append(data)
        if self._in_snippet:
            self._snippet_parts.append(data)

    def _finish_result(self) -> None:
        title = _single_line(self._title_parts)
        href = self._href.strip()
        snippet = _single_line(self._snippet_parts)[:200]
        if title and href:
            self.results.append((title, href, snippet))
        self._h2_depth = 0
        self._in_title_link = False
        self._in_snippet = False
        self._snippet_finished = False
        self._title_parts = []
        self._snippet_parts = []
        self._href = ""


def _extract_bing_results(
    body: bytes,
    encoding: str | None,
    max_results: int,
) -> str:
    try:
        html = body.decode(encoding or "utf-8", errors="replace")
    except LookupError:
        html = body.decode("utf-8", errors="replace")

    parser = _BingSearchParser(max_results)
    parser.feed(html)
    parser.close()
    if not parser.results:
        return "[0 results]"
    return "\n\n".join(
        f"{index}. {title}\n   {href}\n   {snippet}"
        for index, (title, href, snippet) in enumerate(parser.results, 1)
    )


async def _bing_search(query: str, max_results: int = 5) -> str:
    cache_key = f"{query}|{max_results}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        async with asyncio.timeout(_SEARCH_TIMEOUT_S):
            output = await _bing_search_within_deadline(query, max_results)
    except TimeoutError as exc:
        raise ValueError(
            f"Search timed out after {_SEARCH_TIMEOUT_S} seconds."
        ) from exc
    _cache.put(cache_key, output)
    return output


async def _bing_search_within_deadline(query: str, max_results: int) -> str:
    headers, params, cookies = _bing_request_options(query, max_results)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(_SEARCH_TIMEOUT_S),
        headers=headers,
        cookies=cookies,
        follow_redirects=True,
    ) as client:
        async with client.stream(
            "GET",
            _BING_SEARCH_URL,
            params=params,
        ) as response:
            response.raise_for_status()
            declared_size = response.headers.get("content-length")
            if declared_size:
                try:
                    too_large = int(declared_size) > _SEARCH_MAX_RESPONSE_BYTES
                except ValueError:
                    too_large = False
                if too_large:
                    raise ValueError("Search response is larger than the 1 MB limit.")

            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > _SEARCH_MAX_RESPONSE_BYTES:
                    raise ValueError("Search response is larger than the 1 MB limit.")
                chunks.append(chunk)
            body = b"".join(chunks)
            encoding = response.charset_encoding

    return await asyncio.to_thread(
        _extract_bing_results,
        body,
        encoding,
        max_results,
    )


# ---------------------------------------------------------------------------
# Skill handler
# ---------------------------------------------------------------------------

class WebSearchSkill(Skill):
    async def execute(self, context: SkillContext) -> SkillResult:
        if context.tool_name != "web_search":
            return SkillResult(output=f"Unknown tool: {context.tool_name}", success=False)

        query = (context.args.get("query") or "").strip()
        if not query:
            return SkillResult(output="Search query is empty.", success=False)
        if len(query) > _MAX_QUERY_LEN:
            return SkillResult(
                output=f"Query too long ({len(query)} chars, max {_MAX_QUERY_LEN}).",
                success=False,
            )

        max_results = context.args.get("max_results", _DEFAULT_MAX_RESULTS)
        max_results = max(1, min(10, int(max_results)))

        try:
            result = await _bing_search(query, max_results=max_results)
            return SkillResult(output=result, content_source="external_web")
        except (httpx.HTTPError, ValueError, OSError) as exc:
            log.error("Web search failed: %s", exc)
            return SkillResult(output=f"Search error: {exc}", success=False)
