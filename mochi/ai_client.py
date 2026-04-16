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
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

from mochi.llm import get_client_for_tier, LLMResponse
from mochi.prompt_loader import get_prompt, get_system_chat_modules
from mochi.db import (
    save_message, get_recent_messages, get_core_memory, log_usage,
    recall_memory,
)
from mochi.skills.habit.queries import list_habits
import mochi.skills as skill_registry
from mochi.transport import IncomingMessage

log = logging.getLogger(__name__)

STICKER_RE = re.compile(r"\[STICKER:([^\]]+)\]")

# Tools excluded from tool_history annotation — not meaningful skill executions
_TOOL_HISTORY_EXCLUDE = frozenset({"request_tools", "send_sticker"})

# ── Auto-recall state (per-user cooldown) ──
_user_last_recall: dict[int, float] = {}   # user_id → timestamp
_USER_LAST_RECALL_MAX = 100                # evict oldest when exceeded


def _retrieve_memories_for_turn(text: str, user_id: int) -> list[dict]:
    """Pre-turn automatic memory retrieval via embedding + hybrid search.

    Runs in parallel with router classification and DB fetches.
    Returns a list of relevant memory dicts, or [] on any failure.
    """
    from mochi.config import (
        MEMORY_AUTO_RECALL, MEMORY_AUTO_RECALL_TOP_K,
        MEMORY_AUTO_RECALL_MAX_ITEMS, MEMORY_AUTO_RECALL_MIN_VEC_SIM,
        MEMORY_AUTO_RECALL_MIN_SCORE, MEMORY_AUTO_RECALL_MAX_CHARS,
        MEMORY_AUTO_RECALL_COOLDOWN,
    )

    if not MEMORY_AUTO_RECALL or not user_id or not text or not text.strip():
        return []

    # Cooldown check
    if MEMORY_AUTO_RECALL_COOLDOWN > 0 and user_id in _user_last_recall:
        elapsed = time.time() - _user_last_recall[user_id]
        if elapsed < MEMORY_AUTO_RECALL_COOLDOWN:
            log.debug("auto-recall: cooldown skip (%.0fs < %ds)",
                      elapsed, MEMORY_AUTO_RECALL_COOLDOWN)
            return []

    try:
        from mochi.model_pool import get_pool
        query_emb = get_pool().embed(text)
        if query_emb is None:
            return []

        recalled = recall_memory(
            user_id, query=text,
            limit=max(1, MEMORY_AUTO_RECALL_TOP_K),
            query_embedding=query_emb,
            bump_access=False,
        )

        # Filter by quality gates
        max_chars = max(80, MEMORY_AUTO_RECALL_MAX_CHARS)
        selected: list[dict] = []
        for item in recalled:
            vec_sim = float(item.get("vec_sim") or 0.0)
            if vec_sim < MEMORY_AUTO_RECALL_MIN_VEC_SIM:
                continue
            raw_score = float(item.get("score") or 0.0)
            normalized = max(0.0, min(1.0, raw_score / 10.0))
            if normalized < MEMORY_AUTO_RECALL_MIN_SCORE:
                continue

            content = " ".join((item.get("content") or "").split())
            if len(content) > max_chars:
                content = content[:max_chars - 3].rstrip() + "..."

            selected.append({
                "text": content,
                "score": round(normalized, 2),
                "ts": str(item.get("updated_at") or item.get("created_at") or "")[:10],
                "category": str(item.get("category") or ""),
            })
            if len(selected) >= max(1, MEMORY_AUTO_RECALL_MAX_ITEMS):
                break

        # Update cooldown timestamp (evict oldest if bounded dict full)
        if len(_user_last_recall) >= _USER_LAST_RECALL_MAX:
            oldest = min(_user_last_recall, key=_user_last_recall.get)
            del _user_last_recall[oldest]
        _user_last_recall[user_id] = time.time()

        # KG entity context injection (high-precision, priority slots)
        from mochi.config import KG_ENABLED
        if KG_ENABLED:
            try:
                from mochi.knowledge_graph import find_matching_entities, entity_context_for_prompt
                matched = find_matching_entities(user_id, text)
                for ent_name in matched[:2]:
                    kg_text = entity_context_for_prompt(user_id, ent_name)
                    if kg_text:
                        selected.insert(0, {
                            "text": kg_text,
                            "score": 0.95,
                            "ts": "",
                            "category": "knowledge_graph",
                        })
            except Exception:
                pass  # non-critical, degrade gracefully

        # Final cap: memory items + up to 2 KG entities
        max_total = max(1, MEMORY_AUTO_RECALL_MAX_ITEMS) + 2
        selected = selected[:max_total]

        if selected:
            log.info("auto-recall: %d memories (top score=%.2f)",
                     len(selected), selected[0]["score"])
        return selected

    except Exception as e:
        log.warning("auto-recall failed (non-fatal): %s", e)
        return []


def _expand_history(history: list[dict]) -> list[dict]:
    """Expand conversation history into API-native messages.

    Each message is prefixed with its original timestamp so the LLM can
    distinguish "what was said then" from "what time is it now".

    For assistant messages with tool_history, reconstructs the tool call
    sequence so the LLM structurally recognizes prior tool usage:
      1. assistant message with tool_calls (content=None)
      2. tool result messages (one per tool, content="OK")
      3. assistant message with original reply text
    """
    messages: list[dict] = []
    for msg_idx, msg in enumerate(history):
        role = msg.get("role")
        content = msg.get("content")
        tool_history_raw = msg.get("tool_history")

        # Prefix content with timestamp so LLM sees when each message was sent
        created_at = msg.get("created_at", "")
        if created_at and content:
            ts = created_at[:16]  # "2026-04-16T18:28"
            content = f"[{ts}] {content}"

        if role == "assistant" and tool_history_raw:
            try:
                tool_history = json.loads(tool_history_raw)
            except (json.JSONDecodeError, TypeError):
                tool_history = []

            if tool_history:
                # 1. Assistant message with tool_calls
                tool_calls = []
                for t_idx, th in enumerate(tool_history):
                    call_id = f"hist_{msg_idx}_{t_idx}"
                    tool_calls.append({
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": th.get("name", "unknown"),
                            "arguments": "{}",
                        },
                    })
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tool_calls,
                })

                # 2. Tool result messages
                for t_idx, th in enumerate(tool_history):
                    call_id = f"hist_{msg_idx}_{t_idx}"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": "OK",
                    })

                # 3. Assistant message with original reply text
                messages.append({"role": "assistant", "content": content})
            else:
                messages.append({"role": role, "content": content})
        else:
            messages.append({"role": role, "content": content})
    return messages


@dataclass
class ChatResult:
    """Result returned by chat() — text reply + optional sticker file_ids."""
    text: str = ""
    stickers: list[str] = field(default_factory=list)


def _render_runtime_context(template: str, diary_status: str = "",
                            diary_journal: str = "") -> str:
    """Fill runtime_context.md placeholders. Remove sections with no data."""
    result = template

    if diary_status:
        result = result.replace("{{diary_status}}", diary_status)
    else:
        # Remove ### 状态速览 block
        result = re.sub(
            r"### 状态速览\n\{\{diary_status\}\}\n*", "", result,
        )

    if diary_journal:
        result = result.replace("{{diary_entry}}", diary_journal)
    else:
        # Remove ### 日记 block
        result = re.sub(
            r"### 日记\n\{\{diary_entry\}\}\n*", "", result,
        )

    # If both sub-sections removed, remove the entire ## 今日 header + intro
    result = re.sub(
        r"## 今日\n用户今天的状态与经历，由系统自动汇总。\n*$", "", result,
    )

    return result.strip()


def _build_system_prompt(user_id: int, usage_rules: str = "",
                         tool_names: list[str] | None = None,
                         core_memory: str = "",
                         habits: list[dict] | None = None,
                         transport: str = "",
                         recalled_memories: list[dict] | None = None,
                         diary_status: str = "",
                         diary_journal: str = "") -> str:
    """Build the system prompt using Zone A/B/C architecture.

    Zone A (primacy)  — identity & relationship (soul, user, core memory,
                        recalled memories)
    Zone B (reference) — capabilities & reference (agent, skills, usage rules,
                         habits, notes, bubble format)
    Zone C (recency)   — temporal context (diary, current time)

    Args:
        user_id: Owner user ID for core memory lookup.
        usage_rules: Optional tool usage rules to inject (from pre-router).
        tool_names: Tool names available this turn (for dynamic context injection).
        core_memory: Pre-fetched core memory string (avoids redundant DB call).
        habits: Pre-fetched habit list (avoids redundant DB call).
        transport: Transport name for transport-aware capability summary.
        recalled_memories: Auto-recalled memories to inject (from _retrieve_memories_for_turn).
        diary_status: Today's status panel (habits/todos/reminders progress).
        diary_journal: Today's journal entries.
    """
    from mochi.config import TIMEZONE_OFFSET_HOURS

    modules = get_system_chat_modules()

    # Current local time (respects TIMEZONE_OFFSET_HOURS)
    tz = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))
    now = datetime.now(tz)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    parts = []

    # ── Zone A: 身份与关系 (primacy — prompt 最前) ──────────────
    if "soul" in modules:
        parts.append(modules["soul"])

    if "user" in modules:
        parts.append(modules["user"])

    if core_memory:
        parts.append(f"## 你对用户的了解\n{core_memory}")

    if recalled_memories:
        lines = []
        for m in recalled_memories:
            lines.append(
                f"- [{m.get('ts', '')}] {m.get('category', '')} — "
                f"{m.get('text', '')}"
            )
        parts.append(
            "## 相关记忆\n"
            "以下是系统根据当前对话自动检索的历史片段，可能与当前话题相关：\n"
            + "\n".join(lines)
        )

    # ── Zone B: 能力与参考 (reference — 中间) ──────────────────
    if "agent" in modules:
        parts.append(modules["agent"])

    # Dynamic capability list (cached, refreshed on skill toggle)
    from mochi.skills import get_capability_summary
    cap = get_capability_summary(transport=transport)
    if cap:
        parts.append(cap)

    if usage_rules:
        parts.append(f"## 工具使用规则\n{usage_rules}")

    # Active habits (only when habit tools are available this turn)
    if user_id and tool_names and habits:
        habit_tool_names = {"query_habit", "checkin_habit", "edit_habit"}
        if habit_tool_names & set(tool_names):
            habit_lines = "  ".join(
                f"#{h['id']} {h['name']} ({h['frequency']})"
                for h in habits
            )
            if habit_lines:
                parts.append(f"## 习惯列表 (打卡用)\n{habit_lines}")

    # Notes (persistent working memory — via prompt section hook)
    for section in skill_registry.get_prompt_sections(compact=True):
        parts.append(section)

    # Bubble formatting instruction
    from mochi.config import BUBBLE_ENABLED
    if BUBBLE_ENABLED:
        bubble_inst = get_prompt("system_chat/_bubble")
        if bubble_inst:
            parts.append(bubble_inst)

    # [预留] conv_summary — 对话早期摘要，功能实现后在此注入

    # ── Zone C: 当下语境 (recency — prompt 最后) ────────────────
    if "runtime_context" in modules:
        rendered_rc = _render_runtime_context(
            modules["runtime_context"], diary_status, diary_journal,
        )
        if rendered_rc:
            parts.append(rendered_rc)

    # Current time — system block 末尾，利用 block 内 recency bias
    parts.append(f"当前时间：{now_str}")

    if not parts:
        raise RuntimeError("System prompt is empty — check prompts/ directory and prompt_loader")
    return "\n\n".join(parts)


async def chat(message: IncomingMessage) -> ChatResult:
    """Process an incoming message and return the bot's response.

    Flow:
    0. Sticker learning: if message carries sticker metadata, learn or rewrite
    1. Route: classify skills needed (if TOOL_ROUTER_ENABLED)
    1b. Auto-recall: embed user message → hybrid search → inject relevant memories
    2. Build system prompt (personality + core memory + recalled memories + usage rules)
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
        # Gate: skip sticker learning if skill is excluded for this transport
        sticker_skill = skill_registry.get_skill("sticker")
        sticker_excluded = (
            sticker_skill is not None
            and message.transport in sticker_skill.exclude_transports
        )
        if sticker_skill and not sticker_excluded:
            result = await sticker_skill.learn_sticker(
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

    # ── Parallel pre-fetch: router classification + DB queries ──
    usage_rules = ""
    tier = "chat"  # default tier

    # Pre-fetch habits (fast sync DB) — shared by router hint + system prompt
    habits = await asyncio.to_thread(list_habits, user_id)

    if TOOL_ROUTER_ENABLED:
        from mochi.tool_router import (
            classify_skills, resolve_tier, REQUEST_TOOLS_DEF, validate_escalation,
        )
        # Launch router (with habits hint) + remaining DB fetches concurrently
        skill_names, core_memory, history, recalled_memories = await asyncio.gather(
            classify_skills(text, user_id=user_id, habits=habits,
                            transport=message.transport),
            asyncio.to_thread(get_core_memory, user_id),
            asyncio.to_thread(get_recent_messages, user_id, 20),
            asyncio.to_thread(_retrieve_memories_for_turn, text, user_id),
        )

        # Always-on skills (declared in SKILL.md) + router-selected, deduplicated
        always_on = skill_registry.get_always_on_skill_names(
            transport=message.transport)
        all_skill_names = list(dict.fromkeys(always_on + skill_names))

        # Tier from router-selected skills only (always-on are lite,
        # shouldn't downgrade default "chat" tier for pure-chat messages)
        if skill_names:
            tier = resolve_tier(llm_skills=set(skill_names))

        # Tools from merged list
        tools = skill_registry.get_tools_by_names(
            all_skill_names, transport=message.transport)
        tool_names_list = [
            t["function"]["name"] for t in tools if "function" in t
        ]
        usage_rules = skill_registry.get_usage_rules_for_tools(tool_names_list)

        # Inject escalation virtual tool when router is active
        if TOOL_ESCALATION_ENABLED:
            tools.append(REQUEST_TOOLS_DEF)
    else:
        tools = skill_registry.get_tools(transport=message.transport)
        # Parallel DB fetches even when router is off
        core_memory, history, recalled_memories = await asyncio.gather(
            asyncio.to_thread(get_core_memory, user_id),
            asyncio.to_thread(get_recent_messages, user_id, 20),
            asyncio.to_thread(_retrieve_memories_for_turn, text, user_id),
        )

    # ── Policy: filter denied tools before LLM sees them ──
    from mochi.tool_policy import filter_tools, check as policy_check
    tools = filter_tools(tools)

    # Build context
    active_tool_names = [t["function"]["name"] for t in tools if "function" in t]

    # Fetch diary data for Zone C runtime context
    # Only journal (events) — status panel (habits/todos) excluded from chat
    # to avoid LLM parroting progress in every reply. Status is available
    # via tools (query_habit, manage_todo) when the user asks.
    from mochi.diary import diary as _diary
    _dj = _diary.read(section="今日日記")

    system_prompt = _build_system_prompt(
        user_id, usage_rules=usage_rules, tool_names=active_tool_names,
        core_memory=core_memory, habits=habits, transport=message.transport,
        recalled_memories=recalled_memories,
        diary_status="", diary_journal=_dj,
    )

    # Build messages array
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(_expand_history(history))

    # ── LLM call with tool loop ──
    max_tool_rounds = TOOL_LOOP_MAX_ROUNDS
    client = get_client_for_tier(tier)
    escalation_count = 0
    tool_names_used: list[str] = []  # track for tool_history persistence
    on_interim = message.on_interim

    for round_num in range(max_tool_rounds):
        for _attempt in range(2):
            try:
                response = await asyncio.to_thread(
                    client.chat,
                    messages=messages,
                    tools=tools if tools else None,
                    temperature=0.7,
                    max_tokens=AI_CHAT_MAX_COMPLETION_TOKENS,
                )
                break
            except Exception as e:
                if _attempt == 0:
                    log.warning("LLM call failed (attempt 1), retrying: %s", e)
                    continue
                log.error("LLM call failed (attempt 2): %s", e, exc_info=True)
                return ChatResult(text=f"API 报错：{e}")

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
            tool_history_json = (
                json.dumps([{"name": n} for n in tool_names_used], ensure_ascii=False)
                if tool_names_used else None
            )
            save_message(user_id, "assistant", reply, tool_history=tool_history_json)
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
                            skill_registry.get_tools_by_names(
                                new_skills, transport=message.transport)
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

            # Notify transport of tool execution (status UX)
            if on_interim:
                try:
                    await on_interim(None, tool_name=tc["name"])
                except Exception:
                    pass

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
                transport=message.transport,
            )

            # Record tool name for history (exclude internal-only tools)
            if tc["name"] not in _TOOL_HISTORY_EXCLUDE:
                tool_names_used.append(tc["name"])

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
    reply = reply or "处理过程出了点问题，你再说一次试试？"
    tool_history_json = (
        json.dumps([{"name": n} for n in tool_names_used], ensure_ascii=False)
        if tool_names_used else None
    )
    save_message(user_id, "assistant", reply, tool_history=tool_history_json)
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
        core_memory = get_core_memory(user_id)
        system_prompt = _build_system_prompt(user_id, core_memory=core_memory)

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
        messages.extend(_expand_history(history))
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

    except Exception:
        # Let exceptions bubble up to _dispatch_proactive for recording
        # in heartbeat_log (observability: proactive_failed needs a reason).
        raise


# ═══════════════════════════════════════════════════════════════════════════
# Bedtime Tidy — evening review with tools (notes/todos)
# ═══════════════════════════════════════════════════════════════════════════

_last_bedtime_tidy_date: str = ""


async def chat_bedtime_tidy(
    findings: list[dict],
    user_id: int,
) -> str | None:
    """Bedtime review — tidy todos, clean notes, say goodnight.

    Same pattern as chat_proactive but with its own tools, timeout, and prompt.
    Injects notes.md into context so the LLM can see current notes.
    Only runs once per calendar day to prevent duplicate tidying.
    """
    global _last_bedtime_tidy_date
    from mochi.config import (
        BEDTIME_TIDY_MAX_TOKENS,
        BEDTIME_TIDY_TOOLS,
        BEDTIME_TIDY_MAX_ROUNDS,
        PROACTIVE_CHAT_HISTORY_TURNS,
        TZ,
    )

    local_today = datetime.now(TZ).strftime("%Y-%m-%d")
    if _last_bedtime_tidy_date == local_today:
        log.info("bedtime_tidy already ran today (%s), skipping", local_today)
        return None

    # Format findings
    lines = []
    for f in findings:
        line = f"- [{f.get('topic', '?')}] {f.get('summary', '?')}"
        lines.append(line)
    findings_text = "\n".join(lines)

    try:
        # Build system prompt (same soul + runtime context as chat)
        core_memory = get_core_memory(user_id)
        system_prompt = _build_system_prompt(user_id, core_memory=core_memory)

        # Inject notes via prompt section hook (full, not compact)
        for section in skill_registry.get_prompt_sections(compact=False):
            system_prompt += f"\n\n{section}\n"

        # Load history for context awareness
        history = get_recent_messages(user_id, limit=PROACTIVE_CHAT_HISTORY_TURNS)

        # Load bedtime tidy instruction and inject findings
        instruction = get_prompt("bedtime_tidy")
        if not instruction:
            log.warning("bedtime_tidy prompt not found")
            return None
        instruction = instruction.replace("{findings_text}", findings_text)

        # Assemble messages: system + history + instruction
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(_expand_history(history))
        messages.append({"role": "user", "content": instruction})

        # Resolve tools from skill registry
        tools = skill_registry.get_tools_by_names(BEDTIME_TIDY_TOOLS)
        tool_name_list = [t["function"]["name"] for t in tools] if tools else []
        log.info("chat_bedtime_tidy: findings=%d, history=%d, tools=%s",
                 len(findings), len(history), tool_name_list)

        client = get_client_for_tier("chat")

        # Tool loop (sequential rounds)
        for round_num in range(BEDTIME_TIDY_MAX_ROUNDS):
            response = await asyncio.to_thread(
                client.chat,
                messages=messages,
                tools=tools if tools else None,
                temperature=0.7,
                max_tokens=BEDTIME_TIDY_MAX_TOKENS,
            )

            log_usage(
                response.prompt_tokens, response.completion_tokens,
                response.total_tokens,
                tool_calls=len(response.tool_calls),
                model=response.model,
                purpose="bedtime_tidy",
            )

            if not response.tool_calls:
                break

            # Process tool calls
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
                log.info("bedtime_tidy tool: %s(%s)",
                         tc["name"], json.dumps(tc["arguments"], ensure_ascii=False)[:100])
                result = await skill_registry.dispatch(
                    tc["name"], tc["arguments"],
                    user_id=user_id, channel_id=0,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result.output or "No result",
                })

        reply = (response.content or "").strip()

        if "[SKIP]" in reply:
            log.info("bedtime_tidy vetoed by LLM")
            return "[SKIP]"

        if not reply:
            return None

        log.info("bedtime_tidy generated: %s", reply[:60])
        _last_bedtime_tidy_date = local_today
        return reply

    except Exception as e:
        log.error("chat_bedtime_tidy failed: %s", e, exc_info=True)
        return None
