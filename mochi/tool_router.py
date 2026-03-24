"""Tool router — selective skill injection via LLM classification + keyword fallback.

Instead of injecting ALL tools into every LLM call (wastes tokens), the router
classifies the user message first, then injects only the relevant tools.

Two-tier detection:
  1. LLM classification (BG_FAST tier, ~100 tokens) — primary
  2. Keyword fallback (0ms, 0 tokens)  — ONLY when LLM returns None or empty

Iron rule: keywords fire ONLY when classify_skills_llm() returns None or empty.
           Never union keywords with LLM results.
"""

import asyncio
import json
import logging
from typing import Optional

from mochi.config import TOOL_ROUTER_MAX_TOKENS

log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Keyword map — high-precision only. Fallback when LLM classification fails.
# ────────────────────────────────────────────────────────────────────────

_SKILL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "reminder": ("remind", "提醒", "alarm", "闹钟", "timer", "定时"),
    "todo": ("todo", "待办", "task", "任务", "to-do", "checklist"),
    "memory": ("remember", "记住", "forget", "忘记", "recall", "回忆"),
    "oura": ("sleep", "睡眠", "heart rate", "心率", "hrv", "readiness", "stress", "oura"),
}


def _build_skill_descriptions() -> dict[str, str]:
    """Build {skill_name: description} from registered skills."""
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


def _build_router_prompt(descriptions: dict[str, str]) -> str:
    """Build the system prompt for the LLM router."""
    skill_lines = "\n".join(
        f"- {name}: {desc}" for name, desc in descriptions.items()
    )
    return (
        "You are a skill classifier. Given a user message, return a JSON object "
        "with the skills needed to handle it.\n\n"
        "Available skills:\n"
        f"{skill_lines}\n\n"
        "Return JSON: {\"skills\": [\"skill1\", \"skill2\"]}\n"
        "If no tools are needed (pure chat), return: {\"skills\": []}\n"
        "Be conservative — only include skills the message clearly needs."
    )


async def classify_skills_llm(message: str) -> Optional[list[str]]:
    """Classify which skills a message needs using BG_FAST tier LLM.

    Returns list of skill names, or None on failure (triggers keyword fallback).
    """
    try:
        from mochi.llm import get_client_for_tier
        from mochi.db import log_usage
    except ImportError:
        log.warning("LLM imports failed, router falling back to keywords")
        return None

    descriptions = _build_skill_descriptions()
    if not descriptions:
        return None

    prompt = _build_router_prompt(descriptions)

    try:
        client = get_client_for_tier("bg_fast")
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


async def classify_skills(message: str) -> list[str]:
    """Main entry point: classify skills for a message.

    LLM first, keyword fallback ONLY when LLM fails or returns empty.
    """
    skills = await classify_skills_llm(message)
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
