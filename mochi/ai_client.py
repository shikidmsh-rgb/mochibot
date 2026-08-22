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
import os
import platform
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

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
from mochi.token_estimator import estimate_tokens
from mochi.tool_availability import ToolAvailability, unavailable_tool_error
from mochi.bedtime_tool import ENTER_BEDTIME_DEF, ENTER_BEDTIME_TOOL_NAME
import mochi.skills as skill_registry
from mochi.transport import IncomingMessage, ImageAttachment
from mochi.transport.utils import normalize_legacy_bubble_delimiters

log = logging.getLogger(__name__)

STICKER_RE = re.compile(r"\[STICKER:([^\]]+)\]")
_HISTORY_TIMESTAMP_PREFIX_RE = re.compile(
    r"^\s*\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]\s*"
)
_WEEKDAY_NAMES = (
    "星期一", "星期二", "星期三", "星期四",
    "星期五", "星期六", "星期日",
)

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


def _meal_source_for_current_message(
    image: ImageAttachment | None,
) -> str:
    """Bind meal provenance to the actual input shape, not model arguments."""
    return "photo" if image is not None else "text"


def _replace_current_user_with_image(
    messages: list[dict], stored_text: str, text: str, image: ImageAttachment,
) -> None:
    """Attach an image to the just-saved user turn without persisting bytes."""
    content = _image_content(text, image)
    if messages:
        message = messages[-1]
        existing = message.get("content")
        # _expand_history prefixes persisted turns with [YYYY-MM-DD HH:MM].
        if (message.get("role") == "user"
                and isinstance(existing, str)
                and (existing == stored_text or existing.endswith(stored_text))):
            message["content"] = content
            return
    # Defensive fallback for tests or custom DB adapters that omit the new row.
    messages.append({"role": "user", "content": content})

# ── Auto-recall state (per-user cooldown) ──
_user_last_recall: dict[int, float] = {}   # user_id → timestamp
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


def _retrieve_memories_for_turn(text: str, user_id: int) -> list[dict]:
    """Recall explicit text matches, optionally enhanced by vectors and KG."""
    from mochi.config import (
        MEMORY_AUTO_RECALL, MEMORY_AUTO_RECALL_TOP_K,
        MEMORY_AUTO_RECALL_MAX_ITEMS, MEMORY_AUTO_RECALL_MIN_VEC_SIM,
        MEMORY_AUTO_RECALL_MAX_CHARS, MEMORY_AUTO_RECALL_MAX_TOKENS,
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

    query_emb = None
    try:
        from mochi.model_pool import get_pool
        query_emb = get_pool().embed(text)
    except Exception as exc:
        log.warning(
            "auto-recall embedding failed; using keyword search: %s", exc,
        )

    try:
        recalled = recall_memory(
            user_id, query=text,
            limit=max(1, MEMORY_AUTO_RECALL_TOP_K),
            query_embedding=query_emb,
            bump_access=False,
        )

        max_chars = max(80, MEMORY_AUTO_RECALL_MAX_CHARS)
        candidates: list[dict] = []
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

            content = " ".join((item.get("content") or "").split())
            if not content:
                continue
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

        max_total = max(1, MEMORY_AUTO_RECALL_MAX_ITEMS) + 2
        max_tokens = max(1, MEMORY_AUTO_RECALL_MAX_TOKENS)
        selected: list[dict] = []
        for candidate in candidates:
            proposed = selected + [candidate]
            if estimate_tokens(_format_recalled_memories(proposed)) > max_tokens:
                break
            selected = proposed
            if len(selected) >= max_total:
                break

        if not selected:
            return []
        mark_memory_items_accessed(
            user_id,
            [
                candidate["memory_id"]
                for candidate in selected
                if candidate.get("memory_id") is not None
            ],
        )
        if len(_user_last_recall) >= _USER_LAST_RECALL_MAX:
            oldest = min(_user_last_recall, key=_user_last_recall.get)
            del _user_last_recall[oldest]
        _user_last_recall[user_id] = time.time()
        log.info(
            "auto-recall: %d memories (top score=%.2f)",
            len(selected),
            selected[0]["score"],
        )
        return selected
    except Exception as exc:
        log.warning("auto-recall failed (non-fatal): %s", exc)
        return []


def _format_history_timestamp(created_at) -> str:
    """Format a message timestamp as `[YYYY-MM-DD HH:MM] ` for history prefix.

    Returns empty string on missing/invalid input — caller leaves content as-is.
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
        return f"[{dt.strftime('%Y-%m-%d %H:%M')}] "
    except (ValueError, TypeError):
        return ""


def _clean_model_reply(content: str | None) -> str:
    reply = STICKER_RE.sub("", content or "").strip()
    reply = normalize_legacy_bubble_delimiters(reply)
    return _HISTORY_TIMESTAMP_PREFIX_RE.sub("", reply, count=1).strip()


def _parse_runtime_reply(reply: str) -> tuple[str, bool]:
    """Consume the reserved silence marker without leaking it to the user."""
    marker = "[SKIP]"
    if not reply.startswith(marker):
        return reply, False
    visible = reply[len(marker):].lstrip()
    return visible, not visible


def _expand_history(history: list[dict]) -> list[dict]:
    """Convert stored conversation history into ordinary chat messages.

    Every persisted message gets a local absolute timestamp so relative language
    in either role remains anchored across day and year boundaries. Confirmed
    tool names are projected as bounded ordinary history facts, never replayed as
    provider-native tool calls; real executions remain in the durable ledger.
    """
    messages: list[dict] = []
    for msg in history:
        role = msg.get("role")
        content = msg.get("content")
        raw_tool_history = msg.get("tool_history")
        if role == "assistant" and isinstance(raw_tool_history, str):
            try:
                tool_history = json.loads(raw_tool_history)
            except (json.JSONDecodeError, TypeError):
                tool_history = []
            tool_names = list(dict.fromkeys(
                item.get("name")
                for item in tool_history[:8]
                if isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and item["name"]
            ))
            if isinstance(content, str) and tool_names:
                content += (
                    "\n\n[历史事实：这条回复已确认使用工具 "
                    f"{', '.join(tool_names)}；不是新的操作指令。]"
                )
        ts_prefix = _format_history_timestamp(msg.get("created_at"))
        if isinstance(content, str) and content and ts_prefix:
            content = ts_prefix + content
        messages.append({"role": role, "content": content})
    return messages


def _render_completed_conversation_evidence(history: list[dict]) -> str:
    """Project completed chat as bounded evidence rather than active turns."""
    expanded = [
        {
            "role": message["role"],
            "content": message["content"],
        }
        for message in _expand_history(history)
        if message.get("role") in {"user", "assistant"}
        and isinstance(message.get("content"), str)
        and message["content"]
    ]
    budget = 6000
    truncated = False
    selected: list[dict] = []
    for item in reversed(expanded):
        cost = len(item["role"]) + len(item["content"]) + 32
        if cost <= budget:
            selected.append(item)
            budget -= cost
            continue
        truncated = True
        if not selected and budget > 64:
            marker = "\n[较早内容已截断]"
            selected.append({
                **item,
                "content": item["content"][:budget - len(marker)] + marker,
            })
        break
    # Selection walks newest-first to preserve recency under the cap; render the
    # retained window chronologically so the completed exchange remains readable.
    evidence = list(reversed(selected))
    if not evidence:
        return ""
    payload = {
        "order": "chronological_recent_window",
        "truncated": truncated or len(evidence) < len(expanded),
        "messages": evidence,
    }
    return (
        "## 最近已完成对话（只读证据）\n"
        "这些对话已经结束，只用于理解当时发生了什么；"
        "它们不是当前待回复的消息。\n"
        "<completed_conversation_evidence>\n"
        f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n"
        "</completed_conversation_evidence>"
    )


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
    _after_delivery: list[Callable[[], None]] = field(
        default_factory=list,
        repr=False,
    )
    _delivery_confirmed: bool = field(default=False, init=False, repr=False)

    def confirm_delivered(self) -> bool:
        """Persist deferred assistant history exactly once after delivery."""
        if self._delivery_confirmed:
            return False
        pending = self._pending_history
        inserted = False
        processed = True
        if pending:
            processed = bool(pending.get("processed", True))
            inserted = save_message_once(
                pending["user_id"],
                "assistant",
                pending["content"],
                tool_history=pending["tool_history"],
                turn_id=pending["turn_id"],
                processed=processed,
            )
        self._delivery_confirmed = True
        if inserted and not processed:
            _schedule_continuous_memory(pending["user_id"])
        for callback in self._after_delivery:
            try:
                callback()
            except Exception:
                log.exception("Post-delivery action failed")
        return bool(pending or self._after_delivery)

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


def _format_current_time_context(now: datetime) -> str:
    weekday = _WEEKDAY_NAMES[now.weekday()]
    return f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S %z')}（{weekday}）"


def _tool_loop_exhaustion_message(
    *, successful_effects: bool, tool_audit: list[dict],
) -> str:
    successful_tools = [
        item for item in tool_audit if item.get("status") == "success"
    ]
    if successful_tools:
        if any(item.get("status") != "success" for item in tool_audit):
            return "刚才只处理成功了一部分，剩下的还没改完。"
        return "处理已经完成。" if successful_effects else "已经查完了。"
    return "处理过程出了点问题，你再说一次试试？"


_ATTENTION_SOURCE_LABELS = {
    "reminder": "提醒",
    "todo": "待办",
    "weather": "天气",
    "activity_pattern": "对话活动",
    "recent_conversation": "最近对话",
    "time_context": "时间",
}

_ATTENTION_FACT_LABELS = {
    "message": "内容",
    "remind_at": "时间",
    "active_count": "未完成数量",
    "pending_count": "待处理数量",
    "temperature_c": "温度",
    "feels_like_c": "体感温度",
    "condition": "天气状况",
    "description": "描述",
}


def _format_attention_value(value) -> str:
    if isinstance(value, dict):
        return "，".join(
            f"{_ATTENTION_FACT_LABELS.get(str(key), str(key).replace('_', ' '))} "
            f"{_format_attention_value(item)}"
            for key, item in value.items()
        )
    if isinstance(value, list):
        return "、".join(_format_attention_value(item) for item in value)
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _format_attention_fact(fact) -> str:
    source = _ATTENTION_SOURCE_LABELS.get(
        fact.source, fact.source.replace("_", " "),
    )
    freshness = "新近观察" if fact.freshness == "fresh" else "较早观察"
    try:
        observed = datetime.fromisoformat(fact.observed_at)
        from mochi.config import TZ
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=TZ)
        else:
            observed = observed.astimezone(TZ)
        observed_label = observed.strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        observed_label = ""
    details = _format_attention_value(fact.facts)
    context = f"{freshness} {observed_label}".strip()
    return f"- {source}（{context}）：{details}"


def _render_autonomous_situation(runtime_entry: MainRuntimeEntry) -> str:
    if runtime_entry.kind == "free_time":
        situation = get_prompt("free_time_entry")
    elif runtime_entry.kind == "attention":
        situation = get_prompt("attention_entry")
        fact_lines = [
            _format_attention_fact(fact)
            for fact in runtime_entry.attention_facts
        ]
        situation = situation.replace(
            "{{wake_reason}}", runtime_entry.wake_reason or "periodic",
        ).replace(
            "{{attention_facts}}",
            "\n".join(fact_lines) if fact_lines else "- 当前没有观察事实",
        )
    else:
        raise ValueError("runtime situation is only available for autonomous entries")
    if not situation:
        raise RuntimeError(f"{runtime_entry.kind} entry prompt is missing")
    protocol = get_prompt("runtime_silence_protocol")
    if not protocol:
        raise RuntimeError("Runtime silence protocol prompt is missing")
    return f"{situation}\n\n{protocol}"


def _render_self_reminder_event(runtime_entry: MainRuntimeEntry) -> str:
    if runtime_entry.kind != "self_reminder":
        raise ValueError("Self reminder event requires a self reminder entry")
    reminder_context = get_prompt("self_reminder_entry")
    if not reminder_context:
        raise RuntimeError("Self reminder entry prompt is missing")
    return (
        reminder_context.replace(
            "{{scheduled_for}}", runtime_entry.scheduled_for or "",
        ).replace(
            "{{recurrence}}", runtime_entry.recurrence or "one_time",
        ).replace(
            "{{intent}}", runtime_entry.intent or "",
        )
    )


def _build_system_prompt(user_id: int, capability_context: str = "",
                         tool_names: list[str] | None = None,
                         core_memory: str = "",
                         habits: list[dict] | None = None,
                         transport: str = "",
                         recalled_memories: list[dict] | None = None,
                         diary_status: str = "",
                         diary_journal: str = "",
                         conv_summary: str = "",
                         conversation_evidence: str = "",
                         recent_operations: str = "",
                         runtime_entry: MainRuntimeEntry | None = None,
                         weekly_context: str = "",
                         policy: ContextPolicy | None = None,
                         defer_runtime_situation: bool = False) -> str:
    """Assemble explicit identity, situation, capability, and live-context zones."""

    modules = get_system_chat_modules()
    from mochi.config import TZ
    now = datetime.now(TZ)
    policy = policy or context_policy(runtime_entry)
    is_weekly = bool(
        runtime_entry and runtime_entry.kind == "weekly_maintenance"
    )
    is_autonomous = bool(
        runtime_entry and runtime_entry.kind in {"free_time", "attention"}
    )
    is_self_reminder = bool(
        runtime_entry and runtime_entry.kind == "self_reminder"
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
        if (
            policy.early_runtime_situation
            and runtime_entry
            and not defer_runtime_situation
        ):
            early_runtime_situation.append(
                _render_autonomous_situation(runtime_entry)
            )

    capability_parts = []
    if not is_weekly:
        from mochi.skills import get_capability_summary
        cap = get_capability_summary(transport=transport)
        if cap:
            capability_parts.append(cap)

    if capability_context:
        capability_parts.append(f"## 能力上下文\n{capability_context}")

    if user_id and tool_names and habits:
        habit_tool_names = {"query_habit", "checkin_habit", "edit_habit"}
        if habit_tool_names & set(tool_names):
            from mochi.skills.habit.logic import describe_frequency
            habit_lines = "  ".join(
                f"#{h['id']} {h['name']} ({describe_frequency(h['frequency'])})"
                for h in habits
            )
            if habit_lines:
                capability_parts.append(f"## 习惯列表 (打卡用)\n{habit_lines}")

    if policy.prompt_sections and not is_weekly:
        for section in skill_registry.get_prompt_sections(compact=True):
            capability_parts.append(section)

    hist_ts_inst = get_prompt("system_chat/_history_timestamp")
    if (
        hist_ts_inst
        and not is_weekly
        and not is_self_reminder
        and policy.recent_history
    ):
        capability_parts.append(hist_ts_inst)

    dynamic_live_context = []
    if "runtime_context" in modules:
        rendered_rc = _render_runtime_context(
            modules["runtime_context"], diary_status, diary_journal,
        )
        if rendered_rc:
            dynamic_live_context.append(rendered_rc)

    if conv_summary:
        dynamic_live_context.append(f"## 本次对话早期内容（摘要）\n{conv_summary}")

    if conversation_evidence:
        dynamic_live_context.append(conversation_evidence)

    if recent_operations:
        dynamic_live_context.append(recent_operations)

    if recalled_memories:
        dynamic_live_context.append(
            _format_recalled_memories(recalled_memories)
        )

    if runtime_entry and runtime_entry.kind == "bedtime":
        bedtime_context = get_prompt("bedtime_entry")
        if not bedtime_context:
            raise RuntimeError("Bedtime entry prompt is missing")
        silence_protocol = get_prompt("runtime_silence_protocol")
        if not silence_protocol:
            raise RuntimeError("Runtime silence protocol prompt is missing")
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
            + "\n\n"
            + silence_protocol
        )
    elif is_self_reminder and not defer_runtime_situation:
        dynamic_live_context.append(_render_self_reminder_event(runtime_entry))
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
        + (
            [_format_current_time_context(now)]
            if policy.temporal_context
            else []
        )
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
        TOOL_LOOP_DUPLICATE_LIMIT,
    )

    runtime_entry = runtime_entry or (
        message.runtime_entry if message is not None else None
    )
    if message is None and runtime_entry is None:
        raise ValueError("chat requires an incoming message or runtime entry")
    if (
        message is not None
        and runtime_entry is not None
        and runtime_entry.kind in {"self_reminder", "free_time", "attention"}
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
        runtime_entry and runtime_entry.kind in {"free_time", "attention"}
    )
    prompt_policy = context_policy(runtime_entry)
    turn_id = (
        runtime_entry.idempotency_key
        if (is_self_reminder or is_weekly or is_autonomous)
        and runtime_entry.idempotency_key
        else uuid.uuid4().hex
    )
    pending_stickers: list[str] = []

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
    current_user_message_id = None
    if message is not None:
        current_user_message_id = save_message(
            user_id,
            "user",
            stored_text,
            turn_id=turn_id,
        )

    # ── Parallel pre-fetch: router classification + DB queries ──
    capability_context = ""
    tier = "main"
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
                current_user_message_id=current_user_message_id,
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
    sticky_skill_names = (
        await asyncio.to_thread(
            skill_registry.get_recent_multi_turn_skill_names,
            user_id,
        )
        if message is not None and runtime_entry is None and not skill_mode_off
        else []
    )
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
        core_memory, conversation_context = await asyncio.gather(
            asyncio.to_thread(read_core),
            _safe_conversation_context(),
        )
        weekly_session = create_weekly_session(
            user_id=user_id,
            logical_date=runtime_entry.logical_date or "",
            period_key=runtime_entry.period_key or "",
            core_content=core_memory,
        )
        tools = weekly_session.definitions()
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

        skill_names = turn_plan.filter_router_selection([
            *skill_names,
            *sticky_skill_names,
        ])
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
        # receives resident tools, recent multi-turn tools, and request_tools.
        tools = list(turn_plan.resident_definitions)
        routed_skill_names = list(sticky_skill_names)
        tools = skill_registry.get_tools_by_names(
            routed_skill_names,
            transport=transport,
            loads={"routed"},
        ) + tools
        core_memory, conversation_context, recalled_memories = await asyncio.gather(
            asyncio.to_thread(read_core),
            _safe_conversation_context(),
            _safe_recalled_memories(),
        )

    history = (
        [
            *(
                conversation_context["overflow"]
                if prompt_policy.trailing_history
                else []
            ),
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
    conv_summary = (
        conversation_context["summary"]
        if prompt_policy.conversation_summary
        else ""
    )
    conversation_evidence = (
        _render_completed_conversation_evidence(history)
        if is_self_reminder
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
    tools = skill_registry.filter_tools_for_context(
        tools,
        user_id=user_id,
        transport=transport,
    )
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

    from mochi.tool_execution import recent_operations_context
    recent_operations = (
        ""
        if not prompt_policy.recent_operations
        else await asyncio.to_thread(
            recent_operations_context, user_id, text, routed_skill_names,
        )
    )

    # Fetch diary data for Zone C runtime context
    # Only journal (events) — status panel (habits/todos) excluded from chat
    # to avoid LLM parroting progress in every reply. Status is available
    # via tools (query_habit, manage_todo) when the user asks.
    from mochi.diary import diary as _diary
    _ds = (
        _diary.read(section="今日状態")
        if prompt_policy.diary_status
        else ""
    )
    _dj = (
        _diary.read(section="今日日記")
        if prompt_policy.diary_journal
        else ""
    )

    system_prompt = _build_system_prompt(
        user_id, capability_context=capability_context, tool_names=active_tool_names,
        core_memory=core_memory, habits=habits, transport=transport,
        recalled_memories=recalled_memories,
        diary_status=_ds, diary_journal=_dj,
        conv_summary=(conv_summary or "") if prompt_policy.conversation_summary else "",
        conversation_evidence=conversation_evidence,
        recent_operations=recent_operations,
        runtime_entry=runtime_entry,
        weekly_context=(
            weekly_session.context.rendered if weekly_session else ""
        ),
        policy=prompt_policy,
        defer_runtime_situation=is_autonomous or is_self_reminder,
    )

    # Build messages array
    messages = [{"role": "system", "content": system_prompt}]
    if not is_self_reminder:
        messages.extend(_expand_history(history))
    if is_autonomous:
        messages.append({
            "role": "system",
            "content": _render_autonomous_situation(runtime_entry),
        })
    elif is_self_reminder:
        messages.append({
            # Provider APIs need one active turn. The complete typed event is
            # that turn; it explicitly distinguishes itself from owner speech.
            "role": "user",
            "content": _render_self_reminder_event(runtime_entry),
        })
    if image:
        _replace_current_user_with_image(messages, stored_text, text, image)
        # Image understanding belongs to the configured Main model.
        tier = "main"

    # ── LLM call with tool loop ──
    max_tool_rounds = TOOL_LOOP_MAX_ROUNDS
    client = get_client_for_tier(tier)
    tool_names_used: list[str] = []  # track for tool_history persistence
    tool_audit: list[dict] = []
    successful_effects = False
    core_expected = core_memory
    core_write_completed = False
    diary_expected = _dj
    diary_write_completed = False
    bedtime_requested = False
    after_delivery_actions: list[Callable[[], None]] = []
    tool_budget = ToolLoopBudget()
    on_interim = message.on_interim if message is not None else None

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

    def _final_result(reply: str) -> ChatResult:
        if is_bedtime or bedtime_requested:
            reply, _ = _parse_runtime_reply(reply)
        tool_history_json = (
            json.dumps([{"name": n} for n in tool_names_used], ensure_ascii=False)
            if tool_names_used else None
        )
        if is_bedtime:
            if not reply and not pending_stickers:
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
                },
            )
        if is_self_reminder or is_autonomous:
            reply, skipped = _parse_runtime_reply(reply)
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
        if bedtime_requested and not reply and not pending_stickers:
            return ChatResult(
                bedtime_requested=True,
                tool_audit=tool_audit,
                successful_effects=successful_effects,
                disposition="handled",
            )
        return ChatResult(
            text=reply,
            stickers=pending_stickers,
            bedtime_requested=bedtime_requested,
            _after_delivery=list(after_delivery_actions),
            _pending_history={
                "user_id": user_id,
                "content": reply,
                "tool_history": tool_history_json,
                "turn_id": turn_id,
                "processed": False,
            },
        )

    for round_num in range(max_tool_rounds):
        round_availability = availability
        for _attempt in range(2):
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

        # No tool calls — we have the final response
        if not response.tool_calls:
            reply = _clean_model_reply(response.content)
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
                        "arguments": json.dumps(tc["arguments"]),
                    },
                }
                for tc in response.tool_calls
            ]
        messages.append(assistant_msg)

        pending_definitions: list[dict] = []
        core_update_attempted = False
        diary_update_attempted = False
        weekly_core_update_attempted = False
        for tc in response.tool_calls:
            # ── Handle tool escalation ──
            if tc["name"] == "request_tools":
                if not round_availability.allows("request_tools"):
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": unavailable_tool_error(tc["name"]),
                    })
                    continue
                budget_error = tool_budget.claim_request(
                    TOOL_ESCALATION_MAX_PER_TURN,
                )
                if budget_error:
                    request_result, additions = budget_error, []
                else:
                    request_result, additions = resolve_request(
                        tc["arguments"],
                        round_availability,
                        transport=transport,
                        user_id=user_id,
                    )
                pending_definitions.extend(additions)
                result_text = json.dumps(request_result, ensure_ascii=False)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_text,
                })
                continue

            if tc["name"] == ENTER_BEDTIME_TOOL_NAME:
                if not round_availability.allows(ENTER_BEDTIME_TOOL_NAME):
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": unavailable_tool_error(tc["name"]),
                    })
                    continue
                if tc["arguments"]:
                    result_text = json.dumps({
                        "ok": False,
                        "error": "enter_bedtime accepts no arguments",
                    }, ensure_ascii=False)
                else:
                    bedtime_requested = True
                    result_text = json.dumps({
                        "ok": True,
                        "bedtime_requested": True,
                        "message": (
                            "Bedtime will begin after this turn. If nothing else "
                            "needs saying, finish with [SKIP]."
                        ),
                    }, ensure_ascii=False)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_text,
                })
                continue

            # ── Normal tool execution ──
            if tc["name"] == "update_core" and core_write_completed:
                current = await asyncio.to_thread(read_core)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": (
                        "Core was already updated successfully this turn. "
                        f"No second write was applied.\n\nCurrent Core:\n{current}"
                    ),
                })
                continue
            if tc["name"] == "write_diary" and diary_write_completed:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": (
                        "Today's journal was already updated successfully this "
                        "turn. No second write was applied.\n\n"
                        f"Current journal:\n{_diary.read(section='今日日記')}"
                    ),
                })
                continue

            log.info("Tool call: %s", tc["name"])
            log.debug("Tool args: %s(%s)", tc["name"], tc["arguments"])

            if not round_availability.allows(tc["name"]):
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": unavailable_tool_error(tc["name"]),
                })
                continue

            budget_error = tool_budget.claim_tool(
                tc["name"],
                tc["arguments"],
                total_limit=TOOL_LOOP_TOTAL_TOOL_LIMIT,
                duplicate_limit=TOOL_LOOP_DUPLICATE_LIMIT,
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
                    "content": "Tool is outside the Weekly entry scope.",
                })
                continue
            if not is_weekly_tool:
                decision = policy_check(tc["name"], user_id)
                if not decision.allowed:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": decision.reason,
                    })
                    continue

            from mochi.tool_execution import (
                action_for, outcome_for, serialized_arguments,
            )
            skill_name = (
                "memory"
                if is_weekly_tool
                else skill_registry.get_tool_skill(tc["name"]) or ""
            )
            execution_source = (
                "weekly"
                if is_weekly_tool
                else f"runtime:{runtime_entry.kind}"
                if runtime_entry is not None
                else "chat"
            )
            execution_id = start_tool_execution(
                turn_id=turn_id,
                tool_call_id=tc["id"],
                user_id=user_id,
                source=execution_source,
                skill_name=skill_name,
                tool_name=tc["name"],
                action=action_for(tc["name"], tc["arguments"]),
                arguments_json=serialized_arguments(tc["name"], tc["arguments"]),
            )
            dispatch_args = tc["arguments"]
            if tc["name"] == "update_core" and not is_weekly_tool:
                core_update_attempted = True
                dispatch_args = {
                    **tc["arguments"],
                    "_expected_content": core_expected,
                }
            elif tc["name"] == "write_diary" and not is_weekly_tool:
                diary_update_attempted = True
                dispatch_args = {
                    **tc["arguments"],
                    "_expected_content": diary_expected,
                }
            elif tc["name"] == "log_meal" and not is_weekly_tool:
                dispatch_args = {
                    **tc["arguments"],
                    "_source": _meal_source_for_current_message(image),
                }
            elif tc["name"] == "update_weekly_core":
                weekly_core_update_attempted = True
            try:
                if is_weekly_tool:
                    result = await weekly_session.execute(
                        tc["name"], tc["arguments"],
                    )
                else:
                    result = await skill_registry.dispatch(
                        tc["name"], dispatch_args,
                        user_id=user_id, channel_id=channel_id,
                        transport=transport,
                        actor="main",
                        source=execution_source,
                        turn_id=turn_id,
                    )
                if result.after_delivery:
                    after_delivery_actions.append(result.after_delivery)
                outcome = outcome_for(
                    skill_name, tc["name"], tc["arguments"], result,
                )
                tool_audit.append({
                    "name": tc["name"],
                    "status": outcome["status"],
                    "state_changed": bool(outcome["state_changed"]),
                })
                if outcome["status"] == "success" and outcome["state_changed"]:
                    successful_effects = True
                    if tc["name"] == "update_core":
                        core_write_completed = True
                    elif tc["name"] == "write_diary":
                        diary_write_completed = True
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
                for m in STICKER_RE.finditer(result.output):
                    pending_stickers.append(m.group(1).strip())

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result.output,
            })

        if core_update_attempted:
            core_memory = await asyncio.to_thread(read_core)
            if not core_write_completed:
                core_expected = core_memory
        if diary_update_attempted:
            current_journal = await asyncio.to_thread(
                _diary.read,
                "今日日記",
            )
            if not diary_write_completed:
                diary_expected = current_journal
        if weekly_core_update_attempted and weekly_session:
            weekly_session.expected_core = await asyncio.to_thread(read_core)

        if pending_definitions:
            availability = availability.with_definitions(
                pending_definitions,
                source=f"request_round_{round_num + 1}",
            )

    # If we exhausted tool rounds, return whatever we have
    reply = _clean_model_reply(response.content)
    if not reply and not (
        is_bedtime or is_self_reminder or is_weekly or is_autonomous
    ):
        reply = _tool_loop_exhaustion_message(
            successful_effects=successful_effects,
            tool_audit=tool_audit,
        )
    if _health_warning and reply:
        reply += _health_warning
    return _final_result(reply)
