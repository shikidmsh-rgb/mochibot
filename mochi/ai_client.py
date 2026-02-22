"""AI client — orchestrates LLM chat with tool dispatch and memory context.

This is the "brain" that ties together:
- LLM provider (chat completions)
- Skill registry (tool execution)
- Memory (core memory in system prompt, extraction after conversations)
- Prompt loader (system personality)
"""

import json
import logging
from datetime import datetime, timezone, timedelta

from mochi.llm import get_client, LLMResponse
from mochi.prompt_loader import get_full_prompt
from mochi.db import (
    save_message, get_recent_messages, get_core_memory, log_usage,
)
import mochi.skills as skill_registry
from mochi.transport import IncomingMessage

log = logging.getLogger(__name__)


def _build_system_prompt(user_id: int) -> str:
    """Build the system prompt: personality(Chat) + heartbeat context + core memory."""
    from mochi.config import HEARTBEAT_INTERVAL_MINUTES, AWAKE_HOUR_START, AWAKE_HOUR_END, TIMEZONE_OFFSET_HOURS
    personality = get_full_prompt("system_chat", "Chat")
    core_memory = get_core_memory(user_id)

    # Current local time (respects TIMEZONE_OFFSET_HOURS)
    tz = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))
    now = datetime.now(tz)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S %Z")

    parts = []
    if personality:
        parts.append(personality)

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

    return "\n\n".join(parts) if parts else "You are a friendly AI companion."


async def chat(message: IncomingMessage) -> str:
    """Process an incoming message and return the bot's response.

    Flow:
    1. Build system prompt (personality + core memory)
    2. Load recent conversation history
    3. Call LLM with tools
    4. If tool_calls: execute tools → feed results back → call LLM again
    5. Save messages to DB
    6. Return final response
    """
    user_id = message.user_id
    text = message.text

    # Save user message
    save_message(user_id, "user", text)

    # Build context
    system_prompt = _build_system_prompt(user_id)
    history = get_recent_messages(user_id, limit=20)
    tools = skill_registry.get_tools()

    # Build messages array
    messages = [{"role": "system", "content": system_prompt}]
    for msg in history:
        messages.append({"role": msg["role"], "content": msg["content"]})

    # LLM call (with tool loop)
    max_tool_rounds = 5
    client = get_client()

    for round_num in range(max_tool_rounds):
        response = client.chat(
            messages=messages,
            tools=tools if tools else None,
            temperature=0.7,
        )

        log_usage(
            response.prompt_tokens, response.completion_tokens,
            response.total_tokens,
            tool_calls=len(response.tool_calls),
            model=response.model,
            purpose="chat",
        )

        # No tool calls — we have the final response
        if not response.tool_calls:
            reply = response.content
            save_message(user_id, "assistant", reply)
            return reply

        # Execute tool calls
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
            # Log tool name only — arguments may contain personal data
            log.info("Tool call: %s", tc["name"])
            log.debug("Tool args: %s(%s)", tc["name"], tc["arguments"])
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
