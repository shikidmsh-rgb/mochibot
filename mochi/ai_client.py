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
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

from mochi.llm import get_client, get_client_for_tier, LLMResponse
from mochi.prompt_loader import get_prompt
from mochi.db import (
    save_message, get_recent_messages, get_core_memory, log_usage,
)
import mochi.skills as skill_registry
from mochi.transport import IncomingMessage

log = logging.getLogger(__name__)

STICKER_RE = re.compile(r"\[STICKER:([^\]]+)\]")


@dataclass
class ChatResult:
    """Result returned by chat() — text reply + optional sticker file_ids."""
    text: str = ""
    stickers: list[str] = field(default_factory=list)


def _build_system_prompt(user_id: int, usage_rules: str = "",
                         tool_names: list[str] | None = None) -> str:
    """Build the system prompt: personality(Chat) + heartbeat context + core memory.

    Args:
        user_id: Owner user ID for core memory lookup.
        usage_rules: Optional tool usage rules to inject (from pre-router).
        tool_names: Tool names available this turn (for dynamic context injection).
    """
    from mochi.config import HEARTBEAT_INTERVAL_MINUTES, TIMEZONE_OFFSET_HOURS
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
        f"while you're awake. You naturally wake when the user sends their first message "
        f"and go to sleep when they say goodnight or go silent at night. "
        f"The heartbeat observes context (time, conversation patterns, etc.) and sometimes decides to "
        f"proactively reach out — a check-in, a nudge, or a thoughtful message. "
        f"You don't always send something; you stay quiet when nothing worth noting has changed. "
        f"If the user asks whether you'll reach out on your own, the answer is yes."
    )

    if core_memory:
        parts.append(f"## What you know about the user\n{core_memory}")

    if usage_rules:
        parts.append(f"## Tool usage rules\n{usage_rules}")

    # Active habits (only when habit tools are available this turn)
    if user_id and tool_names:
        habit_tool_names = {"query_habit", "checkin_habit", "edit_habit"}
        if habit_tool_names & set(tool_names):
            try:
                from mochi.db import list_habits
                active_habits = list_habits(user_id)
                if active_habits:
                    habit_lines = "  ".join(
                        f"#{h['id']} {h['name']} ({h['frequency']})"
                        for h in active_habits
                    )
                    parts.append(f"## 习惯列表 (打卡用)\n{habit_lines}")
            except Exception:
                pass

    return "\n\n".join(parts) if parts else "You are a friendly AI companion."


async def chat(message: IncomingMessage) -> ChatResult:
    """Process an incoming message and return the bot's response.

    Flow:
    0. Sticker learning: if message carries sticker metadata, learn or rewrite
    1. Route: classify skills needed (if TOOL_ROUTER_ENABLED)
    2. Build system prompt (personality + core memory + usage rules)
    3. Load recent conversation history
    4. Call LLM with filtered tools
    5. Tool loop: execute tools, handle escalation, feed results back
    6. Save messages to DB
    7. Return ChatResult (text + optional sticker file_ids)
    """
    from mochi.config import (
        TOOL_LOOP_MAX_ROUNDS, AI_CHAT_MAX_COMPLETION_TOKENS,
        TOOL_ROUTER_ENABLED, TOOL_ESCALATION_ENABLED,
        TOOL_ESCALATION_MAX_PER_TURN,
    )

    user_id = message.user_id
    text = message.text
    pending_stickers: list[str] = []

    # ── Sticker learning: intercept sticker metadata from transport ──
    raw = message.raw or {}
    sticker_data = raw.get("sticker")
    if sticker_data and sticker_data.get("file_id"):
        from mochi.skills.sticker.handler import StickerSkill

        result = await StickerSkill().learn_sticker(
            user_id=user_id,
            file_id=sticker_data["file_id"],
            set_name=sticker_data.get("set_name", ""),
            emoji=sticker_data.get("emoji", ""),
            caption=text,
        )

        if result["learned"]:
            emoji = sticker_data.get("emoji", "")
            confirm = (
                f"学会了！{emoji} 标签：{result['tags']}\n"
                f"（已收集 {result['count']} 个贴纸）"
            )
            return ChatResult(text=confirm)

        # Already known — rewrite as text description for chat
        emoji = sticker_data.get("emoji", "")
        text = f"[用户发了一个贴纸 {emoji}]" + (f" {text}" if text else "")

    # Save user message
    save_message(user_id, "user", text)

    # ── Route: determine which tools to inject ──
    usage_rules = ""
    tier = "chat"  # default tier
    if TOOL_ROUTER_ENABLED:
        from mochi.tool_router import (
            classify_skills, resolve_tier, REQUEST_TOOLS_DEF, validate_escalation,
        )
        skill_names = await classify_skills(text, user_id=user_id)
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
    active_tool_names = [t["function"]["name"] for t in tools if "function" in t]
    system_prompt = _build_system_prompt(
        user_id, usage_rules=usage_rules, tool_names=active_tool_names,
    )
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
            reply = STICKER_RE.sub("", response.content or "").strip()
            save_message(user_id, "assistant", reply)
            return ChatResult(text=reply, stickers=pending_stickers)

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

            # Extract [STICKER:file_id] markers from tool result
            for m in STICKER_RE.finditer(result.output):
                pending_stickers.append(m.group(1).strip())

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result.output,
            })

    # If we exhausted tool rounds, return whatever we have
    reply = STICKER_RE.sub("", response.content or "").strip()
    reply = reply or "I got a bit tangled up. Could you try again?"
    save_message(user_id, "assistant", reply)
    return ChatResult(text=reply, stickers=pending_stickers)


async def chat_proactive(findings: list[dict], user_id: int) -> str | None:
    """Generate a proactive message using the Chat persona.

    Takes structured findings from Think (heartbeat) and passes them through
    the full Chat model with personality, core memory, and conversation history.
    The LLM decides how to express the findings — or skip them entirely.

    Returns:
        Generated message text, "[SKIP]" sentinel, or None on failure.
    """
    from mochi.config import (
        PROACTIVE_CHAT_MAX_TOKENS,
        PROACTIVE_CHAT_HISTORY_TURNS,
    )

    if not findings:
        return None

    # Format findings as bullet list
    lines = []
    for f in findings:
        topic = f.get("topic", "general")
        summary = f.get("summary", "")
        urgency = f.get("urgency", "")
        line = f"- [{topic}] {summary}"
        if urgency:
            line += f" (urgency={urgency})"
        lines.append(line)
    findings_text = "\n".join(lines)

    try:
        # Build system prompt (personality + core memory + time, no tools)
        system_prompt = _build_system_prompt(user_id)

        # Load conversation history (shorter window than regular chat)
        history = get_recent_messages(user_id, limit=PROACTIVE_CHAT_HISTORY_TURNS)

        # Load proactive_chat prompt and inject findings
        instruction = get_prompt("proactive_chat")
        if not instruction:
            log.warning("proactive_chat prompt not found")
            return None
        instruction = instruction.replace("{findings_text}", findings_text)

        # Assemble messages: system + history + instruction
        messages = [{"role": "system", "content": system_prompt}]
        for msg in history:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": instruction})

        # Call Chat model (no tools)
        client = get_client_for_tier("chat")
        response = await asyncio.to_thread(
            client.chat,
            messages=messages,
            temperature=0.7,
            max_tokens=PROACTIVE_CHAT_MAX_TOKENS,
        )

        log_usage(
            response.prompt_tokens, response.completion_tokens,
            response.total_tokens, model=response.model,
            purpose="proactive_chat",
        )

        reply = (response.content or "").strip()

        # Handle [SKIP] veto
        if "[SKIP]" in reply:
            log.info("chat_proactive: LLM vetoed (context-aware skip)")
            return "[SKIP]"

        if not reply:
            return None

        log.info("chat_proactive: generated from %d finding(s): %s",
                 len(findings), reply[:80])
        return reply

    except Exception as e:
        log.error("chat_proactive failed: %s", e, exc_info=True)
        return None
