"""In-memory model health tracking per tier (lite / main).

Thread-safe counters for success/failure rates.  Mirrors the
error_buffer.py pattern: pure utility, no upward imports.
"""

from __future__ import annotations

import threading
import time
WARN_THRESHOLD = 3  # consecutive failures before user-facing warning

_lock = threading.Lock()
_stats: dict[str, dict] = {}


def _model_api_error_types() -> tuple[type[Exception], ...]:
    types: list[type[Exception]] = []
    try:
        from openai import APIError as OpenAIAPIError
        types.append(OpenAIAPIError)
    except ImportError:
        pass
    try:
        from anthropic import APIError as AnthropicAPIError
        types.append(AnthropicAPIError)
    except ImportError:
        pass
    return tuple(types)


def is_model_api_error(exc: Exception) -> bool:
    """Return whether an exception came from a supported chat provider SDK."""
    error_types = _model_api_error_types()
    return bool(error_types) and isinstance(exc, error_types)


def describe_model_api_error(exc: Exception) -> dict:
    """Return bounded, actionable provider error details without raw payloads."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, bool) or not isinstance(status, int):
        status = None

    code = getattr(exc, "code", None)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        detail = body.get("error", body)
        if isinstance(detail, dict):
            code = code or detail.get("code") or detail.get("type")
    code_text = str(code or "").strip()
    if not code_text.replace("_", "").replace("-", "").isalnum():
        code_text = ""
    code_text = code_text[:80]

    class_name = type(exc).__name__.lower()
    code_lower = code_text.lower()
    if status == 401 or "authentication" in class_name:
        message = "API Key 不正确或已经失效。"
    elif status == 403 or "permission" in class_name:
        message = "API Key 没有访问该模型的权限。"
    elif (
        status == 404
        or code_lower in {"unknown_model", "model_not_found", "resource_not_found"}
    ):
        message = "模型名、Base URL 或 API 路径不存在。"
    elif status == 429 or "ratelimit" in class_name:
        message = "模型额度不足或请求过于频繁。"
    elif status == 408 or "timeout" in class_name:
        message = "请求模型服务超时。"
    elif "connection" in class_name:
        message = "无法连接模型服务。"
    elif status is not None and status >= 500:
        message = "模型服务端暂时异常。"
    else:
        message = "模型服务请求失败。"

    result = {"error": message}
    if status is not None:
        result["status"] = status
    if code_text:
        result["code"] = code_text
    return result


def format_chat_model_api_error(exc: Exception) -> str:
    """Render a concise owner-facing Main API failure."""
    detail = describe_model_api_error(exc)
    technical = []
    if "status" in detail:
        technical.append(f"HTTP {detail['status']}")
    if "code" in detail:
        technical.append(detail["code"])
    suffix = f"（{' · '.join(technical)}）" if technical else ""
    return (
        "模型服务暂时不可用，请到管理后台检查 Chat 模型的 API 配置和服务状态。\n"
        f"错误：{detail['error']}{suffix}"
    )


def _ensure(tier: str) -> dict:
    if tier not in _stats:
        _stats[tier] = {
            "total": 0,
            "failures": 0,
            "consecutive_failures": 0,
            "last_error": None,
            "last_error_time": None,
        }
    return _stats[tier]


def record_success(tier: str) -> None:
    with _lock:
        s = _ensure(tier)
        s["total"] += 1
        s["consecutive_failures"] = 0


def record_failure(tier: str, error_msg: str) -> None:
    with _lock:
        s = _ensure(tier)
        s["total"] += 1
        s["failures"] += 1
        s["consecutive_failures"] += 1
        s["last_error"] = error_msg
        s["last_error_time"] = time.time()


def should_warn_user(tier: str) -> bool:
    """Return True once when consecutive failures reach the threshold.

    Resets the counter so the warning doesn't repeat every turn.
    """
    with _lock:
        s = _stats.get(tier)
        if not s or s["consecutive_failures"] < WARN_THRESHOLD:
            return False
        s["consecutive_failures"] = 0
        return True


def get_warning_message(tier: str) -> str:
    return (
        f"\n\n⚠️ 技能路由模型({tier})最近连续失败，"
        "部分功能可能无法正常触发。建议在管理面板检查模型配置。"
    )


def get_health() -> dict:
    """Return health summary for all tiers."""
    with _lock:
        result = {}
        for tier, s in _stats.items():
            total = s["total"]
            failures = s["failures"]
            result[tier] = {
                "total": total,
                "failures": failures,
                "success_rate": round((total - failures) / total, 3) if total else 1.0,
                "consecutive_failures": s["consecutive_failures"],
                "last_error": s["last_error"],
                "last_error_time": s["last_error_time"],
            }
        return result
