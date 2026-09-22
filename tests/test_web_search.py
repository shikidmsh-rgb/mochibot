"""Search provider selection, stored credentials, and bounded failures."""

import asyncio
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
async def test_encrypted_saved_key_routes_to_baidu_and_preserves_provenance(monkeypatch):
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
    def respond(request):
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


def test_plaintext_config_remains_compatible_and_local_queries_keep_their_language():
    from mochi.skill_config_resolver import resolve_skill_config
    from mochi.skills.base import ConfigField

    set_skill_config("example", "SECRET", "legacy-plaintext")
    set_skill_config("example", "LABEL", "gAAAAA-not-a-secret")
    assert resolve_skill_config("example", [
        ConfigField("SECRET", "str", "", secret=True),
        ConfigField("LABEL", "str", ""),
    ]) == {"SECRET": "legacy-plaintext", "LABEL": "gAAAAA-not-a-secret"}
    headers, params, cookies = handler._bing_request_options("\u82cf\u5dde\u5929\u6c14", 5)
    assert headers["Accept-Language"].startswith("zh-CN")
    assert "ensearch" not in params
    assert cookies == {}
    with pytest.raises(ValueError, match="invalid response|references"):
        handler._format_baidu_results({}, 5)
