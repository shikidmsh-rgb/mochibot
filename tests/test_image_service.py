import base64
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mochi import image_service as images
from mochi.db import get_skill_config
from mochi.transport import ImageAttachment


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aCWQAAAAASUVORK5CYII="
)


@pytest.fixture
def image_config(monkeypatch):
    from mochi.admin import admin_crypto

    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setattr(admin_crypto, "_fernet_instance", None)
    images.save_image_config("auto", "", "image-model", "private-image-key")


@pytest.mark.parametrize(("protocol", "root", "expected"), [
    ("auto", "", "openai"),
    ("auto", "https://api.openai.com/v1", "openai"),
    ("auto", "https://generativelanguage.googleapis.com/v1beta", "gemini"),
    ("auto", "https://resource.services.ai.azure.com/openai/v1", "openai"),
    ("gemini", "https://api.example.com/v1beta", "gemini"),
    ("openai", "https://api.example.com/v1", "openai"),
])
def test_image_protocol_resolves_by_endpoint_not_model_name(protocol, root, expected):
    assert images.resolve_protocol(protocol, root)[0] == expected


def test_image_config_is_independent_encrypted_and_does_not_probe(image_config, monkeypatch):
    monkeypatch.setattr(images.aiohttp, "ClientSession", MagicMock())
    stored = get_skill_config("_image_generation")["config"]
    assert "private-image-key" not in stored
    assert images.get_image_config()["api_key_set"]
    assert "api_key" not in images.get_image_config()
    assert images.get_image_config(include_key=True)["api_key"] == "private-image-key"
    images.save_image_config("openai", "https://api.openai.com/v1", "new-model", "__KEEP__")
    assert images.get_image_config(include_key=True)["api_key"] == "private-image-key"
    before = get_skill_config("_image_generation")
    with pytest.raises(ValueError, match="重新填写"):
        images.save_image_config("openai", "https://different.example/v1", "m", "__KEEP__")
    monkeypatch.setattr(images, "encrypt_api_key", lambda value: value)
    with pytest.raises(ValueError, match="加密失败"):
        images.save_image_config("openai", "", "m", "new-key")
    assert get_skill_config("_image_generation") == before
    from mochi.admin.admin_db import list_tier_assignments
    assert list_tier_assignments() == {}
    images.clear_image_config()
    assert not images.get_image_config()["configured"]
    images.aiohttp.ClientSession.assert_not_called()


def test_image_config_rejects_ambiguous_and_wrong_api_roots():
    for protocol, root in [
        ("auto", "https://gateway.example/v1"),
        ("auto", "http://api.openai.com/v1"),
        ("openai", "https://api.example/v1?key=secret"),
        ("openai", "https://api.example/v1/images/generations"),
        ("auto", "https://generativelanguage.googleapis.com/v1beta/openai"),
    ]:
        with pytest.raises(ValueError):
            images.resolve_protocol(protocol, root)


def _http_session(monkeypatch, payload, *, download=PNG, status=200):
    @asynccontextmanager
    async def response(data, code):
        async def chunks(size):
            yield data
        yield SimpleNamespace(
            status=code, content=SimpleNamespace(iter_chunked=chunks),
        )

    session = SimpleNamespace(
        post=MagicMock(side_effect=lambda *a, **kw: response(json.dumps(payload).encode(), status)),
        get=MagicMock(side_effect=lambda *a, **kw: response(download, 200)),
    )

    @asynccontextmanager
    async def client(**kwargs):
        yield session

    monkeypatch.setattr(images.aiohttp, "ClientSession", client)
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["openai_inline", "openai_url", "gemini"])
async def test_image_generation_returns_real_bytes_without_leaking_credentials(image_config, monkeypatch, shape):
    encoded = base64.b64encode(PNG).decode()
    if shape == "gemini":
        images.save_image_config("gemini", "", "models/new-image-model", "gemini-key")
        payload = {"candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [
                {"thought": True, "inlineData": {"data": "ignored"}},
                {"inlineData": {"mimeType": "image/png", "data": encoded}},
            ]},
        }]}
    else:
        item = {"b64_json": encoded} if shape == "openai_inline" else {
            "url": "https://cdn.example/image?signature=private",
        }
        payload = {"data": [item]}
    session = _http_session(monkeypatch, payload)
    result = await images.generate_image("Owner supplied image description")
    assert result == ImageAttachment(PNG, "image/png")
    session.post.assert_called_once()
    assert session.post.call_args.kwargs["allow_redirects"] is False
    if shape == "openai_url":
        assert "headers" not in session.get.call_args.kwargs
        assert session.get.call_args.kwargs["allow_redirects"] is False
    else:
        session.get.assert_not_called()


@pytest.mark.asyncio
async def test_generation_fails_explicitly_without_retry(image_config, monkeypatch):
    session = _http_session(monkeypatch, {"error": {"message": "secret"}}, status=403)
    with pytest.raises(ValueError, match="HTTP 403") as denied:
        await images.generate_image("Draw")
    assert "secret" not in str(denied.value)
    session.post.assert_called_once()
    session = _http_session(monkeypatch, {"data": []})
    with pytest.raises(ValueError, match="未返回"):
        await images.generate_image("Draw")
    images.save_image_config("gemini", "", "image-model", "key")
    _http_session(monkeypatch, {"candidates": [{"finishReason": "MAX_TOKENS"}]})
    with pytest.raises(ValueError, match="不完整"):
        await images.generate_image("Draw")


@pytest.mark.asyncio
async def test_admin_only_configures_images_without_execution(image_config, monkeypatch):
    import httpx
    import mochi.config as config
    from mochi.admin import admin_server

    monkeypatch.setattr(config, "ADMIN_TOKEN", "test-admin-token")
    network = MagicMock()
    monkeypatch.setattr(images.aiohttp, "ClientSession", network)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=admin_server.app, client=("198.51.100.10", 123),
        ), base_url="http://test",
    ) as client:
        assert (await client.get("/api/images/config")).status_code == 401
        client.headers["Authorization"] = "Bearer test-admin-token"
        cfg = (await client.get("/api/images/config")).json()
        assert "api_key" not in cfg
        response = await client.put("/api/images/config", json={
            "protocol": "auto", "base_url": "", "model": "another-model", "api_key": "__KEEP__",
        })
        assert response.status_code == 200
        assert response.json()["model"] == "another-model"
        assert "api_key" not in response.json()
        assert (await client.delete("/api/images/config")).json()["ok"]
        assert not (await client.get("/api/images/config")).json()["configured"]
    network.assert_not_called()
    from mochi.db import get_recent_messages
    assert get_recent_messages(1) == []
