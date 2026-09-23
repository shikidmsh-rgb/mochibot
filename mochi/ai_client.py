"""AI client — orchestrates LLM chat with tool dispatch and memory context.

This is the "brain" that ties together:
- LLM provider (chat completions)
- Skill registry (tool execution)
- Memory (core memory in system prompt, extraction after conversations)
- Prompt loader (system personality)
"""

import asyncio
import hashlib
import json
import logging
import os
import platform
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from mochi.llm import get_client_for_tier, LLMResponse
from mochi.prompt_loader import get_prompt, get_system_chat_modules
from mochi.db import (
    save_message, save_message_once, log_usage,
    recall_memory, mark_memory_items_accessed, get_conversation_context,
    start_tool_execution, finish_tool_execution,
)
from mochi.core_store import read_core
from mochi.skills.habit.queries import list_habits
from mochi.main_runtime import (
    BEDTIME_ROUTED_SKILLS,
    ContextPolicy,
    DurableChatResult,
    MainRuntimeEntry,
    context_policy,
)
from mochi.request_tools import ToolLoopBudget, resolve_request
from mochi.skills.base import SkillResult
from mochi.token_estimator import estimate_tokens
from mochi.tool_execution import model_result_for
from mochi.tool_availability import (
    ToolAvailability,
    tool_call_error,
    unavailable_tool_error,
)
from mochi.bedtime_tool import ENTER_BEDTIME_DEF, ENTER_BEDTIME_TOOL_NAME
import mochi.skills as skill_registry
from mochi.transport import IncomingMessage, ImageAttachment

log = logging.getLogger(__name__)

STICKER_RE = re.compile(r"\[STICKER:([^\]]+)\]")

# Tools excluded from tool_history annotation — not meaningful skill executions
_TOOL_HISTORY_EXCLUDE = frozenset({
    "request_tools", "send_sticker", ENTER_BEDTIME_TOOL_NAME,
})


def _deployment_environment() -> str:
    system = platform.system() or "Unknown"
    if os.path.exists("/.dockerenv"):
        return f"Docker 容器（{system}）"
    return {
        "Windows": "Windows 环境",
        "Linux": "Linux 环境",
        "Darwin": "macOS 环境",
    }.get(system, "其他系统环境")


def _image_content(text: str, image: ImageAttachment) -> list[dict]:
    """Build the framework's OpenAI-style canonical multimodal content."""
    return [
        {"type": "text", "text": text},
        {
            "type": "image_url",
            "image_url": {"url": image.data_url(), "detail": "auto"},
        },
    ]


def _replace_current_user_with_image(
    messages: list[dict], stored_text: str, text: str, image: ImageAttachment,
) -> None:
    """Attach an image to the just-saved user turn without persisting bytes."""
    content = _image_content(text, image)
    if messages:
        message = messages[-1]
        existing = message.get("content")
        if (message.get("role") == "user"
                and isinstance(existing, str)
                and existing == stored_text):
            message["content"] = content
            return
    # Defensive fallback for tests or custom DB adapters that omit the new row.
    messages.append({"role": "user", "content": content})

# ── Auto-recall state (per-user cooldown) ──
_user_last_recall: dict[int, tuple[float, str]] = {}
_USER_LAST_RECALL_MAX = 100                # evict oldest when exceeded


def _format_recalled_memories(memories: list[dict]) -> str:
    lines = []
    for memory in memories:
        start = memory.get("evidence_start", "")
        end = memory.get("evidence_end", "")
        if start and end and start != end:
            prefix = f"[用户于 {start} 至 {end} 提到] "
        elif start:
            prefix = f"[用户于 {start} 提到] "
        else:
            prefix = ""
        lines.append(f"- {prefix}{memory.get('text', '')}")
    return (
        "## 相关记忆\n"
        "以下是系统根据当前对话自动检索的历史片段，可能与当前话题相关：\n"
        + "\n".join(lines)
    )


def _memory_recall_queries(text: str, user_id: int) -> list[str]:
    queries = [text.strip()]
    context = get_conversation_context(
        user_id, 3, include_summary=False, include_standalone=False,
    )
    recent = context["recent"]
    selected = [message for message in recent if message["role"] == "user"][-3:]
    assistants = [message for message in recent if message["role"] == "assistant"]
    if assistants:
        selected.append(assistants[-1])
    selected.sort(key=lambda message: message["id"])
    labels = {"user": "用户", "assistant": "Mochi"}
    lines = [
        f"[{labels[message['role']]}] {' '.join(message['content'].split())[:500]}"
        for message in selected if message["content"].strip()
    ]
    if lines:
        queries.append(
            "最近已完成对话：\n" + "\n".join(lines)
            + f"\n[当前用户] {text.strip()}"
        )
    return queries


def _record_recalled_memories_exposed(user_id: int, memories: list[dict]) -> None:
    try:
        mark_memory_items_accessed(
            user_id, [item["memory_id"] for item in memories if "memory_id" in item],
        )
    except sqlite3.Error as exc:
        log.warning("Memory reference accounting failed: %s", exc)
    query_key = next(
        (item["_recall_query_key"] for item in memories if "_recall_query_key" in item),
        None,
    )
    if query_key is not None:
        if len(_user_last_recall) >= _USER_LAST_RECALL_MAX:
            oldest = min(_user_last_recall, key=lambda uid: _user_last_recall[uid][0])
            del _user_last_recall[oldest]
        _user_last_recall[user_id] = (time.time(), query_key)


def _retrieve_memories_for_turn(text: str, user_id: int) -> list[dict]:
    """Fuse current-topic and complete-conversation recall without another LLM."""
    from mochi.config import (
        MEMORY_AUTO_RECALL, MEMORY_AUTO_RECALL_TOP_K,
        MEMORY_AUTO_RECALL_MAX_ITEMS, MEMORY_AUTO_RECALL_MIN_VEC_SIM,
        MEMORY_AUTO_RECALL_MAX_CHARS, MEMORY_AUTO_RECALL_MAX_TOKENS,
        MEMORY_AUTO_RECALL_COOLDOWN,
    )

    if not MEMORY_AUTO_RECALL or not user_id or not text or not text.strip():
        return []

    try:
        queries = _memory_recall_queries(text, user_id)
    except sqlite3.Error as exc:
        log.warning("auto-recall continuity unavailable: %s", exc)
        queries = [text.strip()]
    query_key = hashlib.sha256("\0".join(queries).encode("utf-8")).hexdigest()
    if MEMORY_AUTO_RECALL_COOLDOWN > 0 and user_id in _user_last_recall:
        recalled_at, previous_key = _user_last_recall[user_id]
        elapsed = time.time() - recalled_at
        if elapsed < MEMORY_AUTO_RECALL_COOLDOWN and previous_key == query_key:
            log.debug("auto-recall: cooldown skip (%.0fs < %ds)",
                      elapsed, MEMORY_AUTO_RECALL_COOLDOWN)
            return []

    embeddings = [None] * len(queries)
    try:
        from mochi.model_pool import get_pool
        embeddings = get_pool().embed_batch(queries)
        if len(embeddings) != len(queries):
            raise ValueError("auto-recall embedding count mismatch")
    except Exception as exc:
        log.warning(
            "auto-recall embedding failed; using keyword search: %s", exc,
        )
        embeddings = [None] * len(queries)

    try:
        recalled = []
        for query, embedding in zip(queries, embeddings):
            items = recall_memory(
                user_id, query=query,
                limit=max(1, MEMORY_AUTO_RECALL_TOP_K),
                query_embedding=embedding,
                bump_access=False,
            )
            recalled.extend(items)

        max_chars = max(80, MEMORY_AUTO_RECALL_MAX_CHARS)
        candidates: list[dict] = []
        seen_ids: set[int] = set()
        for item in recalled:
            vec_sim = float(item.get("vec_sim") or 0.0)
            match_source = str(item.get("match_source") or "")
            text_hit = bool(item.get("fts_hit")) or match_source in {
                "fts", "like", "hybrid",
            }
            vector_hit = bool(item.get("has_vector")) and (
                vec_sim >= MEMORY_AUTO_RECALL_MIN_VEC_SIM
            )
            if not text_hit and not vector_hit:
                continue
            if item["id"] in seen_ids:
                continue

            content = " ".join((item.get("content") or "").split())
            if not content:
                continue
            seen_ids.add(item["id"])
            if len(content) > max_chars:
                content = content[:max_chars - 3].rstrip() + "..."
            raw_score = float(item.get("score") or 0.0)
            candidates.append({
                "memory_id": item["id"],
                "text": content,
                "score": round(max(0.0, min(1.0, raw_score / 10.0)), 2),
                "evidence_start": str(item.get("evidence_start") or "")[:10],
                "evidence_end": str(item.get("evidence_end") or "")[:10],
            })

        from mochi.config import KG_ENABLED
        if KG_ENABLED:
            try:
                from mochi.knowledge_graph import find_matching_entities, entity_context_for_prompt
                matched = find_matching_entities(user_id, text)
                kg_candidates = []
                for ent_name in matched[:2]:
                    kg_text = entity_context_for_prompt(user_id, ent_name)
                    if kg_text:
                        kg_candidates.append({
                            "text": kg_text,
                            "score": 0.95,
                            "evidence_start": "",
                            "evidence_end": "",
                        })
                candidates = kg_candidates + candidates
            except Exception:
                pass  # non-critical, degrade gracefully

        max_total = max(1, MEMORY_AUTO_RECALL_MAX_ITEMS)
        max_tokens = max(1, MEMORY_AUTO_RECALL_MAX_TOKENS)
        selected: list[dict] = []
        for candidate in candidates:
            proposed = selected + [candidate]
            if estimate_tokens(_format_recalled_memories(proposed)) > max_tokens:
                continue
            candidate["_recall_query_key"] = query_key
            selected = proposed
            if len(selected) >= max_total:
                break

        if not selected:
            return []
        log.info(
            "auto-recall: %d memories (top score=%.2f)",
            len(selected),
            selected[0]["score"],
        )
        return selected
    except Exception as exc:
        log.warning("auto-recall failed (non-fatal): %s", exc)
        return []


_WEEKDAY_NAMES = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def _format_history_timestamp(created_at) -> str:
    """Format a message timestamp as local `YYYY-MM-DD HH:MM` metadata.

    Returns empty string on missing/invalid input.
    """
    if not created_at:
        return ""
    from mochi.config import TZ
    tz = TZ
    try:
        dt = datetime.fromisoformat(str(created_at))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        else:
            dt = dt.astimezone(tz)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return ""


def _format_history_timestamps(history: list[dict]) -> str:
    lines = []
    for index, msg in enumerate(history, start=1):
        timestamp = _format_history_timestamp(msg.get("created_at"))
        if timestamp and msg.get("role") in {"user", "assistant"}:
            lines.append(f"{index}. {msg['role']}: {timestamp}")
    return "\n".join(lines)


def _expand_history(history: list[dict]) -> list[dict]:
    """Preserve stored speech without inserting metadata into either role.

    Stored tool history is not replayed as provider-native tool calls;
    real executions are kept in the tool execution ledger.
    """
    messages: list[dict] = []
    for msg in history:
        role = msg.get("role")
        content = msg.get("content")
        expanded = {"role": role, "content": content}
        if role == "assistant" and "reasoning_content" in msg:
            expanded["reasoning_content"] = msg["reasoning_content"]
        messages.append(expanded)
    return messages


def _schedule_continuous_memory(user_id: int) -> None:
    """Wake both non-blocking Layer 2 coordinators after one eligible turn."""
    from mochi.conversation_summary import schedule_conversation_summary
    from mochi.memory_extraction import schedule_memory_extraction

    schedule_conversation_summary(user_id)
    schedule_memory_extraction(user_id)


@dataclass
class ChatResult:
    """Result returned by chat() — text reply + optional sticker file_ids."""
    text: str = ""
    stickers: list[str] = field(default_factory=list)
    tool_audit: list[dict] = field(default_factory=list)
    successful_effects: bool = False
    bedtime_requested: bool = False
    disposition: str = "deliver"
    _pending_history: dict | None = field(default=None, repr=False)
    _delivery_confirmed: bool = field(default=False, init=False, repr=False)
    _after_delivery: list[Callable[[], None]] = field(default_factory=list, repr=False)
    _final_delivery_confirmed: bool = field(default=False, init=False, repr=False)

    def confirm_delivered(self, *, final: bool = False) -> bool:
        """Persist text once; lifecycle actions also require the whole reply."""
        confirmed = False
        if not self._delivery_confirmed and self._pending_history:
            pending = self._pending_history
            processed = bool(pending.get("processed", True))
            inserted = save_message_once(
                pending["user_id"], "assistant", pending["content"],
                tool_history=pending["tool_history"], turn_id=pending["turn_id"],
                processed=processed, reasoning_content=pending.get("reasoning_content"),
                reasoning_source=pending.get("reasoning_source", ""),
            )
            self._delivery_confirmed = True
            confirmed = True
            if inserted and not processed:
                _schedule_continuous_memory(pending["user_id"])
        if final and not self._final_delivery_confirmed:
            self._final_delivery_confirmed = True
            callbacks, self._after_delivery = self._after_delivery, []
            for callback in callbacks:
                callback()
            confirmed = confirmed or bool(callbacks)
        return confirmed

    def to_durable(self) -> DurableChatResult:
        return DurableChatResult(
            text=self.text,
            stickers=tuple(self.stickers),
            pending_history=self._pending_history,
            tool_audit=tuple(self.tool_audit),
            successful_effects=self.successful_effects,
            disposition=self.disposition,
        )

    @classmethod
    def from_durable(cls, result: DurableChatResult) -> "ChatResult":
        return cls(
            text=result.text,
            stickers=list(result.stickers),
            tool_audit=list(result.tool_audit),
            successful_effects=result.successful_effects,
            disposition=result.disposition,
            _pending_history=result.pending_history,
        )


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


def _build_system_prompt(user_id: int, capability_context: str = "",
                         tool_names: list[str] | None = None,
                         core_memory: str = "",
                         habits: list[dict] | None = None,
                         transport: str = "",
                         recalled_memories: list[dict] | None = None,
                         diary_status: str = "",
                         diary_journal: str = "",
                         diary_tomorrow: str = "",
                         conv_summary: str = "",
                         recent_operations: str = "",
                         runtime_entry: MainRuntimeEntry | None = None,
                         weekly_context: str = "",
                         policy: ContextPolicy | None = None,
                         habit_progress_context: str = "",
                         history_timestamps: str = "") -> str:
    """Assemble explicit identity, situation, capability, and live-context zones."""

    modules = get_system_chat_modules()
    from mochi.config import TZ
    now = datetime.now(TZ)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S %z") + f" {_WEEKDAY_NAMES[now.weekday()]}"
    policy = policy or context_policy(runtime_entry)
    is_weekly = bool(
        runtime_entry and runtime_entry.kind == "weekly_maintenance"
    )
    is_autonomous = bool(
        runtime_entry and runtime_entry.kind == "free_time"
    )

    stable_identity = []
    if core_memory:
        stable_identity.append(core_memory)
    if "agent" in modules:
        stable_identity.append(
            modules["agent"].replace(
                "{{deployment_environment}}",
                _deployment_environment(),
            )
        )
    early_runtime_situation = []
    if policy.early_runtime_situation and runtime_entry:
        situation = get_prompt("free_time_entry")
        if not situation:
            raise RuntimeError(f"{runtime_entry.kind} entry prompt is missing")
        early_runtime_situation.append(situation)
        protocol = get_prompt("runtime_silence_protocol")
        if not protocol:
            raise RuntimeError("Runtime silence protocol prompt is missing")
        early_runtime_situation.append(protocol)

    capability_parts = []
    if not is_weekly:
        from mochi.skills import get_capability_summary
        cap = get_capability_summary(transport=transport)
        if cap:
            capability_parts.append(cap)

    if capability_context:
        capability_parts.append(f"## 能力上下文\n{capability_context}")

    if habit_progress_context:
        capability_parts.append(
            f"## 本轮习惯进度快照（只读事实）\n{habit_progress_context}"
        )
    elif tool_names and habits:
        from mochi.skills.habit.logic import describe_frequency
        habit_tool_names = {"habit_progress", "edit_habit"}
        if habit_tool_names & set(tool_names):
            habit_lines = "  ".join(
                f"#{h['id']} {h['name']} ({describe_frequency(h['frequency'])})"
                for h in habits
            )
            if habit_lines:
                capability_parts.append(f"## 习惯列表 (打卡用)\n{habit_lines}")

    if policy.prompt_sections and not is_weekly:
        for section in skill_registry.get_prompt_sections(compact=True):
            capability_parts.append(section)

    from mochi.config import BUBBLE_ENABLED
    if BUBBLE_ENABLED and not is_weekly and not is_autonomous:
        bubble_inst = get_prompt("system_chat/_bubble")
        if bubble_inst:
            capability_parts.append(bubble_inst)

    dynamic_live_context = []
    if history_timestamps and not is_weekly and policy.recent_history:
        hist_ts_inst = get_prompt("system_chat/_history_timestamp")
        if hist_ts_inst:
            dynamic_live_context.append(
                hist_ts_inst.replace("{{history_timestamps}}", history_timestamps)
            )
    if "runtime_context" in modules:
        rendered_rc = _render_runtime_context(
            modules["runtime_context"], diary_status, diary_journal,
        )
        if rendered_rc:
            dynamic_live_context.append(rendered_rc)

    if conv_summary:
        dynamic_live_context.append(f"## 本次对话早期内容（摘要）\n{conv_summary}")

    if recent_operations:
        dynamic_live_context.append(recent_operations)

    if runtime_entry and runtime_entry.kind == "bedtime" and diary_tomorrow:
        dynamic_live_context.append("## 明日日记草稿\n" + diary_tomorrow)

    if recalled_memories:
        dynamic_live_context.append(
            _format_recalled_memories(recalled_memories)
        )

    if runtime_entry and runtime_entry.kind == "bedtime":
        bedtime_context = get_prompt("bedtime_entry")
        if not bedtime_context:
            raise RuntimeError("Bedtime entry prompt is missing")
        trigger_labels = {
            "explicit": "用户刚刚亲自表达了晚安或准备睡觉",
            "silence": "夜间持续安静后，系统判断用户大概已经睡着",
            "resleep": "用户夜里短暂醒来后再次安静下来",
        }
        dynamic_live_context.append(
            bedtime_context.replace(
                "{{trigger}}",
                trigger_labels[runtime_entry.trigger],
            )
        )
    elif runtime_entry and runtime_entry.kind == "self_reminder":
        reminder_context = get_prompt("self_reminder_entry")
        if not reminder_context:
            raise RuntimeError("Self reminder entry prompt is missing")
        dynamic_live_context.append(
            reminder_context.replace(
                "{{intent}}", runtime_entry.intent or "",
            ).replace(
                "{{scheduled_for}}", runtime_entry.scheduled_for or "",
            )
        )
    elif is_weekly:
        weekly_prompt = get_prompt("weekly_maintenance_entry")
        if not weekly_prompt:
            raise RuntimeError("Weekly maintenance entry prompt is missing")
        dynamic_live_context.append(
            weekly_prompt.replace("{{weekly_context}}", weekly_context)
        )

    from mochi.db import get_last_user_message_time
    last_msg_time = (
        get_last_user_message_time(user_id)
        if policy.temporal_context and not is_weekly
        else None
    )
    if last_msg_time:
        try:
            last_dt = datetime.fromisoformat(last_msg_time)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=TZ)
            silence_mins = int((now - last_dt).total_seconds() / 60)
            if silence_mins < 2:
                silence_label = "刚刚"
            elif silence_mins < 60:
                silence_label = f"{silence_mins}分钟前"
            else:
                silence_hours = silence_mins // 60
                if silence_hours < 24:
                    silence_label = f"{silence_hours}小时前"
                else:
                    silence_label = f"{silence_hours // 24}天前"
            dynamic_live_context.append(f"用户上次发消息：{silence_label}")
        except (ValueError, TypeError):
            pass

    parts = (
        stable_identity
        + early_runtime_situation
        + capability_parts
        + dynamic_live_context
        + [f"当前时间：{now_str}"]
    )
    if not parts:
        raise RuntimeError("System prompt is empty — check prompts/ directory and prompt_loader")
    return "\n\n".join(parts)


async def chat(
    message: IncomingMessage | None = None,
    *,
    runtime_entry: MainRuntimeEntry | None = None,
) -> ChatResult:
    """Process an incoming message and return the bot's response.

    Flow:
    0. Sticker learning: if message carries sticker metadata, learn or rewrite
    1. Route: classify skills needed (if TOOL_ROUTER_ENABLED)
    1b. Auto-recall: embed user message → hybrid search → inject relevant memories
    2. Build system prompt (personality + memory + available capability context)
    3. Load recent conversation history
    4. Call LLM with filtered tools
    5. Tool loop: execute tools, handle escalation, feed results back
    6. Save messages to DB
    7. Return ChatResult (text + optional sticker file_ids)
    """
    from mochi.config import (
        TOOL_LOOP_MAX_ROUNDS, AI_CHAT_MAX_COMPLETION_TOKENS,
        TOOL_ROUTER_ENABLED, TOOL_ESCALATION_ENABLED,
        TOOL_ESCALATION_MAX_PER_TURN, TOOL_LOOP_TOTAL_TOOL_LIMIT,
        TOOL_LOOP_PER_TOOL_LIMIT,
    )

    runtime_entry = runtime_entry or (
        message.runtime_entry if message is not None else None
    )
    if message is None and runtime_entry is None:
        raise ValueError("chat requires an incoming message or runtime entry")
    if (
        message is not None
        and runtime_entry is not None
        and runtime_entry.kind in {"self_reminder", "free_time"}
    ):
        raise ValueError(f"{runtime_entry.kind} runtime entries are system-only")

    user_id = message.user_id if message is not None else runtime_entry.user_id
    channel_id = (
        message.channel_id if message is not None else runtime_entry.channel_id
    )
    transport = (
        message.transport if message is not None else runtime_entry.transport
    )
    text = (
        message.text
        if message is not None
        else runtime_entry.intent or ""
    )
    image = message.image if message is not None else None
    is_bedtime = bool(runtime_entry and runtime_entry.kind == "bedtime")
    is_self_reminder = bool(
        runtime_entry and runtime_entry.kind == "self_reminder"
    )
    is_weekly = bool(
        runtime_entry and runtime_entry.kind == "weekly_maintenance"
    )
    is_autonomous = bool(
        runtime_entry and runtime_entry.kind == "free_time"
    )
    prompt_policy = context_policy(runtime_entry)
    turn_id = (
        runtime_entry.idempotency_key
        if (is_self_reminder or is_weekly or is_autonomous)
        and runtime_entry.idempotency_key
        else uuid.uuid4().hex
    )
    pending_stickers: list[str] = []
    after_delivery: list[Callable[[], None]] = []
    pending_exposure_ids: set[int] = set()
    exposed_memory_ids: set[int] = set()

    # ── Sticker learning: intercept sticker metadata from transport ──
    raw = message.raw or {} if message is not None else {}
    sticker_data = raw.get("sticker")
    if sticker_data and sticker_data.get("file_id"):
        # Gate: skip sticker learning if skill is excluded for this transport
        sticker_skill = skill_registry.get_skill("sticker")
        sticker_excluded = (
            sticker_skill is not None
            and transport in sticker_skill.exclude_transports
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

    # Keep image bytes ephemeral. History only records a readable placeholder.
    stored_text = f"[图片] {text}" if image else text
    if message is not None:
        save_message(user_id, "user", stored_text, turn_id=turn_id)

    # ── Parallel pre-fetch: router classification + DB queries ──
    capability_context = ""
    tier = "main"
    client = get_client_for_tier(tier)
    routed_skill_names: list[str] = []

    # Pre-fetch habits (fast sync DB) — shared by router hint + system prompt
    habits = (
        []
        if is_bedtime or is_self_reminder or is_weekly or is_autonomous
        else await asyncio.to_thread(list_habits, user_id)
    )

    async def _safe_conversation_context() -> dict:
        if not (
            prompt_policy.conversation_summary
            or prompt_policy.recent_history
        ):
            return {
                "summary": "",
                "overflow": [],
                "recent": [],
                "trailing": [],
            }
        from mochi.config import MAX_HISTORY_TURNS
        recent_turns = (
            prompt_policy.recent_turns
            if prompt_policy.recent_turns is not None
            else MAX_HISTORY_TURNS
        )
        try:
            return await asyncio.to_thread(
                get_conversation_context,
                user_id,
                recent_turns,
                include_summary=prompt_policy.conversation_summary,
                include_standalone=prompt_policy.standalone_history,
                reasoning_source=client.reasoning_source,
            )
        except Exception as e:
            log.warning("Conversation context skipped: %s", e)
            return {
                "summary": "",
                "overflow": [],
                "recent": [],
                "trailing": [],
            }

    async def _safe_recalled_memories() -> list[dict]:
        if not prompt_policy.auto_recall or not text.strip():
            return []
        return await asyncio.to_thread(
            _retrieve_memories_for_turn, text, user_id,
        )

    # ── Skill mode: /skilloff skips router + non-core tools ──
    from mochi.db import get_skill_mode
    from mochi.turn_tool_policy import build_turn_tool_plan
    skill_mode_off = get_skill_mode() == "off"
    turn_plan = build_turn_tool_plan(transport)
    escalation_available = (
        is_bedtime
        or is_self_reminder
        or is_autonomous
        or (not is_weekly and turn_plan.request_tools_enabled)
    )
    _health_warning = ""

    weekly_session = None
    if is_weekly:
        from mochi.weekly_maintenance import create_weekly_session
        weekly_session = create_weekly_session(
            user_id=user_id,
            logical_date=runtime_entry.logical_date or "",
            period_key=runtime_entry.period_key or "",
        )
        tools = weekly_session.definitions()
        core_memory, conversation_context = await asyncio.gather(
            asyncio.to_thread(read_core),
            _safe_conversation_context(),
        )
        recalled_memories = []
        habits = []

    elif is_autonomous:
        tools = skill_registry.get_tools_by_load(
            "resident", transport=transport,
        )
        if escalation_available:
            from mochi.request_tools import REQUEST_TOOLS_DEF
            tools.append(REQUEST_TOOLS_DEF)
        core_memory, conversation_context = await asyncio.gather(
            asyncio.to_thread(read_core),
            _safe_conversation_context(),
        )
        recalled_memories = []
        habits = []

    elif skill_mode_off:
        tools = list(turn_plan.resident_definitions)

        core_memory, conversation_context, recalled_memories = await asyncio.gather(
            asyncio.to_thread(read_core),
            _safe_conversation_context(),
            _safe_recalled_memories(),
        )
        habits = []  # not needed in skilloff mode

    elif (is_bedtime or is_self_reminder) and TOOL_ROUTER_ENABLED:
        tools = skill_registry.get_tools_by_load(
            "resident", transport=transport,
        )
        tools.extend(skill_registry.get_tools_by_names(
            list(BEDTIME_ROUTED_SKILLS),
            transport=transport,
            loads={"routed"},
        ))
        tools = list({
            tool["function"]["name"]: tool
            for tool in tools
        }.values())
        if escalation_available:
            from mochi.request_tools import REQUEST_TOOLS_DEF
            tools.append(REQUEST_TOOLS_DEF)

        core_memory, conversation_context, recalled_memories = await asyncio.gather(
            asyncio.to_thread(read_core),
            _safe_conversation_context(),
            _safe_recalled_memories(),
        )

    elif turn_plan.router_enabled:
        from mochi.tool_router import classify_skills
        # Launch router (with habits hint) + remaining DB fetches concurrently
        skill_names, core_memory, conversation_context, recalled_memories = await asyncio.gather(
            classify_skills(text, user_id=user_id, habits=habits,
                            transport=transport,
                            catalog=turn_plan.router_descriptions),
            asyncio.to_thread(read_core),
            _safe_conversation_context(),
            _safe_recalled_memories(),
        )

        routed_skill_names = list(skill_names)

        from mochi.model_health import should_warn_user, get_warning_message
        if should_warn_user("lite"):
            _health_warning = get_warning_message("lite")

        tools = list(turn_plan.resident_definitions)
        tools = skill_registry.get_tools_by_names(
            skill_names, transport=transport, loads={"routed"},
        ) + tools
    else:
        # No explicit Lite assignment means no semantic pre-router. Main still
        # receives resident tools and request_tools when enabled.
        tools = list(turn_plan.resident_definitions)
        core_memory, conversation_context, recalled_memories = await asyncio.gather(
            asyncio.to_thread(read_core),
            _safe_conversation_context(),
            _safe_recalled_memories(),
        )

    history = (
        [
            *conversation_context["overflow"],
            *conversation_context["recent"],
            *(
                conversation_context["trailing"]
                if prompt_policy.trailing_history
                else []
            ),
        ]
        if prompt_policy.recent_history
        else []
    )
    history.sort(key=lambda item: item["id"])
    conv_summary = (
        conversation_context["summary"]
        if prompt_policy.conversation_summary
        else ""
    )

    if (
        escalation_available
        and not any(
            tool.get("function", {}).get("name") == "request_tools"
            for tool in tools
        )
    ):
        from mochi.request_tools import REQUEST_TOOLS_DEF
        tools.append(REQUEST_TOOLS_DEF)
    if message is not None and runtime_entry is None:
        from mochi.heartbeat import bedtime_tool_available

        if bedtime_tool_available():
            tools.append(ENTER_BEDTIME_DEF)

    # ── Policy: filter denied tools before LLM sees them ──
    from mochi.tool_policy import filter_tools, check as policy_check
    tools = filter_tools(tools)
    availability = ToolAvailability.from_definitions(
        tools, source="initial",
    )

    # Build context
    active_tool_names = list(availability.names)
    capability_context = skill_registry.get_capability_context_for_tools(
        active_tool_names,
        include_requestable_tools=escalation_available,
        transport=transport,
    )
    from mochi.skills.habit.handler import HabitSkill
    habit_skill = skill_registry.all_skills().get("habit")

    async def _habit_progress_context() -> str:
        if isinstance(habit_skill, HabitSkill) and "habit_progress" in availability.names:
            return await asyncio.to_thread(habit_skill.progress_context, user_id)
        return ""

    habit_progress_context = await _habit_progress_context()
    habit_context_loaded = bool(habit_progress_context)

    from mochi.tool_execution import recent_operations_context
    recent_operations = (
        ""
        if not prompt_policy.recent_operations
        else await asyncio.to_thread(
            recent_operations_context, user_id, history,
            include_autonomous=True,
        )
    )

    # Fetch diary data for Zone C runtime context
    # Only journal (events) — status panel (habits/todos) excluded from chat
    # to avoid LLM parroting progress in every reply. Status is available
    # via tools (habit_progress, manage_todo) when the user asks.
    from mochi.diary import diary as _diary
    _ds = (
        _diary.read(section="今日状態")
        if prompt_policy.diary_status
        else ""
    )
    diary_source_date, diary_today, diary_tomorrow = await asyncio.to_thread(
        _diary.read_write_snapshot,
    )
    _dj = diary_today if prompt_policy.diary_journal else ""
    diary_target_dates = {
        "today": diary_source_date,
        "tomorrow": (
            datetime.fromisoformat(diary_source_date) + timedelta(days=1)
        ).date().isoformat(),
    }
    diary_expected = {
        diary_source_date: diary_today if prompt_policy.diary_journal else None,
        diary_target_dates["tomorrow"]: (
            diary_tomorrow if is_bedtime or not diary_tomorrow else None
        ),
    }
    core_expected = core_memory
    if weekly_session:
        weekly_session.expected_core = core_memory

    system_prompt = _build_system_prompt(
        user_id, capability_context=capability_context, tool_names=active_tool_names,
        core_memory=core_memory, habits=habits, transport=transport,
        recalled_memories=recalled_memories,
        diary_status=_ds, diary_journal=_dj, diary_tomorrow=diary_tomorrow,
        conv_summary=(conv_summary or "") if prompt_policy.conversation_summary else "",
        recent_operations=recent_operations,
        runtime_entry=runtime_entry,
        weekly_context=(
            weekly_session.context.rendered if weekly_session else ""
        ),
        policy=prompt_policy,
        habit_progress_context=habit_progress_context,
        history_timestamps=_format_history_timestamps(history),
    )

    # Build messages array
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(_expand_history(history))
    if image:
        _replace_current_user_with_image(messages, stored_text, text, image)
        # Image understanding belongs to the configured Main model.
        tier = "main"

    # ── LLM call with tool loop ──
    max_tool_rounds = TOOL_LOOP_MAX_ROUNDS
    tool_names_used: list[str] = []  # track for tool_history persistence
    tool_audit: list[dict] = []
    successful_effects = False
    recall_exposure_recorded = False
    bedtime_requested = False
    tool_budget = ToolLoopBudget()
    on_interim = message.on_interim if message is not None else None
    bedtime_finalization_attempted = False
    history_response: LLMResponse | None = None
    bedtime_skip_requested = False

    def _log_main_usage(
        response: LLMResponse,
        *,
        call_type: str | None = None,
    ) -> None:
        log_usage(
            response.prompt_tokens, response.completion_tokens,
            response.total_tokens,
            tool_calls=len(response.tool_calls),
            model=response.model,
            purpose=(
                "bedtime_entry"
                if is_bedtime
                else "self_reminder_entry"
                if is_self_reminder
                else runtime_entry.kind
                if is_autonomous
                else "weekly_maintenance"
                if is_weekly
                else f"chat:{tier}"
            ),
            call_type=call_type,
            reasoning_tokens=response.reasoning_tokens,
            cached_prompt_tokens=response.cached_prompt_tokens,
        )

    def _free_time_cancelled() -> bool:
        if not is_autonomous:
            return False
        from mochi.heartbeat import free_time_turn_available

        return not free_time_turn_available(
            runtime_entry.chat_generation, runtime_entry.state_changed_at,
        )

    def _cancelled_result() -> ChatResult:
        return ChatResult(
            tool_audit=tool_audit, successful_effects=successful_effects,
            disposition="handled" if successful_effects else "skip",
        )

    def _final_result(reply: str, *, final_reply: bool = True) -> ChatResult:
        reasoning_metadata = (
            {
                "reasoning_content": history_response.reasoning_content,
                "reasoning_source": history_response.reasoning_source,
            }
            if history_response and history_response.reasoning_source
            else {}
        )
        tool_history_json = (
            json.dumps([{"name": n} for n in tool_names_used], ensure_ascii=False)
            if tool_names_used else None
        )
        if is_bedtime:
            if bedtime_skip_requested:
                return ChatResult(disposition="skip")
            if not reply and not pending_stickers:
                log.warning("Bedtime Main turn returned no disposition")
                return ChatResult()
            return ChatResult(
                text=reply,
                stickers=pending_stickers,
                _pending_history={
                    "user_id": user_id,
                    "content": reply or "[贴纸]",
                    "tool_history": tool_history_json,
                    "turn_id": turn_id,
                    "processed": message is None,
                    **reasoning_metadata,
                },
            )
        if is_self_reminder or is_autonomous:
            skipped = reply == "[SKIP]"
            if skipped:
                reply = ""
            if skipped and not successful_effects and not pending_stickers:
                return ChatResult(
                    tool_audit=tool_audit,
                    disposition="skip",
                )
            if not reply and not pending_stickers:
                return ChatResult(
                    tool_audit=tool_audit,
                    successful_effects=successful_effects,
                    disposition="handled" if successful_effects else "invalid",
                )
            pending_history = (
                None
                if is_autonomous and not reply
                else {
                    "user_id": user_id,
                    "content": reply or "[贴纸]",
                    "tool_history": tool_history_json,
                    "turn_id": turn_id,
                    "processed": True,
                    **reasoning_metadata,
                }
            )
            return ChatResult(
                text=reply,
                stickers=pending_stickers,
                tool_audit=tool_audit,
                successful_effects=successful_effects,
                disposition="deliver",
                _pending_history=pending_history,
            )
        if is_weekly:
            return ChatResult(
                tool_audit=tool_audit,
                successful_effects=successful_effects,
                disposition="handled" if successful_effects else "skip",
            )
        return ChatResult(
            text=reply,
            stickers=pending_stickers,
            bedtime_requested=bedtime_requested,
            _after_delivery=list(after_delivery) if final_reply else [],
            _pending_history={
                "user_id": user_id,
                "content": reply,
                "tool_history": tool_history_json,
                "turn_id": turn_id,
                "processed": False,
                **reasoning_metadata,
            },
        )

    async def _finalize_bedtime() -> str:
        nonlocal bedtime_finalization_attempted, history_response
        if bedtime_finalization_attempted:
            return ""
        bedtime_finalization_attempted = True
        try:
            final_response = await asyncio.to_thread(
                client.chat,
                messages=messages,
                tools=None,
                max_tokens=AI_CHAT_MAX_COMPLETION_TOKENS,
            )
        except Exception as exc:
            log.error("Bedtime finalization failed: %s", exc, exc_info=True)
            return ""
        _log_main_usage(
            final_response,
            call_type="bedtime_finalization",
        )
        history_response = final_response
        return STICKER_RE.sub("", final_response.content or "").strip()

    async def _ensure_bedtime_farewell(reply: str) -> str:
        nonlocal bedtime_skip_requested
        if not (is_bedtime or bedtime_requested):
            return reply
        if reply == "[SKIP]" and is_bedtime:
            bedtime_skip_requested = True
            return ""
        if reply == "[SKIP]":
            reply = ""
        if pending_stickers:
            return reply
        if not reply:
            reply = await _finalize_bedtime()
        return "" if reply == "[SKIP]" else reply

    for round_num in range(max_tool_rounds):
        if _free_time_cancelled():
            return _cancelled_result()
        if weekly_session:
            weekly_session.advance_visible_context()
        if not habit_context_loaded:
            habit_progress_context = await _habit_progress_context()
            if habit_progress_context:
                messages.append({
                    "role": "system",
                    "content": f"## 本轮习惯进度快照（只读事实）\n{habit_progress_context}",
                })
                habit_context_loaded = True
        document_updates: dict[str, str] = {}
        availability = availability.refresh_extensions(transport)
        round_availability = availability
        for _attempt in range(2):
            if _free_time_cancelled():
                return _cancelled_result()
            try:
                response = await asyncio.to_thread(
                    client.chat,
                    messages=messages,
                    tools=(
                        round_availability.provider_tools()
                        if round_availability.entries
                        else None
                    ),
                    max_tokens=AI_CHAT_MAX_COMPLETION_TOKENS,
                )
                break
            except Exception as e:
                if _attempt == 0:
                    log.warning("LLM call failed (attempt 1), retrying: %s", e)
                    continue
                log.error("LLM call failed (attempt 2): %s", e, exc_info=True)
                if is_bedtime:
                    return ChatResult()
                if is_self_reminder or is_autonomous:
                    return ChatResult(disposition="invalid")
                if is_weekly:
                    raise
                if image:
                    return ChatResult(
                        text=(
                            "图片处理失败了。请确认管理后台配置的 Chat 模型支持图片，"
                            "或换一张图片再试。"
                        )
                    )
                return ChatResult(text=f"API 报错：{e}")

        _log_main_usage(response)
        if _free_time_cancelled():
            return _cancelled_result()
        if recalled_memories and not recall_exposure_recorded:
            _record_recalled_memories_exposed(user_id, recalled_memories)
            exposed_memory_ids.update(
                item["memory_id"] for item in recalled_memories if "memory_id" in item
            )
            recall_exposure_recorded = True
        newly_exposed = pending_exposure_ids - exposed_memory_ids
        if newly_exposed:
            try:
                mark_memory_items_accessed(user_id, list(newly_exposed))
            except sqlite3.Error as exc:
                log.warning("Memory reference accounting failed: %s", exc)
            exposed_memory_ids.update(newly_exposed)
        pending_exposure_ids.clear()
        history_response = response

        # No tool calls — we have the final response
        if not response.tool_calls:
            reply = STICKER_RE.sub("", response.content or "").strip()
            reply = await _ensure_bedtime_farewell(reply)
            return _final_result(reply)

        # Add assistant message with tool_calls to context
        assistant_msg = {"role": "assistant", "content": response.content or ""}
        if response.reasoning_content:
            assistant_msg["reasoning_content"] = response.reasoning_content
        if response.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(
                            tc["arguments"]
                            if isinstance(tc["arguments"], dict)
                            else {}
                        ),
                    },
                }
                for tc in response.tool_calls
            ]
        messages.append(assistant_msg)

        next_availability = round_availability
        for tc in response.tool_calls:
            if _free_time_cancelled():
                return _cancelled_result()
            if not response.tool_calls_complete:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_call_error(
                        tc["name"],
                        "incomplete_tool_call",
                        "The provider ended before this tool call was complete. "
                        "It was not executed; retry it with complete arguments.",
                    ),
                })
                continue
            if tc["argument_error"]:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_call_error(
                        tc["name"],
                        "malformed_tool_arguments",
                        "The tool arguments were malformed. The tool was not "
                        "executed; retry with one valid JSON object.",
                    ),
                })
                continue
            if not round_availability.allows(tc["name"]):
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": unavailable_tool_error(tc["name"]),
                })
                continue
            validation_error = round_availability.validate_arguments(
                tc["name"], tc["arguments"],
            )
            if validation_error:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_call_error(
                        tc["name"],
                        "invalid_tool_arguments",
                        f"{validation_error}. The tool was not executed; "
                        "retry with arguments matching its schema.",
                    ),
                })
                continue

            arguments = tc["arguments"]
            assert isinstance(arguments, dict)

            # ── Handle tool escalation ──
            if tc["name"] == "request_tools":
                budget_error = tool_budget.claim_request(
                    TOOL_ESCALATION_MAX_PER_TURN,
                )
                if budget_error:
                    request_result, additions = budget_error, []
                else:
                    request_result, additions = resolve_request(
                        arguments,
                        round_availability,
                        transport=transport,
                    )
                next_availability = next_availability.with_definitions(
                    additions, source=f"request_round_{round_num + 1}",
                )
                result_text = json.dumps(request_result, ensure_ascii=False)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_text,
                })
                continue

            if tc["name"] == ENTER_BEDTIME_TOOL_NAME:
                if arguments:
                    result_text = model_result_for(SkillResult(
                        output="enter_bedtime accepts no arguments",
                        success=False,
                        error_code="invalid_tool_arguments",
                        retryable=True,
                    ))
                else:
                    bedtime_requested = True
                    result_text = json.dumps({
                        "ok": True,
                        "bedtime_requested": True,
                        "message": "Bedtime will begin after your farewell.",
                    }, ensure_ascii=False)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_text,
                })
                continue

            # ── Normal tool execution ──
            log.info("Tool call: %s", tc["name"])
            log.debug("Tool args: %s(%s)", tc["name"], tc["arguments"])

            budget_error = tool_budget.claim_tool(
                tc["name"],
                arguments,
                total_limit=TOOL_LOOP_TOTAL_TOOL_LIMIT,
                per_tool_limit=TOOL_LOOP_PER_TOOL_LIMIT,
            )
            if budget_error:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(budget_error, ensure_ascii=False),
                })
                continue

            # Notify transport of tool execution (status UX)
            if on_interim:
                try:
                    await on_interim(None, tool_name=tc["name"])
                except Exception:
                    pass

            is_weekly_tool = bool(
                weekly_session and weekly_session.owns(tc["name"])
            )
            if is_weekly and not is_weekly_tool:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": model_result_for(SkillResult(
                        output="Tool is outside the Weekly entry scope.",
                        success=False,
                        error_code="tool_outside_runtime_scope",
                        retryable=False,
                    )),
                })
                continue
            if not is_weekly_tool:
                decision = policy_check(tc["name"], user_id)
                if not decision.allowed:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": model_result_for(SkillResult(
                            output=decision.reason,
                            success=False,
                            error_code="policy_denied",
                            retryable=False,
                        )),
                    })
                    continue

            from mochi.tool_execution import (
                action_for, outcome_for, serialized_arguments,
            )
            skill_name = (
                "memory"
                if is_weekly_tool
                else round_availability.binding_for(tc["name"]).name
                if round_availability.binding_for(tc["name"]) is not None
                else skill_registry.get_tool_skill(tc["name"]) or ""
            )
            execution_source = (
                "weekly" if is_weekly_tool
                else f"runtime:{runtime_entry.kind}" if runtime_entry is not None
                else "chat"
            )
            execution_id = start_tool_execution(
                turn_id=turn_id,
                tool_call_id=tc["id"],
                user_id=user_id,
                source=execution_source,
                skill_name=skill_name,
                tool_name=tc["name"],
                action=action_for(tc["name"], arguments),
                arguments_json=serialized_arguments(tc["name"], arguments),
            )
            try:
                dispatch_args = dict(arguments)
                if tc["name"] == "update_core":
                    dispatch_args["_expected_content"] = core_expected
                elif tc["name"] == "write_diary":
                    target = diary_target_dates[arguments.get("day", "today")]
                    dispatch_args.update(
                        _expected_content=diary_expected[target],
                        _source_date=diary_source_date,
                        _target_date=target,
                    )
                if is_weekly_tool:
                    result = await weekly_session.execute(
                        tc["name"], arguments,
                    )
                    result.execution_started = True
                else:
                    result = await skill_registry.dispatch(
                        tc["name"], dispatch_args,
                        user_id=user_id, channel_id=channel_id,
                        transport=transport,
                        actor="main",
                        source=execution_source,
                        turn_id=turn_id,
                        bound_skill=round_availability.binding_for(tc["name"]),
                        owner_authorized=(
                            message.owner_authorized
                            if message is not None
                            else False
                        ),
                    )
                outcome = outcome_for(
                    skill_name, tc["name"], arguments, result,
                )
                tool_audit.append({
                    "name": tc["name"],
                    "status": outcome["status"],
                    "state_changed": bool(outcome["state_changed"]),
                })
                if outcome["status"] == "success" and outcome["state_changed"]:
                    successful_effects = True
                finish_tool_execution(
                    execution_id,
                    status=outcome["status"],
                    result_summary=outcome["result_summary"],
                    entity_refs=outcome["entity_refs"],
                    state_changed=outcome["state_changed"],
                )
            except Exception as e:
                finish_tool_execution(
                    execution_id, status="failed",
                    result_summary=f"Tool execution failed: {type(e).__name__}",
                )
                raise

            # Record tool name for history (exclude internal-only tools)
            if tc["name"] not in _TOOL_HISTORY_EXCLUDE:
                tool_names_used.append(tc["name"])

            # Extract [STICKER:file_id] markers from tool result
            if outcome["status"] == "success":
                if result.after_delivery is not None and runtime_entry is None:
                    if result.after_delivery not in after_delivery:
                        after_delivery.append(result.after_delivery)
                pending_exposure_ids.update(result.exposed_memory_ids)
                for m in STICKER_RE.finditer(result.output):
                    pending_stickers.append(m.group(1).strip())

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": model_result_for(result),
            })
            if result.document_snapshot is not None:
                if tc["name"] in {"update_core", "view_core_memory", "update_weekly_core"}:
                    document_updates["core"] = result.document_snapshot
                elif tc["name"] == "write_diary":
                    target = diary_target_dates[arguments.get("day", "today")]
                    document_updates[target] = result.document_snapshot
                elif tc["name"] == "read_diary" and not arguments.get("date"):
                    document_updates[diary_source_date] = result.document_snapshot

        # Only advance snapshots after their results become visible to Main.
        core_expected = document_updates.pop("core", core_expected)
        diary_expected.update(document_updates)
        if weekly_session:
            weekly_session.expected_core = core_expected
        availability = next_availability

    # If we exhausted tool rounds, return whatever we have
    reply = STICKER_RE.sub("", response.content or "").strip()
    reply = await _ensure_bedtime_farewell(reply)
    if not reply and not (
        is_bedtime or is_self_reminder or is_weekly or is_autonomous
    ):
        reply = "处理过程出了点问题，你再说一次试试？"
    if _health_warning and reply:
        reply += _health_warning
    return _final_result(reply, final_reply=False)
