"""Tool router — selective daily-skill injection via LLM classification.

Instead of injecting ALL tools into every LLM call (wastes tokens), the router
classifies the user message first, then injects only the relevant tools.

The catalog is deliberately limited to high-frequency daily skills. Resident
tools and request_tools escalation are computed separately for each turn.

Metadata is generated from SKILL.md without importing handlers.
"""

import asyncio
import json
import logging
from typing import Optional

from mochi.config import TOOL_ROUTER_MAX_TOKENS

log = logging.getLogger(__name__)


def _build_skill_descriptions(transport: str = "") -> dict[str, str]:
    """Build the live allowlisted Router catalog for this transport."""
    from mochi.turn_tool_policy import build_router_catalog

    return build_router_catalog(transport)


def _build_router_prompt(descriptions: dict[str, str],
                         active_habits: list[str] | None = None) -> str:
    """Build the system prompt for the LLM router."""
    skill_lines = "\n".join(
        f"- {name}: {desc}" for name, desc in descriptions.items()
    )
    habit_hint = _build_habit_hint(active_habits)
    return (
        "你是技能分类器。根据用户消息，返回一个 JSON 对象，列出处理该消息所需的技能。\n\n"
        "可用技能：\n"
        f"{skill_lines}\n\n"
        f"{habit_hint}"
        "返回 JSON：{\"skills\": [\"skill1\", \"skill2\"]}\n"
        "如果不需要任何工具（纯聊天），返回：{\"skills\": []}\n"
        "最多选择 2 个，只包含消息明确需要的技能，不要过度分类。"
    )


# ────────────────────────────────────────────────────────────────────────
# Habit hint — dynamic context for pre-router
# ────────────────────────────────────────────────────────────────────────

def _is_habit_active_today(habit: dict) -> bool:
    """Check if a habit is relevant for today's pre-router hint."""
    paused_until = habit.get("paused_until")
    if paused_until:
        from mochi.config import logical_today
        if paused_until >= logical_today():
            return False
    from mochi.skills.habit.logic import parse_frequency, get_allowed_days
    freq = habit.get("frequency", "")
    if not parse_frequency(freq):
        return False
    allowed = get_allowed_days(freq)
    if allowed is not None:
        from datetime import datetime
        from mochi.config import TZ
        if datetime.now(TZ).weekday() not in allowed:
            return False
    return True


def _build_habit_hint(active_habits: list[str] | None) -> str:
    """Build the active-habits hint block for pre-router prompt."""
    if not active_habits:
        return ""
    names = ", ".join(active_habits)
    return (
        f"当前活跃习惯：{names}\n"
        "如果消息提到了这些习惯（或密切相关的内容，比如喝水习惯对应的饮水、喝了一杯等），"
        "请路由到 \"habit\"。\n\n"
    )


async def classify_skills_llm(message: str, user_id: int | None = None,
                              habits: list[dict] | None = None,
                              transport: str = "",
                              catalog: dict[str, str] | None = None) -> Optional[list[str]]:
    """Classify which skills a message needs using LITE tier LLM.

    Returns list of skill names, or None on failure.
    """
    try:
        from mochi.llm import get_client_for_tier, extract_json
        from mochi.db import log_usage
    except ImportError:
        log.warning("LLM imports failed, router returning None")
        return None

    descriptions = (
        dict(catalog)
        if catalog is not None
        else _build_skill_descriptions(transport=transport)
    )
    if not descriptions:
        return None

    # Use pre-fetched habits if provided, otherwise fetch
    active_habits: list[str] | None = None
    if habits is not None:
        active_habits = [h["name"] for h in habits if _is_habit_active_today(h)] or None
    elif user_id:
        try:
            from mochi.skills.habit.queries import list_habits
            raw = list_habits(user_id)
            active_habits = [h["name"] for h in raw if _is_habit_active_today(h)] or None
        except Exception as e:
            log.warning("Failed to fetch habit hints for pre-router: %s", e)

    prompt = _build_router_prompt(descriptions, active_habits=active_habits)

    try:
        client = get_client_for_tier("lite")
        response = await asyncio.to_thread(
            client.chat,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": message},
            ],
            temperature=0.0,
            max_tokens=TOOL_ROUTER_MAX_TOKENS,
            json_mode=True,
        )

        log_usage(
            response.prompt_tokens, response.completion_tokens,
            response.total_tokens, model=response.model, purpose="tool_router",
            reasoning_tokens=response.reasoning_tokens,
            cached_prompt_tokens=response.cached_prompt_tokens,
            cache_write_tokens=response.cache_write_tokens,
        )

        result = json.loads(extract_json(response.content))
        skills = result.get("skills", [])
        if isinstance(skills, list):
            selected: list[str] = []
            for skill in skills:
                if (
                    isinstance(skill, str)
                    and skill in descriptions
                    and skill not in selected
                ):
                    selected.append(skill)
                if len(selected) >= 2:
                    break
            log.info("Router classified: raw=%s selected=%s", skills, selected)
            from mochi.model_health import record_success
            record_success("lite")
            return selected
        return None

    except (json.JSONDecodeError, KeyError) as e:
        log.warning("Router JSON parse failed: %s", e)
        from mochi.model_health import record_failure
        record_failure("lite", str(e))
        return None
    except Exception as e:
        log.warning("Router LLM call failed: %s", e)
        from mochi.model_health import record_failure
        record_failure("lite", str(e))
        return None


async def classify_skills(message: str, user_id: int | None = None,
                          habits: list[dict] | None = None,
                          transport: str = "",
                          catalog: dict[str, str] | None = None) -> list[str]:
    """Main entry point: classify skills for a message.

    LLM classification only. Returns empty list for pure-chat messages.
    Resident tools and request_tools escalation are computed by the turn policy.
    Routing is optional, so a slow Lite call must not hold up Main's reply.
    """
    from mochi import config

    try:
        skills = await asyncio.wait_for(
            classify_skills_llm(message, user_id=user_id, habits=habits,
                                transport=transport, catalog=catalog),
            timeout=config.TOOL_ROUTER_TIMEOUT_S,
        )
    except TimeoutError:
        log.warning(
            "Router timed out after %.1fs; continuing without routed skills",
            config.TOOL_ROUTER_TIMEOUT_S,
        )
        return []
    if skills is not None and len(skills) > 0:
        return skills
    return []
