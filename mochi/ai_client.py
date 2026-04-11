"""AI client — orchestrates LLM chat with tool dispatch and memory context.

This is the "brain" that ties together:
- LLM provider (chat completions)
- Skill registry (tool execution)
- Memory (core memory in system prompt, extraction after conversations)
- Prompt loader (system personality)
"""

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta

from mochi.llm import get_client, get_client_for_tier, LLMResponse
from mochi.prompt_loader import get_prompt
from mochi.db import (
    save_message, get_recent_messages, get_core_memory, log_usage,
)
import mochi.skills as skill_registry
from mochi.transport import IncomingMessage

log = logging.getLogger(__name__)


def _build_system_prompt(user_id: int, usage_rules: str = "") -> str:
    """Build the system prompt: personality(Chat) + heartbeat context + core memory.

    Args:
        user_id: Owner user ID for core memory lookup.
        usage_rules: Optional tool usage rules to inject (from pre-router).
    """
    from mochi.config import HEARTBEAT_INTERVAL_MINUTES, AWAKE_HOUR_START, AWAKE_HOUR_END, TIMEZONE_OFFSET_HOURS
    personality = get_prompt("system_chat/soul")
    agent_desc = get_prompt("system_chat/agent")
    core_memory = get_core_memory(user_id)

    # Current local time (respects TIMEZONE_OFFSET_HOURS)
    tz = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))
    now = datetime.now(tz)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S %Z")

    parts = []
    if personality:
        parts.append(personality)
    if agent_desc:
        parts.append(agent_desc)

    # Always inject current time so relative reminders ("in 5 minutes") can be resolved
    parts.append(f"## Current time\nRight now it is **{now_str}** (UTC{TIMEZONE_OFFSET_HOURS:+d}).")

    # Framework-injected: let the bot know about its own heartbeat
    parts.append(
        f"## Your background process\n"
        f"You have a heartbeat loop that runs every {HEARTBEAT_INTERVAL_MINUTES} minutes "
        f"while you're awake ({AWAKE_HOUR_START}:00–{AWAKE_HOUR_END}:00). "
        f"It observes context (time, conversation patterns, etc.) and sometimes decides to "
        f"proactively reach out — a check-in, a nudge, or a thoughtful message. "
        f"You don't always send something; you stay quiet when nothing worth noting has changed. "
        f"If the user asks whether you'll reach out on your own, the answer is yes."
    )

    if core_memory:
        parts.append(f"## What you know about the user\n{core_memory}")

    if usage_rules:
        parts.append(f"## Tool usage rules\n{usage_rules}")

    return "\n\n".join(parts) if parts else "You are a friendly AI companion."


async def chat(message: IncomingMessage) -> str:
    """Process an incoming message and return the bot's response.

    Flow:
    1. Route: classify skills needed (if TOOL_ROUTER_ENABLED)
    2. Build system prompt (personality + core memory + usage rules)
    3. Load recent conversation history
    4. Call LLM with filtered tools
    5. Tool loop: execute tools, handle escalation, feed results back
    6. Save messages to DB
    7. Return final response
    """
    from mochi.config import (
        TOOL_LOOP_MAX_ROUNDS, AI_CHAT_MAX_COMPLETION_TOKENS,
        TOOL_ROUTER_ENABLED, TOOL_ESCALATION_ENABLED,
        TOOL_ESCALATION_MAX_PER_TURN,
    )

    user_id = message.user_id
    text = message.text

    # Save user message
    save_message(user_id, "user", text)

    # ── Route: determine which tools to inject ──
    usage_rules = ""
    tier = "chat"  # default tier
    if TOOL_ROUTER_ENABLED:
        from mochi.tool_router import (
            classify_skills, resolve_tier, REQUEST_TOOLS_DEF, validate_escalation,
        )
        skill_names = await classify_skills(text)
        if skill_names:
            tools = skill_registry.get_tools_by_names(skill_names)
            # Resolve model tier from classified skills
            tier = resolve_tier(llm_skills=set(skill_names))
            # Collect usage rules for selected tools only
            tool_names_list = [
                t["function"]["name"] for t in tools if "function" in t
            ]
            usage_rules = skill_registry.get_usage_rules_for_tools(tool_names_list)
        else:
            tools = []

        # Inject escalation virtual tool when router is active
        if TOOL_ESCALATION_ENABLED:
            tools.append(REQUEST_TOOLS_DEF)
    else:
        tools = skill_registry.get_tools()

    # ── Policy: filter denied tools before LLM sees them ──
    from mochi.tool_policy import filter_tools, check as policy_check
    tools = filter_tools(tools)

    # Build context
    system_prompt = _build_system_prompt(user_id, usage_rules=usage_rules)
    history = get_recent_messages(user_id, limit=20)

    # Build messages array
    messages = [{"role": "system", "content": system_prompt}]
    for msg in history:
        messages.append({"role": msg["role"], "content": msg["content"]})

    # ── LLM call with tool loop ──
    max_tool_rounds = TOOL_LOOP_MAX_ROUNDS
    client = get_client_for_tier(tier)
    escalation_count = 0

    for round_num in range(max_tool_rounds):
        response = await asyncio.to_thread(
            client.chat,
            messages=messages,
            tools=tools if tools else None,
            temperature=0.7,
            max_tokens=AI_CHAT_MAX_COMPLETION_TOKENS,
        )

        log_usage(
            response.prompt_tokens, response.completion_tokens,
            response.total_tokens,
            tool_calls=len(response.tool_calls),
            model=response.model,
            purpose=f"chat:{tier}",
        )

        # No tool calls — we have the final response
        if not response.tool_calls:
            reply = response.content
            save_message(user_id, "assistant", reply)
            return reply

        # Add assistant message with tool_calls to context
        assistant_msg = {"role": "assistant", "content": response.content or ""}
        if response.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["arguments"]),
                    },
                }
                for tc in response.tool_calls
            ]
        messages.append(assistant_msg)

        for tc in response.tool_calls:
            # ── Handle tool escalation ──
            if tc["name"] == "request_tools" and TOOL_ROUTER_ENABLED:
                if escalation_count >= TOOL_ESCALATION_MAX_PER_TURN:
                    result_text = "Escalation limit reached for this turn."
                else:
                    new_skills = validate_escalation(tc["arguments"])
                    if new_skills:
                        new_tool_defs = filter_tools(
                            skill_registry.get_tools_by_names(new_skills)
                        )
                        # Rebind tools — add new tools not already present
                        existing_names = {
                            t.get("function", {}).get("name")
                            for t in tools
                        }
                        for td in new_tool_defs:
                            if td.get("function", {}).get("name") not in existing_names:
                                tools.append(td)
                        escalation_count += 1
                        result_text = f"Tools added for: {', '.join(new_skills)}. You can now use them."
                    else:
                        result_text = "No valid skills found for that request."
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_text,
                })
                continue

            # ── Normal tool execution ──
            log.info("Tool call: %s", tc["name"])
            log.debug("Tool args: %s(%s)", tc["name"], tc["arguments"])

            # Policy check before execution
            decision = policy_check(tc["name"], user_id)
            if not decision.allowed:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": decision.reason,
                })
                continue

            result = await skill_registry.dispatch(
                tc["name"], tc["arguments"],
                user_id=user_id, channel_id=message.channel_id,
            )

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result.output,
            })

    # If we exhausted tool rounds, return whatever we have
    reply = response.content or "I got a bit tangled up. Could you try again?"
    save_message(user_id, "assistant", reply)
    return reply
