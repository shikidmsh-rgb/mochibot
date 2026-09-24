"""Image generation is available only during an authorized WeChat chat turn."""

import json

import pytest

from mochi.ai_client import chat
from mochi.db import get_recent_messages, get_recent_tool_executions, set_skill_enabled
from mochi.image_service import clear_image_config, save_image_config
from mochi.llm import AnthropicProvider
from mochi.transport import DeliveryError, ImageAttachment, IncomingMessage
from tests.e2e.mock_llm import make_response, make_tool_call


IMAGE_TOOL = "generate_and_send_image"
IMAGE = ImageAttachment.from_bytes(b"\x89PNG\r\n\x1a\npicture")


@pytest.fixture
def configured_image(monkeypatch):
    from mochi.admin import admin_crypto

    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setattr(admin_crypto, "_fernet_instance", None)
    save_image_config("auto", "", "image-model", "private-image-key")


def _message(*, transport="wechat", authorized=True, sender=None):
    return IncomingMessage(
        user_id=1, channel_id=1, text="画一张月亮",
        transport=transport, owner_authorized=authorized, send_image=sender,
    )


def _tools(request):
    return {
        tool["function"]["name"]
        for tool in request["tools"] or []
    }


@pytest.mark.asyncio
async def test_generated_image_is_delivered_then_shown_to_main_without_persisting_bytes(
    configured_image, mock_llm_factory, monkeypatch,
):
    import mochi.config as config
    import mochi.skills.image_generation.handler as image_skill

    monkeypatch.setattr(config, "TOOL_ESCALATION_ENABLED", True)
    monkeypatch.setattr(config, "TOOL_LOOP_MAX_ROUNDS", 2)
    generated = []

    async def generate(prompt):
        generated.append(prompt)
        return IMAGE

    delivered = []

    async def send(image):
        delivered.append(image)

    monkeypatch.setattr(image_skill, "generate_image", generate)
    mock = mock_llm_factory([
        make_response(tool_calls=[make_tool_call(
            "request_tools", {"skills": ["image_generation"]},
        )]),
        make_response(tool_calls=[make_tool_call(IMAGE_TOOL, {"prompt": "月亮和雪山"})]),
        make_response("我看到雪山和月亮了。"),
    ])
    reply = await chat(_message(sender=send))

    assert reply.text == "我看到雪山和月亮了。"
    assert generated == ["月亮和雪山"]
    assert delivered == [IMAGE]
    assert "你有图片生成能力，可随时按需调用。" in mock.call_log[0]["messages"][0]["content"]
    assert IMAGE_TOOL not in _tools(mock.call_log[0])
    assert IMAGE_TOOL in _tools(mock.call_log[1])
    assert mock.call_log[2]["tools"] is None
    messages = mock.call_log[2]["messages"]
    assert json.loads(messages[-2]["content"]) == {
        "ok": True, "changed": True, "result": "图片已发送。",
    }
    assert messages[-1] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "这是你刚通过工具生成的图片，不是用户发来的新消息。"},
            {"type": "image_url", "image_url": {
                "url": IMAGE.data_url(), "detail": "auto",
            }},
        ],
    }
    converted = AnthropicProvider._convert_messages(messages[1:])
    assert converted[-2]["content"][0]["type"] == "tool_result"
    assert converted[-1]["content"][1]["type"] == "image"
    assert IMAGE.data_url() not in json.dumps(get_recent_messages(1))
    assert get_recent_tool_executions(1)[0]["status"] == "success"


@pytest.mark.asyncio
async def test_unconfirmed_delivery_is_reported_without_repeating_generation(
    configured_image, mock_llm_factory, monkeypatch,
):
    import mochi.config as config
    import mochi.skills.image_generation.handler as image_skill

    monkeypatch.setattr(config, "TOOL_ESCALATION_ENABLED", True)
    generated = []

    async def generate(prompt):
        generated.append(prompt)
        return IMAGE

    async def send(image):
        raise DeliveryError("uncertain", outcome="delivery_unknown")

    monkeypatch.setattr(image_skill, "generate_image", generate)
    mock = mock_llm_factory([
        make_response(tool_calls=[make_tool_call(
            "request_tools", {"skills": ["image_generation"]},
        )]),
        make_response(tool_calls=[make_tool_call(IMAGE_TOOL, {"prompt": "月亮"})]),
        make_response("我看到了画面，但发送状态没确认。"),
    ])
    await chat(_message(sender=send))

    result = json.loads(mock.call_log[-1]["messages"][-2]["content"])
    assert result["ok"] is False
    assert result["code"] == "delivery_unknown"
    assert result["message"] == "图片已生成，但发送未确认；未自动重试。"
    assert mock.call_log[-1]["messages"][-1]["content"][1]["image_url"]["url"] == IMAGE.data_url()
    assert generated == ["月亮"]
    assert get_recent_tool_executions(
        1, include_failures=True, state_changes_only=False,
    )[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_router_can_offer_image_tool_without_forcing_its_use(
    configured_image, mock_llm_factory, monkeypatch,
):
    import mochi.config as config
    import mochi.skills.image_generation.handler as image_skill
    import mochi.tool_router as router
    from mochi.admin import admin_db

    monkeypatch.setattr(config, "TOOL_ROUTER_ENABLED", True)
    monkeypatch.setattr(admin_db, "list_tier_assignments", lambda: {"lite": "test"})

    async def classify(text, **kwargs):
        assert "image_generation" in kwargs["catalog"]
        return ["image_generation"]

    async def generate(prompt):
        return IMAGE

    delivered = []

    async def send(image):
        delivered.append(image)

    monkeypatch.setattr(router, "classify_skills", classify)
    monkeypatch.setattr(image_skill, "generate_image", generate)
    mock = mock_llm_factory([
        make_response(tool_calls=[make_tool_call(IMAGE_TOOL, {"prompt": "月亮"})]),
        make_response("我看见月亮。"),
    ])
    reply = await chat(_message(sender=send))

    assert IMAGE_TOOL in _tools(mock.call_log[0])
    assert reply.text == "我看见月亮。"
    assert delivered == [IMAGE]


@pytest.mark.asyncio
async def test_runtime_tool_dispatch_cannot_generate_outside_owner_chat(
    configured_image, monkeypatch,
):
    import mochi.skills as skills
    import mochi.skills.image_generation.handler as image_skill

    async def generate(prompt):
        raise AssertionError("ineligible dispatch must not generate")

    monkeypatch.setattr(image_skill, "generate_image", generate)
    for source, transport, owner_authorized in [
        ("runtime:free_time", "wechat", True),
        ("chat", "telegram", True),
        ("chat", "wechat", False),
    ]:
        result = await skills.dispatch(
            IMAGE_TOOL, {"prompt": "moon"}, user_id=1,
            transport=transport, source=source, actor="main",
            owner_authorized=owner_authorized,
        )
        assert not result.success
        assert result.image is None


@pytest.mark.asyncio
async def test_image_capability_tracks_config_toggle_and_chat_context(
    configured_image, mock_llm_factory, monkeypatch,
):
    import mochi.config as config
    import mochi.skills.image_generation.handler as image_skill

    monkeypatch.setattr(config, "TOOL_ESCALATION_ENABLED", True)
    generated = []

    async def generate(prompt):
        generated.append(prompt)
        return IMAGE

    async def send(image):
        raise AssertionError("ineligible turn must not send")

    monkeypatch.setattr(image_skill, "generate_image", generate)

    async def check(message):
        mock = mock_llm_factory([
            make_response(tool_calls=[make_tool_call(
                "request_tools", {"skills": ["image_generation"]},
            )]),
            make_response("No image."),
        ])
        await chat(message)
        assert "你有图片生成能力" not in mock.call_log[0]["messages"][0]["content"]
        assert "生成并发送图片" not in mock.call_log[0]["messages"][0]["content"]
        assert IMAGE_TOOL not in _tools(mock.call_log[0])
        assert IMAGE_TOOL not in _tools(mock.call_log[1])
        assert json.loads(mock.call_log[1]["messages"][-1]["content"])["unavailable"] == [
            {"request": "image_generation", "reason": "not_available_this_turn"},
        ]

    await check(_message(sender=None))
    await check(_message(authorized=False, sender=send))
    await check(_message(transport="telegram", sender=send))

    set_skill_enabled("image_generation", False)
    await check(_message(sender=send))
    set_skill_enabled("image_generation", True)
    clear_image_config()
    await check(_message(sender=send))
    assert generated == []
