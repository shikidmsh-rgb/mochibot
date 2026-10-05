"""Human-only request accounting, not model context or a billing estimator."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import logging
import os
from pathlib import Path
import time

log = logging.getLogger(__name__)
ENCODING = "o200k_base"


@dataclass(frozen=True)
class TextSource:
    role: str
    text: str
    parts: tuple[tuple[str, str], ...]


def joined_parts(parts: list[tuple[str, str]], separator: str = "\n\n") -> list[tuple[str, str]]:
    result = []
    for index, part in enumerate(parts):
        if index:
            result.append(("separator", separator))
        result.append(part)
    return result


def replace_range(
    parts: list[tuple[str, str]], start: int, end: int,
    replacement: list[tuple[str, str]],
) -> None:
    """Update a text layout using original character offsets, not content guesses."""
    left, right = [], []
    offset = 0
    for source, body in parts:
        stop = offset + len(body)
        if offset < start:
            left.append((source, body[:max(0, start - offset)]))
        if stop > end:
            right.append((source, body[max(0, end - offset):]))
        offset = stop
    parts[:] = left + replacement + right


@lru_cache(maxsize=1)
def _encoding():
    import tiktoken
    from mochi.db import DB_PATH

    os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(Path(DB_PATH).parent / "tokenizer-cache"))
    return tiktoken.get_encoding(ENCODING)


def _size(text: str) -> dict:
    return {"chars": len(text), "utf8_bytes": len(text.encode("utf-8"))}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def cache_fingerprint(request: dict) -> dict:
    """Describe client-visible prefix equality, not provider cache eligibility."""
    def digest(value) -> str:
        return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()

    inputs = request.get("messages", request.get("input", []))
    if not isinstance(inputs, list):
        inputs = [inputs]
    leading_system = []
    for item in inputs:
        if not isinstance(item, dict) or item.get("role") not in {"system", "developer"}:
            break
        leading_system.append(item)
    tools = request.get("tools") or []
    chain = hashlib.sha256()
    prefixes = []
    for item in inputs:
        body = _json(item).encode("utf-8")
        chain.update(len(body).to_bytes(8, "big"))
        chain.update(body)
        prefixes.append(chain.hexdigest())
    return {
        "tools": digest(tools),
        "tool_order": digest([tool.get("function", tool).get("name") for tool in tools]),
        "system": digest({
            "system": request.get("system"), "instructions": request.get("instructions"),
            "leading_messages": leading_system,
        }),
        "input_prefixes": prefixes,
        "cache_policy": {
            key: request.get(key, (request.get("extra_body") or {}).get(key))
            for key in ("prompt_cache_key", "prompt_cache_options", "prompt_cache_retention")
        },
        "reasoning": request.get("reasoning", (request.get("extra_body") or {}).get("thinking")),
    }


def capture_request(request: dict, sources: list[TextSource]) -> dict:
    """Count original SDK-boundary content before evidence redaction."""
    started = time.monotonic()
    encoding = _encoding()
    parts: list[dict] = []
    call_names: dict[str, str] = {}
    source_layouts: dict[tuple[str, str], list[tuple[tuple[str, str], ...]]] = {}
    source_occurrences: dict[tuple[str, str], int] = {}
    for item in sources:
        source_layouts.setdefault((item.role, item.text), []).append(item.parts)

    def find_calls(value) -> None:
        if isinstance(value, list):
            for item in value:
                find_calls(item)
        elif isinstance(value, dict):
            if value.get("type") in {"function_call", "tool_use", "function"}:
                name = value.get("function", value).get("name")
                identifier = value.get("call_id", value.get("id"))
                if identifier and name:
                    call_names[identifier] = name
            for key in ("content", "tool_calls"):
                if key in value:
                    find_calls(value[key])

    find_calls(request.get("messages"))
    find_calls(request.get("input"))

    def text(value: str, path: str, source: str, *, role: str = "") -> None:
        key = (role, value)
        occurrence = source_occurrences.get(key, 0)
        layouts = source_layouts.get(key, [])
        layout = layouts[occurrence] if occurrence < len(layouts) else ((source, value),)
        source_occurrences[key] = occurrence + 1
        measured = []
        offset = 0
        for name, body in layout:
            measured.append({
                "path": path, "source": name, "representation": "text",
                "char_start": offset, "char_end": offset + len(body),
                **_size(body), "reference_tokens": 0, "cross_boundary_tokens": 0,
            })
            offset += len(body)
        if value:
            index = byte_offset = 0
            part_end = measured[0]["utf8_bytes"]
            for token in encoding.encode_ordinary(value):
                while byte_offset >= part_end:
                    index += 1
                    part_end += measured[index]["utf8_bytes"]
                token_end = byte_offset + len(encoding.decode_single_token_bytes(token))
                measured[index]["reference_tokens"] += 1
                measured[index]["cross_boundary_tokens"] += token_end > part_end
                byte_offset = token_end
        parts.extend(measured)

    def structured(value, path: str, source: str) -> None:
        body = _json(value)
        parts.append({
            "path": path, "source": source, "representation": "canonical_json",
            **_size(body), "reference_tokens": len(encoding.encode_ordinary(body)),
        })

    def opaque(value, path: str, source: str) -> None:
        body = value if isinstance(value, str) else _json(value)
        parts.append({
            "path": path, "source": source, "representation": "opaque",
            **_size(body), "reference_tokens": None,
        })

    def content(value, path: str, source: str, *, role: str = "") -> None:
        if isinstance(value, str):
            text(value, path, source, role=role)
        elif isinstance(value, list):
            for index, block in enumerate(value):
                content(block, f"{path}[{index}]", source, role=role)
        elif isinstance(value, dict):
            kind = value.get("type", "")
            if kind in {
                "image", "image_url", "input_image", "input_audio", "audio",
                "input_file", "file", "redacted_thinking",
            }:
                opaque(value, path, f"{source}.{kind}")
            elif kind in {"tool_use", "function_call"}:
                structured(value, path, f"tool_call.{value.get('name', 'unknown')}")
            elif kind in {"tool_result", "function_call_output"}:
                call_id = value.get("tool_use_id", value.get("call_id", "unknown"))
                key = "content" if "content" in value else "output"
                content(
                    value.get(key), f"{path}.{key}",
                    f"tool_result.{call_names.get(call_id, call_id)}",
                )
            elif kind == "reasoning":
                for key in ("summary", "content"):
                    if key in value:
                        content(value[key], f"{path}.{key}", "reasoning")
                if value.get("encrypted_content") is not None:
                    opaque(value["encrypted_content"], f"{path}.encrypted_content", "reasoning.encrypted")
            elif "text" in value:
                text(value["text"], f"{path}.text", source, role=role)
            elif kind == "thinking":
                text(value["thinking"], f"{path}.thinking", "reasoning")
                if "signature" in value:
                    opaque(value["signature"], f"{path}.signature", "reasoning.signature")
            elif "content" in value:
                content(value["content"], f"{path}.content", source, role=role)
            else:
                opaque(value, path, f"{source}.unclassified")
        elif value is not None:
            opaque(value, path, f"{source}.unclassified")

    for key in ("system", "instructions"):
        if key in request:
            content(request[key], key, "system", role="system")
    for key in ("messages", "input"):
        values = request.get(key)
        if values is None:
            continue
        if not isinstance(values, list):
            content(values, key, "input")
            continue
        for index, message in enumerate(values):
            path = f"{key}[{index}]"
            if not isinstance(message, dict) or "role" not in message:
                content(message, path, "input")
                continue
            role = message["role"]
            source = f"message.{role}"
            if role == "tool":
                call_id = message.get("tool_call_id", "unknown")
                source = f"tool_result.{call_names.get(call_id, call_id)}"
            content(message.get("content"), f"{path}.content", source, role=role)
            if message.get("reasoning_content") is not None:
                text(message["reasoning_content"], f"{path}.reasoning_content", "reasoning")
            for call_index, call in enumerate(message.get("tool_calls") or []):
                name = call.get("function", {}).get("name", "unknown")
                structured(call, f"{path}.tool_calls[{call_index}]", f"tool_call.{name}")
    for index, tool in enumerate(request.get("tools") or []):
        name = tool.get("function", tool).get("name", "unknown")
        structured(tool, f"tools[{index}]", f"tool_schema.{name}")
    for key in ("response_format", "text"):
        if key in request:
            structured(request[key], key, "output_format")

    return {
        "status": "recorded",
        "encoding": ENCODING,
        "basis": "whole_text_token_start_attribution",
        "provider_tokenizer_verified": False,
        "counted_before_redaction": True,
        "model": request.get("model"),
        "parts": parts,
        "totals": totals(parts),
        "provider_usage": None,
        "capture_ms": (time.monotonic() - started) * 1000,
    }


def totals(parts: list[dict]) -> dict:
    return {
        "chars": sum(part["chars"] for part in parts),
        "utf8_bytes": sum(part["utf8_bytes"] for part in parts),
        "reference_tokens": sum(
            part["reference_tokens"] for part in parts if part["reference_tokens"] is not None
        ),
        "uncounted_parts": sum(part["reference_tokens"] is None for part in parts),
        "cross_boundary_tokens": sum(part.get("cross_boundary_tokens", 0) for part in parts),
    }


def record_request(request: dict, sources: list[TextSource]) -> dict:
    try:
        return capture_request(request, sources)
    except Exception as exc:
        log.exception("Could not record token distribution")
        return {
            "status": "unavailable", "error": type(exc).__name__,
            "model": request.get("model"), "provider_usage": None,
        }


def record_usage(distribution: dict, response) -> None:
    try:
        usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump(mode="json")
        if usage is not None and not isinstance(usage, dict):
            raise TypeError("Provider usage is not an object")
        distribution["provider_usage"] = usage
        distribution["provider_usage_status"] = "reported" if usage is not None else "not_reported"
    except Exception as exc:
        log.exception("Token distribution could not read provider usage")
        distribution["provider_usage_status"] = "unavailable"
        distribution["provider_usage_error"] = type(exc).__name__


def summarize(calls: list[dict]) -> dict:
    """Aggregate attempted input, never infer billing for failed/missing calls."""
    by_source: dict[str, list[dict]] = {}
    for call in calls:
        for part in call["distribution"].get("parts", []):
            by_source.setdefault(part["source"], []).append(part)
    return {
        "scope": "returned_page",
        "calls": len(calls),
        "recorded_calls": sum(call["distribution"]["status"] == "recorded" for call in calls),
        "calls_with_provider_usage": sum(
            call["distribution"].get("provider_usage") is not None for call in calls
        ),
        "by_source": {name: totals(parts) for name, parts in by_source.items()},
    }
