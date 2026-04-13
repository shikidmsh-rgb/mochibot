"""Tool router — selective skill injection via LLM classification + keyword fallback.

Instead of injecting ALL tools into every LLM call (wastes tokens), the router
classifies the user message first, then injects only the relevant tools.

Two-tier detection:
  1. LLM classification (LITE tier, ~100 tokens) — primary
  2. Keyword fallback (0ms, 0 tokens)  — ONLY when LLM returns None or empty

Iron rule: keywords fire ONLY when classify_skills_llm() returns None or empty.
           Never union keywords with LLM results.

v3 additions:
  - SSOT metadata from SKILL.md scan (lazy-initialized)
  - get_tool_meta() for risk level lookup
  - resolve_tier() for model tier routing
"""

import asyncio
import json
import logging
from typing import Optional

from mochi.config import TOOL_ROUTER_MAX_TOKENS

log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# v3: SSOT Metadata — auto-generated from SKILL.md files (lazy-initialized)
# ────────────────────────────────────────────────────────────────────────

TOOL_METADATA: dict[str, dict] = {}
_SKILL_DESCRIPTIONS: dict[str, str] = {}
_SKILL_DEFAULT_TIER: dict[str, str] = {}
_SKILL_METAS: list = []
_metadata_initialized = False


def _ensure_skill_metadata():
    """Lazy-initialize metadata from SKILL.md files.

    Safe to call multiple times (idempotent). Uses file-only scan — no handler imports.
    """
    global TOOL_METADATA, _SKILL_DESCRIPTIONS, _SKILL_DEFAULT_TIER
    global _SKILL_METAS, _metadata_initialized

    if _metadata_initialized:
        return

    try:
        from mochi.skills.base import (
            scan_skill_metadata, build_skill_descriptions,
            build_tool_metadata, build_tier_defaults,
        )

        metas = scan_skill_metadata()
        _SKILL_METAS = metas
        TOOL_METADATA = build_tool_metadata(metas)
        _SKILL_DESCRIPTIONS = build_skill_descriptions(metas)
        _SKILL_DEFAULT_TIER = build_tier_defaults(metas)

        tool_count = len([t for t in TOOL_METADATA if t != "request_tools"])
        log.info("[SkillMeta] Auto-generated: %d router skills, %d tools, %d tier overrides",
                 len(_SKILL_DESCRIPTIONS), tool_count, len(_SKILL_DEFAULT_TIER))

    except Exception as e:
        log.error("[SkillMeta] SSOT scan failed: %s", e)
        raise

    _metadata_initialized = True


def get_tool_meta(tool_name: str) -> dict:
    """Get metadata for a tool. Unknown tools default to L0/unknown."""
    _ensure_skill_metadata()
    return TOOL_METADATA.get(tool_name, {"skill": "unknown", "risk_level": "L0"})


# ────────────────────────────────────────────────────────────────────────
# Model Tier Routing
# ────────────────────────────────────────────────────────────────────────

_VALID_TIERS = {"lite", "chat", "deep"}
_TIER_PRIORITY = {"lite": 0, "chat": 1, "deep": 2}


def resolve_tier(
    llm_tier: str | None = None,
    llm_skills: set[str] | None = None,
) -> str:
    """Resolve final chat tier from pre-router output with 4-level fallback.

    Fallback chain:
    1. LLM returned a valid tier → use it
    2. LLM returned skills but no tier → infer from _SKILL_DEFAULT_TIER + admin overrides
    3. Multiple skills with conflicting tiers → pick highest (deep > chat > lite)
    4. Everything failed → "chat" (default)
    """
    _ensure_skill_metadata()

    # Level 1: LLM returned a valid tier
    if llm_tier and llm_tier in _VALID_TIERS:
        return llm_tier

    # Level 2+3: Infer from skills
    if llm_skills:
        skill_tiers: list[str] = []
        for skill in llm_skills:
            # Check admin override first
            override = _get_skill_tier_override(skill)
            if override and override in _VALID_TIERS:
                skill_tiers.append(override)
            elif skill in _SKILL_DEFAULT_TIER:
                skill_tiers.append(_SKILL_DEFAULT_TIER[skill])
        if skill_tiers:
            return max(skill_tiers, key=lambda t: _TIER_PRIORITY.get(t, 1))

    # Level 4: Default
    return "chat"


def _get_skill_tier_override(skill_name: str) -> str | None:
    """Check skill_config table for admin-set tier override."""
    try:
        from mochi.db import get_skill_config
        config = get_skill_config(skill_name)
        return config.get("_tier")
    except Exception:
        return None


# ────────────────────────────────────────────────────────────────────────
# Keyword map — high-precision only. Fallback when LLM classification fails.
# ────────────────────────────────────────────────────────────────────────

_SKILL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "reminder": ("remind", "提醒", "alarm", "闹钟", "timer", "定时"),
    "todo": ("todo", "待办", "task", "任务", "to-do", "checklist"),
    "memory": ("remember", "记住", "forget", "忘记", "recall", "回忆"),
    "oura": ("sleep", "睡眠", "heart rate", "心率", "hrv", "readiness", "stress", "oura"),
    "web_search": ("web search", "google", "look up", "查一下", "搜一下", "duckduckgo", "ddg"),
}


def _build_skill_descriptions(transport: str = "") -> dict[str, str]:
    """Build {skill_name: description} from SKILL.md metadata (SSOT).

    Uses the lazy-initialized metadata scan — no handler imports needed.
    When transport is specified, rebuilds from metas to exclude incompatible skills.
    Falls back to registry for skills without descriptions.
    """
    _ensure_skill_metadata()

    # When transport is specified, rebuild with transport filtering
    if transport and _SKILL_METAS:
        from mochi.skills.base import build_skill_descriptions
        return build_skill_descriptions(_SKILL_METAS, transport=transport)

    if _SKILL_DESCRIPTIONS:
        return dict(_SKILL_DESCRIPTIONS)

    # Fallback: use registry if SSOT not available
    import mochi.skills as registry
    descriptions = {}
    for info in registry.get_skill_info_all():
        name = info["name"]
        desc = info.get("description", "")
        tools = info.get("tools", [])
        if desc:
            descriptions[name] = desc
        elif tools:
            descriptions[name] = f"Tools: {', '.join(tools)}"
    return descriptions


def _build_router_prompt(descriptions: dict[str, str],
                         active_habits: list[str] | None = None) -> str:
    """Build the system prompt for the LLM router."""
    skill_lines = "\n".join(
        f"- {name}: {desc}" for name, desc in descriptions.items()
    )
    habit_hint = _build_habit_hint(active_habits)
    return (
        "You are a skill classifier. Given a user message, return a JSON object "
        "with the skills needed to handle it.\n\n"
        "Available skills:\n"
        f"{skill_lines}\n\n"
        f"{habit_hint}"
        "Return JSON: {\"skills\": [\"skill1\", \"skill2\"]}\n"
        "If no tools are needed (pure chat), return: {\"skills\": []}\n"
        "Be conservative — only include skills the message clearly needs."
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
        f"Active habits: {names}\n"
        "If the message mentions any of these (or closely related items "
        "like beverages for a water-drinking habit), route to \"habit\".\n\n"
    )


async def classify_skills_llm(message: str, user_id: int | None = None,
                              habits: list[dict] | None = None,
                              transport: str = "") -> Optional[list[str]]:
    """Classify which skills a message needs using LITE tier LLM.

    Returns list of skill names, or None on failure (triggers keyword fallback).
    """
    try:
        from mochi.llm import get_client_for_tier
        from mochi.db import log_usage
    except ImportError:
        log.warning("LLM imports failed, router falling back to keywords")
        return None

    descriptions = _build_skill_descriptions(transport=transport)
    if not descriptions:
        return None

    # Use pre-fetched habits if provided, otherwise fetch
    active_habits: list[str] | None = None
    if habits is not None:
        active_habits = [h["name"] for h in habits if _is_habit_active_today(h)] or None
    elif user_id:
        try:
            from mochi.db import list_habits
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
        )

        log_usage(
            response.prompt_tokens, response.completion_tokens,
            response.total_tokens, model=response.model, purpose="tool_router",
        )

        result = json.loads(response.content)
        skills = result.get("skills", [])
        if isinstance(skills, list):
            log.info("Router classified: %s", skills)
            return skills
        return None

    except (json.JSONDecodeError, KeyError) as e:
        log.warning("Router JSON parse failed: %s", e)
        return None
    except Exception as e:
        log.warning("Router LLM call failed: %s", e)
        return None


def keyword_fallback(message: str) -> list[str]:
    """Detect skills from high-precision keywords. Zero LLM calls.

    ONLY called when classify_skills_llm() returns None.
    """
    msg_lower = message.lower()
    matched = []
    for skill_name, keywords in _SKILL_KEYWORDS.items():
        if any(kw in msg_lower for kw in keywords):
            matched.append(skill_name)
    if matched:
        log.info("Keyword fallback matched: %s", matched)
    return matched


async def classify_skills(message: str, user_id: int | None = None,
                          habits: list[dict] | None = None,
                          transport: str = "") -> list[str]:
    """Main entry point: classify skills for a message.

    LLM first, keyword fallback ONLY when LLM fails or returns empty.
    """
    skills = await classify_skills_llm(message, user_id=user_id, habits=habits,
                                       transport=transport)
    if skills is not None and len(skills) > 0:
        return skills
    # LLM failed or returned empty — fall back to keywords
    return keyword_fallback(message)


# ────────────────────────────────────────────────────────────────────────
# Tool Escalation
# ────────────────────────────────────────────────────────────────────────

# Virtual tool definition — injected when router is active
REQUEST_TOOLS_DEF = {
    "type": "function",
    "function": {
        "name": "request_tools",
        "description": (
            "Request additional tools that were not initially provided. "
            "Call this when you need a capability that is not in your current tool set."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skills": {
                    "type": "string",
                    "description": "Comma-separated skill names to request (e.g. 'web_search,reminder')",
                },
                "reason": {
                    "type": "string",
                    "description": "Brief reason why you need these tools",
                },
            },
            "required": ["skills"],
        },
    },
}


def validate_escalation(args: dict) -> list[str]:
    """Validate escalation request against registered skills.

    Returns list of valid skill names, or empty list if none valid.
    """
    import mochi.skills as registry

    skills_str = args.get("skills", "")
    requested = [s.strip() for s in skills_str.split(",") if s.strip()]

    valid = []
    for name in requested:
        if registry.get_skill(name):
            valid.append(name)
        else:
            log.debug("Escalation: unknown skill %s, skipped", name)

    if valid:
        log.info("Escalation approved: %s (reason: %s)",
                 valid, args.get("reason", ""))
    return valid
