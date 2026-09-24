"""Optional image generation, independent of Main/Lite."""

import base64
import json
import logging
from urllib.parse import quote, urlsplit

import aiohttp

from mochi.admin.admin_crypto import decrypt_api_key, encrypt_api_key, is_encrypted
from mochi.admin.admin_db import _is_supported_model
from mochi.db import delete_skill_config, get_skill_config, set_skill_config
from mochi.transport import ImageAttachment, MAX_IMAGE_BYTES

log = logging.getLogger(__name__)
_CONFIG_SCOPE = "_image_generation"


def resolve_protocol(protocol: str, base_url: str) -> tuple[str, str]:
    if protocol not in {"auto", "openai", "gemini"}:
        raise ValueError("请选择自动识别、OpenAI Images 或 Gemini 原生接口。")
    root = base_url.strip().rstrip("/")
    if not root:
        root = (
            "https://generativelanguage.googleapis.com/v1beta"
            if protocol == "gemini" else "https://api.openai.com/v1"
        )
    if not _is_supported_model("openai", root):
        raise ValueError("Base URL 必须是无凭据、查询参数的 HTTPS API 根地址。")
    parsed = urlsplit(root)
    host = parsed.hostname or ""
    path = parsed.path.rstrip("/")
    if (
        path.endswith(("/images/generations", "/generateContent", "/interactions"))
        or ":generateContent" in path
    ):
        raise ValueError("请填写 API 根地址，不要填写生图请求的完整路径。")
    if protocol == "auto":
        if host == "generativelanguage.googleapis.com":
            protocol = "gemini"
        elif host == "api.openai.com" or (
            host.endswith((".services.ai.azure.com", ".openai.azure.com"))
            and path == "/openai/v1"
        ):
            protocol = "openai"
        else:
            raise ValueError("无法从此地址判断生图协议，请手动选择接口类型。")
    if protocol == "gemini" and path.endswith("/openai"):
        raise ValueError("Gemini 原生生图请使用 /v1beta 根地址，不是 /openai 聊天接口。")
    return protocol, root


def get_image_config(*, include_key: bool = False) -> dict:
    raw = get_skill_config(_CONFIG_SCOPE).get("config")
    cfg = json.loads(raw) if raw else {
        "protocol": "auto", "base_url": "", "model": "", "api_key": "",
    }
    result = {key: cfg[key] for key in ("protocol", "base_url", "model")}
    result["api_key_set"] = bool(cfg["api_key"])
    result["configured"] = bool(cfg["model"] and cfg["api_key"])
    result["resolved_protocol"] = resolve_protocol(cfg["protocol"], cfg["base_url"])[0]
    if include_key:
        result["api_key"] = decrypt_api_key(cfg["api_key"])
        if cfg["api_key"] and not result["api_key"]:
            raise ValueError("图片模型 Key 无法解密，请检查原有 ADMIN_TOKEN。")
    return result


def save_image_config(protocol: str, base_url: str, model: str, api_key: str) -> None:
    resolve_protocol(protocol, base_url)
    model = model.strip()
    if not model:
        raise ValueError("请填写图片生成模型名。")
    previous = get_skill_config(_CONFIG_SCOPE).get("config")
    old = json.loads(previous) if previous else {}
    if api_key == "__KEEP__":
        if old and resolve_protocol(protocol, base_url) != resolve_protocol(
            old["protocol"], old["base_url"],
        ):
            raise ValueError("更换接口地址或协议时请重新填写 Key，避免把原有凭据发给其他服务。")
        encrypted = old.get("api_key", "")
    else:
        if not api_key.strip():
            raise ValueError("请填写图片生成 API Key。")
        encrypted = encrypt_api_key(api_key.strip())
        if not is_encrypted(encrypted):
            raise ValueError("Key 加密失败，未保存。请检查 ADMIN_TOKEN 和 cryptography。")
    if not encrypted:
        raise ValueError("尚无可保留的 Key，请填写 API Key。")
    set_skill_config(_CONFIG_SCOPE, "config", json.dumps({
        "protocol": protocol, "base_url": base_url.strip().rstrip("/"),
        "model": model, "api_key": encrypted,
    }))


def clear_image_config() -> None:
    delete_skill_config(_CONFIG_SCOPE, "config")


async def _read_bounded(response: aiohttp.ClientResponse, limit: int) -> bytes:
    data = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        data.extend(chunk)
        if len(data) > limit:
            raise ValueError("图片或响应过大，未继续处理；图片上限为 5 MB。")
    return bytes(data)


async def generate_image(prompt: str) -> ImageAttachment:
    if not prompt.strip():
        raise ValueError("图片描述不能为空。")
    cfg = get_image_config(include_key=True)
    if not cfg["configured"]:
        raise ValueError("请先配置图片生成模型。")
    protocol, root = resolve_protocol(cfg["protocol"], cfg["base_url"])
    if protocol == "openai":
        url = root + "/images/generations"
        headers = {"Authorization": f"Bearer {cfg['api_key']}"}
        body = {"model": cfg["model"], "prompt": prompt, "n": 1}
    else:
        model = quote(cfg["model"].removeprefix("models/"), safe="")
        url = f"{root}/models/{model}:generateContent"
        headers = {"x-goog-api-key": cfg["api_key"]}
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }
    # One explicit request; never retry a potentially billable generation.
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
            async with session.post(url, headers=headers, json=body, allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError(
                        f"生图服务返回 HTTP {response.status}，请检查 Key、模型权限和接口地址。"
                    )
                raw = await _read_bounded(response, MAX_IMAGE_BYTES * 4 // 3 + 1024 * 1024)
                payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("生图服务未返回有效的结果对象。")
            if protocol == "openai":
                items = payload.get("data")
                if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
                    raise ValueError("生图服务未返回一张图片。")
                item = items[0]
                encoded = item.get("b64_json")
                if isinstance(encoded, str) and encoded:
                    return ImageAttachment.from_bytes(base64.b64decode(encoded, validate=True))
                image_url = item.get("url")
                if not isinstance(image_url, str):
                    raise ValueError("生图服务未返回图片数据或下载地址。")
                parsed = urlsplit(image_url)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError("生图服务返回的图片下载地址不是安全的 HTTPS 地址。")
                async with session.get(image_url, allow_redirects=False) as response:
                    if response.status != 200:
                        raise ValueError(f"生成图片下载失败（HTTP {response.status}），未重新生成。")
                    return ImageAttachment.from_bytes(await _read_bounded(response, MAX_IMAGE_BYTES))
            candidates = payload.get("candidates", [])
            if not isinstance(candidates, list) or not candidates:
                raise ValueError("生图服务未返回图片，可能被服务商拒绝。")
            candidate = candidates[0]
            if not isinstance(candidate, dict) or candidate.get("finishReason") != "STOP":
                raise ValueError("生图服务未完整生成图片，未使用不完整结果。")
            content = candidate.get("content", {})
            parts = content.get("parts", []) if isinstance(content, dict) else []
            if not isinstance(parts, list):
                raise ValueError("生图服务返回的图片内容格式无效。")
            images = [
                part["inlineData"] for part in parts
                if isinstance(part, dict) and not part.get("thought")
                and isinstance(part.get("inlineData"), dict)
            ]
            if len(images) != 1 or not isinstance(images[0].get("data"), str):
                raise ValueError("生图服务未返回一张完整图片。")
            return ImageAttachment.from_bytes(base64.b64decode(images[0]["data"], validate=True))
    except (aiohttp.ClientError, TimeoutError) as exc:
        log.warning("Image generation request failed (%s)", type(exc).__name__)
        raise ValueError("生图请求连接失败或超时，结果未知；未自动重试，服务商可能已计费。") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("生图服务返回了无法解析的响应。") from exc
