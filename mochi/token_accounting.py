"""Human-only request pricing. Missing rates or usage never mean free calls."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import logging
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)
UNITS = ("input", "output", "cache_read", "cache_write")


def endpoint_key(endpoint: str) -> str:
    value = urlsplit(endpoint)
    if (
        value.scheme != "https" or not value.hostname or value.username
        or value.password or value.query or value.fragment
    ):
        raise ValueError("Pricing requires a credential-free HTTPS endpoint")
    return urlunsplit((value.scheme, value.netloc.lower(), value.path.rstrip("/"), "", ""))


def load_prices(path: Path) -> dict[tuple[str, str, str], dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {"currency", "models"}:
        raise ValueError("Pricing requires currency and models")
    if payload["currency"] != "USD" or not isinstance(payload["models"], list):
        raise ValueError("Pricing currency must be USD and models must be a list")
    prices = {}
    for entry in payload["models"]:
        if not isinstance(entry, dict) or set(entry) != {
            "provider", "endpoint", "model", "source", *UNITS,
        }:
            raise ValueError("Each price needs provider, endpoint, model, source and four rates")
        if any(not isinstance(entry[key], str) or not entry[key].strip()
               for key in ("provider", "endpoint", "model", "source")):
            raise ValueError("Pricing identity and source must be nonempty strings")
        key = (entry["provider"], endpoint_key(entry["endpoint"]), entry["model"])
        if key in prices:
            raise ValueError("Duplicate provider/endpoint/model pricing")
        rates = {}
        for unit in UNITS:
            value = entry[unit]
            if value is None:
                rates[unit] = None
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ValueError("Rates must be nonnegative decimal numbers or null")
            rate = Decimal(str(value))
            if not rate.is_finite() or rate < 0:
                raise ValueError("Rates must be finite and nonnegative")
            rates[unit] = str(rate)
        prices[key] = {"currency": "USD", "source": entry["source"], "per_million": rates}
    return prices


def capture_price(provider: str, endpoint: str, model: str) -> dict:
    from mochi.db import DB_PATH

    path = Path(DB_PATH).parent / "token-prices.json"
    try:
        key = (provider, endpoint_key(endpoint), model)
        if not path.exists():
            return {"status": "unconfigured"}
        price = load_prices(path).get(key)
        return {"status": "configured", **price} if price else {"status": "unconfigured"}
    except (OSError, ValueError, InvalidOperation):
        log.exception("Could not read request pricing")
        return {"status": "invalid", "reason": "pricing_configuration_invalid"}


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
        # DeepSeek reports the complete uncached billable bucket separately.
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


def estimate_cost(units: dict, price: dict, *, embedding: bool = False) -> dict:
    components = {}
    missing = []
    rates = price.get("per_million", {})
    for unit in ("input",) if embedding else UNITS:
        count = units.get(unit)
        rate = rates.get(unit)
        if count == 0:
            components[unit] = "0"
        elif count is None or rate is None:
            missing.append(unit)
        else:
            components[unit] = str(Decimal(count) * Decimal(rate) / Decimal(1_000_000))
    known = sum((Decimal(value) for value in components.values()), Decimal(0))
    return {
        "status": (
            "complete" if not missing
            else "partial" if any(units[unit] for unit in components)
            else "unknown"
        ),
        "currency": "USD",
        "estimated_usd": str(known) if not missing else None,
        "known_component_usd": str(known),
        "components": components,
        "missing": missing,
    }


def record_billing(facts: dict, usage: dict | None, protocol: str) -> None:
    try:
        units = usage_units(usage, protocol)
        facts["units"] = units
        facts["cost"] = estimate_cost(
            units, facts["price"], embedding=protocol == "embeddings.create",
        )
    except (ValueError, TypeError, AttributeError, InvalidOperation):
        log.exception("Could not account for provider usage")
        facts["units"] = dict.fromkeys((*UNITS, "input_total", "reasoning"))
        facts["cost"] = {
            "status": "unavailable", "reason": "invalid_usage", "estimated_usd": None,
        }
