"""Tests for web_search skill — ddgs-based handler."""

import pytest
from unittest.mock import patch, MagicMock

from mochi.skills.web_search.handler import (
    _TtlCache,
    _MAX_QUERY_LEN,
    _ddg_search_sync,
    WebSearchSkill,
)
from mochi.skills.base import SkillContext


# ---------------------------------------------------------------------------
# Cache tests
# ---------------------------------------------------------------------------

class TestTtlCache:
    def test_put_and_get(self):
        cache = _TtlCache(max_size=10, ttl_s=60)
        cache.put("key", "value")
        assert cache.get("key") == "value"

    def test_miss(self):
        cache = _TtlCache(max_size=10, ttl_s=60)
        assert cache.get("missing") is None

    def test_eviction(self):
        cache = _TtlCache(max_size=2, ttl_s=60)
        cache.put("a", "1")
        cache.put("b", "2")
        cache.put("c", "3")  # should evict "a"
        assert cache.get("a") is None
        assert cache.get("b") == "2"
        assert cache.get("c") == "3"

    def test_ttl_expiry(self):
        cache = _TtlCache(max_size=10, ttl_s=1)
        cache.put("key", "value")
        # Manually backdate the entry past TTL
        original_time = cache._store["key"][0]
        cache._store["key"] = (original_time - 2, "value")
        assert cache.get("key") is None


# ---------------------------------------------------------------------------
# Sync search function tests (with mocked ddgs)
# ---------------------------------------------------------------------------

def _make_ddgs_mock(results):
    """Create a mock DDGS context manager returning given results."""
    mock_instance = MagicMock()
    mock_instance.__enter__ = MagicMock(return_value=mock_instance)
    mock_instance.__exit__ = MagicMock(return_value=False)
    mock_instance.text.return_value = results
    return mock_instance


class TestDdgSearchSync:
    @patch("mochi.skills.web_search.handler.DDGS")
    def test_returns_formatted_results(self, MockDDGS):
        fake_results = [
            {"title": "Example", "href": "https://example.com", "body": "A snippet"},
            {"title": "Other", "href": "https://other.com", "body": "Another snippet"},
        ]
        MockDDGS.return_value = _make_ddgs_mock(fake_results)

        result = _ddg_search_sync("test query", max_results=2)
        assert "1. Example" in result
        assert "https://example.com" in result
        assert "2. Other" in result

    @patch("mochi.skills.web_search.handler.DDGS")
    def test_empty_results(self, MockDDGS):
        MockDDGS.return_value = _make_ddgs_mock([])
        result = _ddg_search_sync("nothing", max_results=5)
        assert result == "[0 results]"

    @patch("mochi.skills.web_search.handler.DDGS")
    def test_snippet_truncated(self, MockDDGS):
        long_body = "x" * 500
        MockDDGS.return_value = _make_ddgs_mock([
            {"title": "T", "href": "https://t.com", "body": long_body},
        ])
        result = _ddg_search_sync("test", max_results=1)
        # Snippet should be truncated to 200 chars
        lines = result.split("\n")
        snippet_line = lines[2].strip()  # third line is the snippet
        assert len(snippet_line) <= 200


# ---------------------------------------------------------------------------
# Skill handler tests
# ---------------------------------------------------------------------------

class TestWebSearchSkill:
    @pytest.mark.asyncio
    async def test_empty_query(self):
        skill = WebSearchSkill()
        ctx = SkillContext(trigger="tool_call", tool_name="web_search", args={"query": ""})
        result = await skill.execute(ctx)
        assert not result.success
        assert "empty" in result.output.lower()

    @pytest.mark.asyncio
    async def test_query_too_long(self):
        skill = WebSearchSkill()
        ctx = SkillContext(
            trigger="tool_call",
            tool_name="web_search",
            args={"query": "x" * (_MAX_QUERY_LEN + 1)},
        )
        result = await skill.execute(ctx)
        assert not result.success
        assert "too long" in result.output.lower()

    @pytest.mark.asyncio
    async def test_unknown_tool(self):
        skill = WebSearchSkill()
        ctx = SkillContext(trigger="tool_call", tool_name="wrong_tool", args={})
        result = await skill.execute(ctx)
        assert not result.success
        assert "Unknown tool" in result.output

    @pytest.mark.asyncio
    async def test_successful_search(self):
        skill = WebSearchSkill()
        ctx = SkillContext(
            trigger="tool_call",
            tool_name="web_search",
            args={"query": "python tutorial"},
        )
        fake_output = "1. Python Tutorial\n   https://python.org\n   Learn Python"
        with patch("mochi.skills.web_search.handler._ddg_search", return_value=fake_output):
            result = await skill.execute(ctx)
        assert result.success
        assert "Python Tutorial" in result.output

    @pytest.mark.asyncio
    async def test_search_error(self):
        skill = WebSearchSkill()
        ctx = SkillContext(
            trigger="tool_call",
            tool_name="web_search",
            args={"query": "test"},
        )
        with patch("mochi.skills.web_search.handler._ddg_search", side_effect=RuntimeError("network down")):
            result = await skill.execute(ctx)
        assert not result.success
        assert "Search error" in result.output

    @pytest.mark.asyncio
    async def test_max_results_clamped(self):
        skill = WebSearchSkill()
        ctx = SkillContext(
            trigger="tool_call",
            tool_name="web_search",
            args={"query": "test", "max_results": 99},
        )
        with patch("mochi.skills.web_search.handler._ddg_search", return_value="ok") as mock_search:
            await skill.execute(ctx)
            # max_results should be clamped to 10
            _, kwargs = mock_search.call_args
            assert kwargs["max_results"] == 10
