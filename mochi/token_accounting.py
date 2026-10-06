"""Human-only token accounting. Missing usage is unknown, not zero."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)
UNITS = ("input", "output", "cache_read", "cache_write")


def _count(value) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError("Provider token counts must be nonnegative integers")
    return value


def usage_units(usage: dict | None, protocol: str) -> dict:
    """Keep native usage intact; normalize only the protocols already in use."""
    result = dict.fromkeys((*UNITS, "input_total", "reasoning"))
    if usage is None:
        return result
    if protocol == "embeddings.create":
        result["input"] = result["input_total"] = _count(usage.get("prompt_tokens"))
        return result
    if protocol == "messages.create":
        result.update(
            input=_count(usage.get("input_tokens")),
            output=_count(usage.get("output_tokens")),
            cache_read=_count(usage.get("cache_read_input_tokens")),
            cache_write=_count(usage.get("cache_creation_input_tokens")),
        )
        inputs = [result[key] for key in ("input", "cache_read", "cache_write")]
        result["input_total"] = sum(inputs) if all(value is not None for value in inputs) else None
        return result
    if protocol not in {"chat.completions.create", "responses.create"}:
        return result
    responses = protocol == "responses.create"
    total = _count(usage.get("input_tokens" if responses else "prompt_tokens"))
    result["input_total"] = total
    result["output"] = _count(usage.get("output_tokens" if responses else "completion_tokens"))
    details = usage.get("input_tokens_details" if responses else "prompt_tokens_details") or {}
    output = usage.get("output_tokens_details" if responses else "completion_tokens_details") or {}
    result["reasoning"] = _count(output.get("reasoning_tokens"))
    read = details.get("cached_tokens")
    result["cache_read"] = _count(
        read if read is not None else usage.get("prompt_cache_hit_tokens")
    )
    result["cache_write"] = _count(details.get("cache_write_tokens"))
    if usage.get("prompt_cache_miss_tokens") is not None:
        # DeepSeek reports uncached input separately.
        result["input"] = _count(usage["prompt_cache_miss_tokens"])
        result["cache_write"] = 0
    elif all(result[key] is not None for key in ("input_total", "cache_read", "cache_write")):
        result["input"] = total - result["cache_read"] - result["cache_write"]
        if result["input"] < 0:
            raise ValueError("Cache token counts exceed total input")
    buckets = [result[key] for key in ("input", "cache_read", "cache_write")]
    if total is not None and all(value is not None for value in buckets) and sum(buckets) != total:
        raise ValueError("Input token buckets do not match the reported total")
    return result


def record_usage_units(facts: dict, usage: dict | None, protocol: str) -> None:
    try:
        facts["units"] = usage_units(usage, protocol)
    except (ValueError, TypeError, AttributeError):
        log.exception("Could not account for provider token usage")
        facts["units"] = dict.fromkeys((*UNITS, "input_total", "reasoning"))
        facts["usage_error"] = "invalid_usage"
