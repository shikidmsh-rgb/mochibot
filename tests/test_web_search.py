"""Search provider selection, stored credentials, and bounded failures."""

import asyncio
from datetime import datetime
import json
from pathlib import Path

import httpx
import pytest

from mochi.admin import admin_crypto
from mochi.db import get_skill_config, set_skill_config
from mochi.skills.base import SkillContext, _parse_skill_md
from mochi.skills.web_search import handler
from mochi.tool_execution import model_result_for


@pytest.fixture(autouse=True)
def isolated_search(monkeypatch):
    monkeypatch.setattr(handler, "_cache", handler._TtlCache())
    monkeypatch.delenv("SKILL_WEB_SEARCH_BAIDU_API_KEY", raising=False)
    monkeypatch.delenv("BAIDU_API_KEY", raising=False)
    monkeypatch.delenv("SKILL_WEB_SEARCH_TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)


def _mock_http(monkeypatch, respond):
    client_class = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        handler.httpx, "AsyncClient",
        lambda **kwargs: client_class(transport=transport, **kwargs),
    )


def _context(**args):
    return SkillContext(
        trigger="tool_call", tool_name="web_search",
        args={"query": "Python documentation", **args},
    )


def _bing_html():
    return (
        b'<li class="b_algo"><h2><a href="https://docs.python.org/">'
        b'Python <strong>documentation</strong></a></h2>'
        b'<p>Official &amp; current</p></li>'
    )


@pytest.mark.asyncio
async def test_encrypted_keys_route_search_and_preserve_provenance(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "search-regression-test-token")
    monkeypatch.setattr(admin_crypto, "_fernet_instance", None)
    key = "search-test-credential"
    encrypted = admin_crypto.encrypt_api_key(key)
    assert encrypted != key
    set_skill_config("web_search", "BAIDU_API_KEY", encrypted)

    skill = handler.WebSearchSkill()
    skill._populate_from_md(_parse_skill_md(
        str(Path(handler.__file__).with_name("SKILL.md")),
    ))
    skill.refresh_config()
    assert skill.config["BAIDU_API_KEY"] == key
    assert skill.requires_config == []
    field = next(f for f in skill._config_schema_typed if f.key == "BAIDU_API_KEY")
    assert field.secret

    requests = []

    def respond(request):
        requests.append(request)
        if request.url.host == "api.tavily.com":
            assert str(request.url) == handler._TAVILY_SEARCH_URL
            assert request.headers["Authorization"] == f"Bearer {key}"
            payload = json.loads(request.content)
            assert payload["query"] == "Python documentation"
            assert payload["max_results"] == 3
            assert payload["search_depth"] == "basic"
            assert payload["include_answer"] is False
            assert payload["include_raw_content"] is False
            return httpx.Response(200, json={
                "answer": "PROVIDER_ANSWER_NOT_EVIDENCE",
                "results": [{
                    "title": "Python docs", "url": "https://docs.python.org/",
                    "content": "Official docs", "published_date": "2026-09-22",
                    "raw_content": "RAW_CONTENT_NOT_REQUESTED",
                }],
            })
        assert str(request.url) == handler._BAIDU_SEARCH_URL
        assert request.headers["Authorization"] == f"Bearer {key}"
        payload = json.loads(request.content)
        assert payload["search_source"] == "baidu_search_v2"
        assert payload["resource_type_filter"] == [{"type": "web", "top_k": 3}]
        assert payload["search_recency_filter"] == "week"
        return httpx.Response(200, json={"references": [{
            "title": "Python docs", "url": "https://docs.python.org/",
            "snippet": "Official docs", "website": "Python", "date": "2026-09-22",
        }]})

    _mock_http(monkeypatch, respond)
    result = await skill.execute(_context(max_results=3, recency="week"))
    assert result.success
    assert "https://docs.python.org/" in result.output
    assert "2026-09-22" in result.output
    assert model_result_for(result).find('"authority":"untrusted_data"') >= 0
    assert key not in result.output
    assert len(requests) == 1
    assert get_skill_config("web_search")["BAIDU_API_KEY"] == encrypted
    assert (await skill.execute(_context(max_results=3, recency="week"))).success
    assert len(requests) == 1

    set_skill_config("web_search", "TAVILY_API_KEY", encrypted)
    skill.refresh_config()
    assert skill.config["TAVILY_API_KEY"] == key
    assert next(
        f for f in skill._config_schema_typed if f.key == "TAVILY_API_KEY"
    ).secret
    result = await skill.execute(_context(max_results=3, recency="week"))
    assert result.success and len(requests) == 2
    assert json.loads(requests[-1].content)["time_range"] == "week"
    assert "https://docs.python.org/" in result.output
    assert "2026-09-22" in result.output
    assert "PROVIDER_ANSWER" not in result.output and "RAW_CONTENT" not in result.output
    assert key not in result.output
    assert '"authority":"untrusted_data"' in model_result_for(result)
    assert get_skill_config("web_search")["TAVILY_API_KEY"] == encrypted
    assert (await skill.execute(_context(max_results=3, recency="week"))).success
    assert len(requests) == 2

    for recency in ("", "month", "year"):
        result = await skill.execute(_context(max_results=3, recency=recency))
        assert result.success
        payload = json.loads(requests[-1].content)
        assert payload.get("time_range", "") == recency
        assert "start_date" not in payload

    for current, expected in (
        ("2026-08-31", "2026-02-28"),
        ("2024-08-31", "2024-02-29"),
        ("2026-01-31", "2025-07-31"),
    ):
        class FixedDateTime:
            @staticmethod
            def now(tz):
                return datetime.fromisoformat(current).replace(tzinfo=tz)

        monkeypatch.setattr(handler, "datetime", FixedDateTime)
        before = len(requests)
        result = await skill.execute(_context(max_results=3, recency="semiyear"))
        assert result.success and len(requests) == before + 1
        payload = json.loads(requests[-1].content)
        assert payload["start_date"] == expected
        assert "time_range" not in payload


@pytest.mark.asyncio
async def test_keyless_bing_handles_redirect_and_discloses_recency(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.params["q"] == "Python documentation"
        assert request.url.params["ensearch"] == "1"
        if request.url.host == "www.bing.com":
            return httpx.Response(
                302, headers={"Location": str(request.url.copy_with(host="cn.bing.com"))},
            )
        assert request.url.host == "cn.bing.com"
        return httpx.Response(200, content=_bing_html())

    _mock_http(monkeypatch, respond)
    result = await handler.WebSearchSkill().execute(_context(recency="week"))
    assert result.success and len(requests) == 2
    assert "does not support the requested recency" in result.output
    assert "Python documentation" in result.output
    assert "Official & current" in result.output


@pytest.mark.asyncio
async def test_baidu_failure_uses_bing_with_an_explicit_notice(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request.url.host)
        if request.url.host == "qianfan.baidubce.com":
            return httpx.Response(429, json={"message": "not returned to Main"})
        return httpx.Response(200, content=_bing_html())

    _mock_http(monkeypatch, respond)
    skill = handler.WebSearchSkill()
    skill.config = {"BAIDU_API_KEY": "test-key"}
    result = await skill.execute(_context(recency="month"))
    assert result.success
    assert requests == ["qianfan.baidubce.com", "www.bing.com"]
    assert "quota is unavailable" in result.output
    assert "Bing fallback" in result.output
    assert "recency filter was not enforced" in result.output


@pytest.mark.asyncio
async def test_failed_or_empty_providers_are_not_cached_as_success(monkeypatch):
    tavily_response = httpx.Response(200, json={"results": []})
    requested = []

    def respond(request):
        requested.append(request.url.host)
        if request.url.host == "api.tavily.com":
            return tavily_response
        if request.url.host == "qianfan.baidubce.com":
            return httpx.Response(200, json={"references": []})
        return httpx.Response(200, content=b"<html>challenge, not results</html>")

    _mock_http(monkeypatch, respond)
    skill = handler.WebSearchSkill()
    skill.config = {"BAIDU_API_KEY": "test-key"}
    result = await skill.execute(_context())
    assert not result.success
    assert "Baidu search returned no web results" in result.output
    assert "Bing search returned no web results" in result.output
    assert "source" not in json.loads(model_result_for(result))
    assert not handler._cache._store

    skill.config["TAVILY_API_KEY"] = "tavily-test-key"
    for response, expected in (
        (httpx.Response(401), "API key was rejected"),
        (httpx.Response(429), "quota is unavailable"),
        (httpx.Response(500), "returned HTTP 500"),
        (httpx.Response(200, content=b"not JSON"), "returned invalid JSON"),
        (httpx.Response(200, json=[]), "returned an invalid response"),
        (httpx.Response(200, json={}), "did not contain results"),
        (httpx.Response(200, json={"results": {}}), "did not contain results"),
        (httpx.Response(200, json={"results": []}), "returned no web results"),
    ):
        tavily_response = response
        requested.clear()
        result = await skill.execute(_context())
        assert not result.success and expected in result.output
        assert "Tavily" in result.output and "tavily-test-key" not in result.output
        assert requested == ["api.tavily.com"]
        assert "source" not in json.loads(model_result_for(result))
        assert not handler._cache._store


@pytest.mark.asyncio
async def test_provider_deadline_cancels_slow_requests(monkeypatch):
    cancelled = []

    async def respond(request):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(request.url.host)
        return httpx.Response(200, content=_bing_html())

    _mock_http(monkeypatch, respond)
    monkeypatch.setattr(handler, "_SEARCH_TIMEOUT_S", 0.01)
    skill = handler.WebSearchSkill()
    skill.config = {"BAIDU_API_KEY": "test-key"}
    result = await asyncio.wait_for(skill.execute(_context()), timeout=1)
    assert not result.success
    assert "timed out" in result.output
    assert cancelled == ["qianfan.baidubce.com", "www.bing.com"]
    assert not handler._cache._store

    skill.config["TAVILY_API_KEY"] = "test-key"
    cancelled.clear()
    result = await asyncio.wait_for(skill.execute(_context()), timeout=1)
    assert not result.success and "Tavily search timed out" in result.output
    assert cancelled == ["api.tavily.com"]
    assert not handler._cache._store


@pytest.mark.asyncio
async def test_oversized_responses_fail_without_cache(monkeypatch):
    _mock_http(
        monkeypatch, lambda request: httpx.Response(200, content=b"x" * 33),
    )
    monkeypatch.setattr(handler, "_SEARCH_MAX_RESPONSE_BYTES", 32)
    skill = handler.WebSearchSkill()
    skill.config = {"BAIDU_API_KEY": "test-key"}
    result = await skill.execute(_context())
    assert not result.success
    assert "larger than" in result.output
    assert not handler._cache._store

    skill.config["TAVILY_API_KEY"] = "test-key"
    result = await skill.execute(_context())
    assert not result.success and "Tavily search response is larger than" in result.output
    assert not handler._cache._store
