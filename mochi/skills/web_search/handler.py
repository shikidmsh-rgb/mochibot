"""Web search skill — DuckDuckGo via ddgs (no API key needed).

Uses the `ddgs` package which handles TLS fingerprint impersonation
to avoid bot-detection challenges from DuckDuckGo.
"""

import asyncio
import logging
import time
from collections import OrderedDict

from ddgs import DDGS

from mochi.skills.base import Skill, SkillContext, SkillResult

log = logging.getLogger(__name__)

_MAX_QUERY_LEN = 500
_DEFAULT_TIMEOUT_S = 20
_DEFAULT_MAX_RESULTS = 5
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
# Search via ddgs
# ---------------------------------------------------------------------------

def _ddg_search_sync(query: str, max_results: int = 5, timeout_s: int = 20) -> str:
    """Synchronous DuckDuckGo search using ddgs. Meant to run in a thread."""
    with DDGS(timeout=timeout_s) as ddgs:
        results = ddgs.text(query, max_results=max_results)

    if not results:
        return "[0 results]"

    parts: list[str] = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        url = r.get("href", "")
        snippet = (r.get("body") or "")[:200]
        parts.append(f"{i}. {title}\n   {url}\n   {snippet}")

    return "\n\n".join(parts)


async def _ddg_search(query: str, max_results: int = 5, timeout_s: int = 20) -> str:
    """Async wrapper — runs ddgs in a thread to avoid blocking the event loop."""
    cache_key = f"{query}|{max_results}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    output = await asyncio.to_thread(_ddg_search_sync, query, max_results, timeout_s)
    _cache.put(cache_key, output)
    return output


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
            result = await _ddg_search(query, max_results=max_results, timeout_s=_DEFAULT_TIMEOUT_S)
            return SkillResult(output=result)
        except Exception as e:
            log.error("Web search failed: %s", e)
            return SkillResult(output=f"Search error: {e}", success=False)
