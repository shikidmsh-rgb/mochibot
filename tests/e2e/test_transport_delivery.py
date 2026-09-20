"""Reply-context recovery, truthful failures, and per-chunk delivery validity."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mochi.ai_client import ChatResult
from mochi.db import get_skill_config
from mochi.transport import DeliveryError
from mochi.transport.weixin import WeixinTransport


@pytest.mark.asyncio
async def test_reply_context_survives_restart_and_is_scoped(monkeypatch):
    import mochi.admin.admin_crypto as crypto
    import mochi.transport.weixin as weixin

    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-key")
    monkeypatch.setattr(crypto, "_fernet_instance", None)
    monkeypatch.setattr(weixin, "WEIXIN_BOT_TOKEN", "test-bot")
    monkeypatch.setattr(weixin, "WEIXIN_ALLOWED_USERS", [])
    first = WeixinTransport()
    first.restore_owner_id("owner")
    await first._handle_message({
        "from_user_id": "owner",
        "context_token": "test-reply-token",
        "item_list": [{"type": 2}],
    })
    stored = get_skill_config("_transport:wechat")["reply_context"]
    assert "test-reply-token" not in stored

    restarted = WeixinTransport()
    restarted.restore_owner_id("owner")
    restarted._session = object()
    api = AsyncMock(return_value={})
    monkeypatch.setattr(restarted, "_api_post", api)
    assert await restarted.send_chat_result_checked(1, ChatResult(text="hello"))
    assert api.call_args.args[1]["msg"]["context_token"] == "test-reply-token"

    other_owner = WeixinTransport()
    other_owner.restore_owner_id("someone-else")
    assert other_owner._context_tokens == {}
    monkeypatch.setattr(weixin, "WEIXIN_BOT_TOKEN", "different-bot")
    other_bot = WeixinTransport()
    other_bot.restore_owner_id("owner")
    assert other_bot._context_tokens == {}


@pytest.mark.asyncio
async def test_send_failures_preserve_reasons_and_invalidate_only_rejected_context(
    monkeypatch, caplog,
):
    import mochi.transport.weixin as weixin

    monkeypatch.setattr(weixin, "WEIXIN_BUBBLE_DELAY_S", 0)
    transport = WeixinTransport()
    transport.restore_owner_id("owner")
    transport._session = object()
    api = AsyncMock()
    monkeypatch.setattr(transport, "_api_post", api)

    with pytest.raises(DeliveryError, match="reply context missing") as missing:
        await transport.send_chat_result_checked(1, ChatResult(text="hello"))
    assert missing.value.outcome == "delivery_unavailable"
    api.assert_not_called()

    transport._remember_context_token("owner", "private-reply-token")
    api.return_value = {"ret": -1, "errcode": 42, "errmsg": "private-reply-token"}
    assert not await transport.send_chat_result(
        1, ChatResult(text="First bubble here.|||Second bubble here."),
    )
    assert api.await_count == 1
    assert "errcode=42" in caplog.text
    assert "private-reply-token" not in caplog.text
    assert transport._context_tokens["owner"] == "private-reply-token"

    api.side_effect = TimeoutError()
    with pytest.raises(DeliveryError) as timeout:
        await transport.send_chat_result_checked(1, ChatResult(text="hello"))
    assert timeout.value.outcome == "delivery_unknown"

    api.side_effect = None
    api.return_value = {"ret": -1, "http_status": 502}
    with pytest.raises(DeliveryError) as gateway:
        await transport.send_chat_result_checked(1, ChatResult(text="hello"))
    assert gateway.value.outcome == "delivery_unknown"

    async def late_rejection(*args):
        transport._remember_context_token("owner", "new-token")
        return {"ret": -1, "errcode": -14}

    api.side_effect = late_rejection
    with pytest.raises(DeliveryError):
        await transport.send_chat_result_checked(1, ChatResult(text="hello"))
    assert transport._context_tokens["owner"] == "new-token"

    api.side_effect = None
    api.return_value = {"ret": -1, "errcode": -14}
    with pytest.raises(DeliveryError) as expired:
        await transport.send_chat_result_checked(1, ChatResult(text="hello"))
    assert expired.value.outcome == "delivery_rejected"
    assert transport._context_tokens["owner"] == ""
    saved = json.loads(get_skill_config("_transport:wechat")["reply_context"])
    assert saved["token"] == ""
    restarted = WeixinTransport()
    restarted.restore_owner_id("owner")
    assert restarted._context_tokens == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["wechat", "telegram"])
async def test_autonomous_guard_stops_remaining_chunks_only(monkeypatch, name):
    import mochi.transport.weixin as weixin
    import mochi.transport.telegram as telegram

    active = True

    async def first_chunk(*args, **kwargs):
        nonlocal active
        active = False
        return {}

    sender = AsyncMock(side_effect=first_chunk)
    if name == "wechat":
        transport = WeixinTransport()
        transport.restore_owner_id("owner")
        transport._session = object()
        transport._remember_context_token("owner", "test-token")
        monkeypatch.setattr(weixin, "WEIXIN_MSG_LIMIT", 10)
        monkeypatch.setattr(transport, "_api_post", sender)
    else:
        transport = telegram.TelegramTransport()
        transport._app = SimpleNamespace(bot=SimpleNamespace(send_message=sender))
    text = "x" * 4200
    with pytest.raises(DeliveryError) as stopped:
        await transport.send_chat_result_checked(
            1, ChatResult(text=text), can_deliver=lambda: active,
        )
    assert stopped.value.outcome == "expired"
    assert sender.await_count == 1

    sender.reset_mock(side_effect=True)
    sender.return_value = {}
    assert await transport.send_chat_result(1, ChatResult(text=text))
    assert sender.await_count > 1


@pytest.mark.asyncio
@pytest.mark.parametrize("delimiter", ["|||", "\n\n"])
async def test_wechat_proactive_text_is_one_message_without_losing_paragraphs(
    monkeypatch, delimiter,
):
    import mochi.transport.weixin as weixin

    transport = WeixinTransport()
    transport.restore_owner_id("owner")
    transport._session = object()
    transport._remember_context_token("owner", "test-token")
    api = AsyncMock(return_value={})
    monkeypatch.setattr(transport, "_api_post", api)
    monkeypatch.setattr(weixin, "WEIXIN_BUBBLE_DELAY_S", 0)
    paragraphs = [f"Paragraph number {i}." for i in range(12)]
    result = ChatResult(text=delimiter.join(paragraphs))

    assert await transport.send_proactive_result_checked(1, result)
    api.assert_awaited_once()
    assert api.call_args.args[1]["msg"]["item_list"][0]["text_item"]["text"] == (
        "\n\n".join(paragraphs)
    )

    api.reset_mock()
    assert await transport.send_chat_result(
        1, ChatResult(text=delimiter.join(paragraphs[:2])),
    )
    assert api.await_count == 2


@pytest.mark.asyncio
async def test_wechat_proactive_length_chunks_preserve_content_and_deadline(monkeypatch):
    import mochi.transport.weixin as weixin

    transport = WeixinTransport()
    transport.restore_owner_id("owner")
    transport._session = object()
    transport._remember_context_token("owner", "test-token")
    monkeypatch.setattr(weixin, "WEIXIN_MSG_LIMIT", 20)
    api = AsyncMock(return_value={})
    monkeypatch.setattr(transport, "_api_post", api)
    text = "First paragraph here.|||Second paragraph here.\n\nLast paragraph here."
    assert await transport.send_proactive_result_checked(1, ChatResult(text=text))
    chunks = [
        call.args[1]["msg"]["item_list"][0]["text_item"]["text"]
        for call in api.call_args_list
    ]
    assert len(chunks) > 1
    assert all(len(chunk) <= 20 for chunk in chunks)
    assert "".join(chunks) == text.replace("|||", "\n\n")

    active = True

    async def first_chunk(*args, **kwargs):
        nonlocal active
        active = False
        return {}

    api.reset_mock()
    api.side_effect = first_chunk
    with pytest.raises(DeliveryError) as stopped:
        await transport.send_proactive_result_checked(
            1, ChatResult(text=text), can_deliver=lambda: active,
        )
    assert stopped.value.outcome == "expired"
    assert api.await_count == 1


@pytest.mark.asyncio
async def test_other_transports_keep_proactive_formatting(monkeypatch):
    from mochi.transport.telegram import TelegramTransport

    transport = TelegramTransport()
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(transport, "send_chat_result_checked", send)
    result = ChatResult(text="First bubble here.|||Second bubble here.")
    guard = lambda: True
    assert await transport.send_proactive_result_checked(1, result, can_deliver=guard)
    send.assert_awaited_once_with(1, result, can_deliver=guard)
