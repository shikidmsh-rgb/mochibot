"""Reply-context recovery, truthful failures, and per-chunk delivery validity."""

import base64
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mochi.ai_client import ChatResult
from mochi.db import get_skill_config
from mochi.transport import DeliveryError, ImageAttachment, MAX_IMAGE_BYTES
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
        "item_list": [{"type": 3}],
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


def _image_session(data, *, status=200, content_length=None):
    async def chunks(_size):
        for start in range(0, len(data), 64 * 1024):
            yield data[start:start + 64 * 1024]

    @asynccontextmanager
    async def response(*args, **kwargs):
        yield SimpleNamespace(
            status=status, content_length=content_length,
            content=SimpleNamespace(iter_chunked=chunks),
        )

    return SimpleNamespace(get=MagicMock(side_effect=response))


def _encrypt_image(data, key):
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


@pytest.mark.asyncio
async def test_wechat_image_send_encrypts_upload_and_requires_receipt(monkeypatch):
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    transport = WeixinTransport()
    transport._owner_weixin_id = "owner"
    transport._context_tokens["owner"] = "reply-token"
    image = ImageAttachment(b"\xff\xd8\xffimage")
    upload_headers = {"x-encrypted-param": "download-reference"}
    active = True

    @asynccontextmanager
    async def upload(*args, **kwargs):
        yield SimpleNamespace(status=200, headers=upload_headers)

    upload_call = MagicMock(side_effect=upload)
    transport._session = SimpleNamespace(post=upload_call)
    api = AsyncMock(side_effect=[
        {"upload_full_url": "https://novac2c.cdn.weixin.qq.com/c2c/upload?signed=ref"},
        {},
    ])
    monkeypatch.setattr(transport, "_api_post", api)
    await transport.send_image_checked(1, image, can_deliver=lambda: active)
    authorize, send = api.call_args_list
    assert authorize.args[0] == "ilink/bot/getuploadurl"
    params = authorize.args[1]
    assert params["media_type"] == 1
    assert params["rawsize"] == len(image.data)
    assert params["to_user_id"] == "owner"
    request = upload_call.call_args
    assert request.kwargs["headers"] == {"Content-Type": "application/octet-stream"}
    assert request.kwargs["allow_redirects"] is False
    key = bytes.fromhex(params["aeskey"])
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    padded = decryptor.update(request.kwargs["data"]) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    assert unpadder.update(padded) + unpadder.finalize() == image.data
    assert send.args[0] == "ilink/bot/sendmessage"
    message = send.args[1]["msg"]
    assert message["context_token"] == "reply-token"
    item = message["item_list"][0]
    assert item["type"] == 2
    assert item["image_item"]["media"]["encrypt_query_param"] == "download-reference"
    assert base64.b64decode(item["image_item"]["media"]["aes_key"]).decode() == params["aeskey"]
    assert item["image_item"]["mid_size"] == len(request.kwargs["data"])

    api.reset_mock()
    api.side_effect = None
    api.return_value = {"upload_param": "opaque"}
    upload_headers.clear()
    with pytest.raises(DeliveryError) as missing_receipt:
        await transport.send_image_checked(1, image)
    assert missing_receipt.value.outcome == "delivery_unavailable"
    api.assert_awaited_once()

    @asynccontextmanager
    async def expires_after_upload(*args, **kwargs):
        nonlocal active
        yield SimpleNamespace(status=200, headers={"x-encrypted-param": "uploaded"})
        active = False

    upload_call.side_effect = expires_after_upload
    api.reset_mock()
    with pytest.raises(DeliveryError) as expired_after_upload:
        await transport.send_image_checked(1, image, can_deliver=lambda: active)
    assert expired_after_upload.value.outcome == "expired"
    api.assert_awaited_once()

    active = False
    api.reset_mock()
    with pytest.raises(DeliveryError) as expired:
        await transport.send_image_checked(1, image, can_deliver=lambda: active)
    assert expired.value.outcome == "expired"
    api.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["hex", "base64", "base64_hex", "plain", "full_url"])
async def test_wechat_image_cdn_decodes_protocol_keys_without_sending_bot_token(encoding):
    data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aCWQAAAAASUVORK5CYII="
    )
    key = bytes(range(16))
    media = {"encrypt_query_param": "opaque+/=&"}
    item = {"media": media}
    if encoding == "hex":
        item["aeskey"] = key.hex()
        media["aes_key"] = "unused-because-hex-takes-precedence"
    elif encoding != "plain":
        encoded = key.hex().encode() if encoding == "base64_hex" else key
        media["aes_key"] = base64.b64encode(encoded).decode()
    if encoding == "full_url":
        media = item["media"] = {
            "full_url": "https://novac2c.cdn.weixin.qq.com/c2c/download?signed=value",
            "aes_key": media["aes_key"],
        }
    transport = WeixinTransport()
    transport._session = _image_session(
        data if encoding == "plain" else _encrypt_image(data, key),
    )
    image = await transport._download_image(item)
    assert image == ImageAttachment(data=data, media_type="image/png")
    request = transport._session.get.call_args
    assert request.args[0] == media.get(
        "full_url",
        "https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param=opaque%2B%2F%3D%26",
    )
    assert "headers" not in request.kwargs
    assert request.kwargs["allow_redirects"] is False
    assert request.kwargs["timeout"].total == 30


@pytest.mark.asyncio
@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize("extra", [0, 1])
async def test_wechat_image_size_limit_includes_stream_and_decrypted_bytes(encrypted, extra):
    from mochi.transport.weixin import _ImageTooLargeError

    data = b"\xff\xd8\xff" + b"x" * (MAX_IMAGE_BYTES - 3 + extra)
    item = {"media": {"encrypt_query_param": "opaque"}}
    if encrypted:
        key = bytes(range(16))
        item["aeskey"] = key.hex()
        payload = _encrypt_image(data, key)
    else:
        payload = data
    transport = WeixinTransport()
    transport._session = _image_session(payload)
    if extra:
        with pytest.raises(_ImageTooLargeError):
            await transport._download_image(item)
    else:
        image = await transport._download_image(item)
        assert image.data == data
        assert image.media_type == "image/jpeg"


@pytest.mark.asyncio
async def test_wechat_image_rejects_failed_download_bad_key_and_unsafe_url():
    transport = WeixinTransport()
    transport._session = _image_session(b"", status=403)
    item = {"media": {"encrypt_query_param": "private-query"}}
    with pytest.raises(ValueError, match="HTTP 403"):
        await transport._download_image(item)

    transport._session.get.reset_mock()
    with pytest.raises(ValueError, match="AES-128"):
        await transport._download_image({**item, "aeskey": "00"})
    with pytest.raises(ValueError, match="HTTPS CDN"):
        await transport._download_image({
            "media": {"full_url": "https://127.0.0.1/private"},
        })
    transport._session.get.assert_not_called()

    transport._session = _image_session(b"not an image")
    with pytest.raises(ValueError, match="Unsupported image"):
        await transport._download_image(item)
    with pytest.raises(ValueError):
        await transport._download_image({**item, "aeskey": bytes(range(16)).hex()})


@pytest.fixture
def wechat_image_transport(monkeypatch):
    import mochi.admin.admin_crypto as crypto
    import mochi.transport.weixin as weixin

    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-key")
    monkeypatch.setattr(crypto, "_fernet_instance", None)
    monkeypatch.setattr(weixin, "WEIXIN_ALLOWED_USERS", ["owner"])
    transport = WeixinTransport()
    monkeypatch.setattr(transport, "_get_typing_ticket", AsyncMock(return_value=None))
    monkeypatch.setattr(transport, "_send_text", AsyncMock(return_value=True))
    monkeypatch.setattr(
        transport, "_download_image",
        AsyncMock(return_value=ImageAttachment(b"\xff\xd8\xff")),
    )
    monkeypatch.setattr(weixin, "_on_message_callback", AsyncMock(return_value=None))
    return transport


@pytest.mark.asyncio
@pytest.mark.parametrize("caption", ["", "What is this?", "/restart"])
async def test_wechat_image_reaches_main_and_can_bind_owner(wechat_image_transport, caption):
    import mochi.transport.weixin as weixin
    import mochi.heartbeat as heartbeat

    transport = wechat_image_transport
    image_item = {"media": {"encrypt_query_param": "private-query"}}
    items = [{"type": 2, "image_item": image_item}]
    if caption:
        items.append({"type": 1, "text_item": {"text": caption}})
    await transport._handle_message({
        "from_user_id": "owner", "context_token": "test-token", "item_list": items,
    })
    transport._download_image.assert_awaited_once_with(image_item)
    weixin._on_message_callback.assert_awaited_once()
    incoming = weixin._on_message_callback.call_args.args[0]
    assert incoming.image == ImageAttachment(b"\xff\xd8\xff")
    assert incoming.text == (caption or "用户发来一张图片。")
    assert incoming.transport == "wechat"
    assert incoming.owner_authorized
    assert transport._owner_weixin_id == "owner"
    assert transport._context_tokens["owner"] == "test-token"
    assert not heartbeat._active_chat_tokens
    transport._send_text.assert_not_called()


@pytest.mark.asyncio
async def test_wechat_image_failure_is_reported_without_text_only_main_call(
    wechat_image_transport, caplog,
):
    import mochi.transport.weixin as weixin

    transport = wechat_image_transport
    message = {
        "from_user_id": "owner", "context_token": "test-token",
        "item_list": [
            {"type": 2, "image_item": {}},
            {"type": 1, "text_item": {"text": "What is this?"}},
        ],
    }
    for failure, expected in [
        (TimeoutError("private-cdn-url"), "图片下载或解析失败"),
        (weixin._ImageTooLargeError(), "5 MB"),
    ]:
        transport._download_image.side_effect = failure
        await transport._handle_message(message)
        assert expected in transport._send_text.call_args.args[1]
    assert "private-cdn-url" not in caplog.text
    weixin._on_message_callback.assert_not_called()

    transport._download_image.reset_mock()
    await transport._handle_message({**message, "from_user_id": "stranger"})
    transport._download_image.assert_not_called()
    message["item_list"].append({"type": 2, "image_item": {}})
    await transport._handle_message(message)
    transport._download_image.assert_not_called()
    assert "分开发送" in transport._send_text.call_args.args[1]
    weixin._on_message_callback.assert_not_called()


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
        1, ChatResult(text=delimiter.join(paragraphs)),
    )
    chunks = [
        call.args[1]["msg"]["item_list"][0]["text_item"]["text"]
        for call in api.call_args_list
    ]
    assert len(chunks) == 8
    assert "\n\n".join(chunks) == "\n\n".join(paragraphs)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["telegram", "wechat"])
@pytest.mark.parametrize("complete", [True, False])
async def test_owner_chat_stays_active_until_whole_final_reply(monkeypatch, name, complete):
    import mochi.heartbeat as heartbeat
    import mochi.transport.telegram as telegram
    import mochi.transport.weixin as weixin

    callbacks = []
    result = ChatResult(
        text="First bubble here.|||Second bubble here.",
        stickers=["sticker"] if name == "telegram" else [],
        _after_delivery=[lambda: callbacks.append("update")],
    )

    async def main_turn(_message):
        assert heartbeat._active_chat_tokens
        return result

    if name == "telegram":
        transport = telegram.TelegramTransport()
        transport._app = SimpleNamespace(bot=SimpleNamespace(
            send_message=AsyncMock(), send_chat_action=AsyncMock(),
        ))
        monkeypatch.setattr(telegram, "_on_message_callback", main_turn)
        monkeypatch.setattr(transport, "_check_owner", AsyncMock(return_value=1))

        async def sticker(*args, **kwargs):
            assert heartbeat._active_chat_tokens
            assert callbacks == []
            return complete

        monkeypatch.setattr(transport, "send_sticker", sticker)
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=1),
            message=SimpleNamespace(
                caption="", sticker=SimpleNamespace(file_id="input", emoji="", set_name=""),
            ),
        )
        await transport._handle_sticker(update, None)
    else:
        transport = WeixinTransport()
        transport.restore_owner_id("owner")
        transport._session = object()
        monkeypatch.setattr(weixin, "WEIXIN_BUBBLE_DELAY_S", 0)
        monkeypatch.setattr(weixin, "WEIXIN_ALLOWED_USERS", [])
        monkeypatch.setattr(weixin, "_on_message_callback", main_turn)
        monkeypatch.setattr(transport, "_get_typing_ticket", AsyncMock(return_value=None))
        chunks = []

        async def send(*args, **kwargs):
            assert heartbeat._active_chat_tokens
            assert callbacks == []
            chunks.append(args)
            return {"ret": -1, "errcode": 42} if not complete and len(chunks) == 2 else {}

        monkeypatch.setattr(transport, "_api_post", send)
        await transport._handle_message({
            "from_user_id": "owner", "context_token": "test-token",
            "item_list": [{"type": 1, "text_item": {"text": "Hello"}}],
        })
        assert len(chunks) == 2

    assert not heartbeat._active_chat_tokens
    assert callbacks == (["update"] if complete else [])
    if complete:
        result.confirm_delivered(final=True)
        assert callbacks == ["update"]


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [True, False])
async def test_update_result_acknowledges_only_confirmed_delivery(monkeypatch, delivered):
    import mochi.update_service as updates
    from mochi.db import get_recent_messages
    from mochi.main import _deliver_update_result

    receipt = {
        "request_id": "update-result", "user_id": 1, "channel_id": 1,
        "transport": "fake", "message": "Update outcome",
    }
    monkeypatch.setattr(updates, "peek_update_result", lambda: receipt)
    acknowledgements = []

    def acknowledge(request_id):
        acknowledgements.append(request_id)
        return True

    monkeypatch.setattr(updates, "ack_update_result", acknowledge)
    transport = SimpleNamespace(name="fake", send_message=AsyncMock(return_value=delivered))
    await _deliver_update_result(transport)

    assert acknowledgements == (["update-result"] if delivered else [])
    messages = get_recent_messages(1)
    assert len(messages) == int(delivered)
    if delivered:
        assert messages[0]["role"] == "assistant"
        assert messages[0]["turn_id"] == "system_update:update-result"
        from mochi.db import _connect
        conn = _connect()
        processed = conn.execute(
            "SELECT processed FROM messages WHERE turn_id=?", ("system_update:update-result",),
        ).fetchone()[0]
        conn.close()
        assert processed == 1
