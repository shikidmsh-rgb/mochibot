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
from dataclasses import asdict, dataclass, field
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
from mochi.bedtime_tool import (
    EMPTY_DIARY, ENTER_BEDTIME_DEF, ENTER_BEDTIME_TOOL_NAME, bedtime_context,
)
import mochi.skills as skill_registry
import mochi.runtime_trace as runtime_trace
from mochi.file_text import MAX_FILE_TEXT_CHARS, extract_file_text
from mochi.transport import DeliveryError, FileAttachment, IncomingMessage, ImageAttachment

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


def _replace_current_user_content(
    messages: list[dict], stored_text: str, content: str | list[dict],
) -> None:
    """Attach ephemeral input to the just-saved user turn without persisting it."""
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


_FILE_TEXT_NOTES = {
    "unsupported": "（这个文件的格式暂时读不了，只知道文件名。）",
    "no_text": "（没有从文件中读到文字，可能是扫描件或图片。）",
    "unreadable": "（文件损坏或已加密，读不了里面的内容。）",
}


def _file_content(text: str, attachment: FileAttachment) -> str:
    extracted = extract_file_text(attachment.name, attachment.data)
    if extracted.status != "ok":
        return f"{text}\n\n{_FILE_TEXT_NOTES[extracted.status]}"
    content = f"{text}\n\n以下是文件「{attachment.name}」中的文字：\n{extracted.text}"
    if extracted.truncated:
        content += f"\n\n（文件较长，以上只包含前 {MAX_FILE_TEXT_CHARS} 字。）"
    return content

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
            items = runtime_trace.prepare_sync(
                "memory_search", recall_memory,
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


def _format_free_time_activation(history: list[dict]) -> str:
    completed_turns = [
        {
            "speaker": message["role"],
            "timestamp": message["created_at"],
            "content": message["content"],
        }
        for message in history
    ]
    payload = json.dumps(completed_turns, ensure_ascii=False, separators=(",", ":"))
    return (
        '<free_time_activation source="runtime">\n'
        "本轮是 Free Time 系统唤醒，没有新的用户消息。\n"
        '<recent_completed_turns role="read_only_evidence">\n'
        f"{payload}\n"
        "</recent_completed_turns>\n"
        "</free_time_activation>"
    )


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
    _trace_id: str = field(default="", repr=False)
    _trace_component: bool = field(default=False, repr=False)

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
            runtime_trace.record_delivery(self, "delivered")
        return confirmed

    def to_durable(self) -> DurableChatResult:
        return DurableChatResult(
            text=self.text,
            stickers=tuple(self.stickers),
            pending_history=self._pending_history,
            tool_audit=tuple(self.tool_audit),
            successful_effects=self.successful_effects,
            disposition=self.disposition,
            trace_id=self._trace_id,
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
            _trace_id=result.trace_id,
        )


def _render_runtime_context(template: str, diary_status: str = "",
                            diary_journal: str | None = "") -> str:
    """Distinguish an empty journal from one intentionally not supplied."""
    result = template

    if diary_status:
        result = result.replace("{{diary_status}}", diary_status)
    else:
        # Remove ### 状态速览 block
        result = re.sub(
            r"### 状态速览\n\{\{diary_status\}\}\n*", "", result,
        )

    if diary_journal is not None:
        result = result.replace("{{diary_entry}}", diary_journal or EMPTY_DIARY)
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


def _fill_agent_section(
    agent: str, name: str, value: str,
    token_parts: list[tuple[str, str]] | None = None,
) -> str:
    """Fill an agent.md placeholder, dropping its heading when empty."""
    placeholder = "{{" + name + "}}"
    pattern = re.escape(placeholder) if value else r"\n*#+ [^\n]*\n+" + re.escape(placeholder)
    if token_parts is not None:
        from mochi.token_distribution import replace_range
        for match in reversed(list(re.finditer(pattern, agent))):
            replace_range(
                token_parts, match.start(), match.end(),
                [(f"agent.{name}", value)] if value else [],
            )
    if value:
        return agent.replace(placeholder, value)
    return re.sub(
        r"\n*#+ [^\n]*\n+" + re.escape(placeholder), "", agent,
    )


def _build_system_prompt(user_id: int, **kwargs) -> str:
    """Single system prompt for entries that keep per-turn context in system."""
    stable, turn_context = _build_prompt_zones(user_id, **kwargs)
    return "\n\n".join(part for part in (stable, turn_context) if part)


def _build_prompt_zones(user_id: int, capability_context: str = "",
                        requestable_tools: str = "",
                        tool_names: list[str] | None = None,
                        core_memory: str = "",
                        habits: list[dict] | None = None,
                        transport: str = "",
                        recalled_memories: list[dict] | None = None,
                        diary_status: str = "",
                        diary_journal: str = "",
                        conv_summary: str = "",
                        recent_operations: str = "",
                        runtime_entry: MainRuntimeEntry | None = None,
                        dream_context: str = "",
                        policy: ContextPolicy | None = None,
                        habit_progress_context: str = "",
                        history_timestamps: str = "",
                        day_start_context: str = "",
                        bedtime_review: str = "",
                        token_parts: dict[str, list[tuple[str, str]]] | None = None,
                        ) -> tuple[str, str]:
    """Return the cross-turn stable prompt and this turn's live context."""

    modules = get_system_chat_modules()
    from mochi.config import TZ
    now = datetime.now(TZ)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S %z") + f" {_WEEKDAY_NAMES[now.weekday()]}"
    policy = policy or context_policy(runtime_entry)
    is_dream = bool(
        runtime_entry and runtime_entry.kind == "dream"
    )
    is_autonomous = bool(
        runtime_entry and runtime_entry.kind == "free_time"
    )

    stable_identity = []
    detailed_parts: dict[str, list[tuple[str, str]]] = {}
    if core_memory:
        stable_identity.append(("core", core_memory))
    if "agent" in modules:
        agent = modules["agent"].replace(
            "{{deployment_environment}}", _deployment_environment(),
        )
        agent_parts = [("agent", agent)]
        agent = _fill_agent_section(agent, "capability_context", capability_context, agent_parts)
        agent = _fill_agent_section(agent, "requestable_tools", requestable_tools, agent_parts)
        detailed_parts["agent"] = agent_parts
        stable_identity.append(("agent", agent))
    early_runtime_situation = []
    if policy.early_runtime_situation and runtime_entry:
        situation = get_prompt("free_time_entry")
        if not situation:
            raise RuntimeError(f"{runtime_entry.kind} entry prompt is missing")
        early_runtime_situation.append(("runtime.situation", situation))
        protocol = get_prompt("runtime_silence_protocol")
        if not protocol:
            raise RuntimeError("Runtime silence protocol prompt is missing")
        early_runtime_situation.append(("runtime.silence_protocol", protocol))

    capability_parts = []
    dynamic_live_context = []
    if habit_progress_context and not is_autonomous:
        dynamic_live_context.append(
            ("habits.progress", f"## 本轮习惯进度快照（只读事实）\n{habit_progress_context}")
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
                capability_parts.append(("habits.list", f"## 习惯列表 (打卡用)\n{habit_lines}"))

    if policy.prompt_sections and not is_dream:
        for index, section in enumerate(skill_registry.get_prompt_sections(compact=True)):
            capability_parts.append((f"skill_prompt.{index}", section))

    from mochi.config import BUBBLE_ENABLED
    if BUBBLE_ENABLED and not is_dream and not is_autonomous:
        bubble_inst = get_prompt("system_chat/_bubble")
        if bubble_inst:
            capability_parts.append(("bubble", bubble_inst))

    if history_timestamps and not is_dream and policy.recent_history:
        hist_ts_inst = get_prompt("system_chat/_history_timestamp")
        if hist_ts_inst:
            dynamic_live_context.append(
                ("history.timestamps", hist_ts_inst.replace("{{history_timestamps}}", history_timestamps))
            )
    if is_autonomous:
        today_parts = [("today.heading", "## 今天")]
        if habit_progress_context:
            today_parts.append(("habits.progress", f"### 习惯\n{habit_progress_context}"))
        if policy.diary_journal:
            today_parts.append(("diary.journal", f"### 日记\n{diary_journal or EMPTY_DIARY}"))
        from mochi.token_distribution import joined_parts
        detailed_parts["today"] = joined_parts(today_parts)
        dynamic_live_context.append(("today", "\n\n".join(body for _, body in today_parts)))
    elif "runtime_context" in modules:
        rendered_rc = _render_runtime_context(
            modules["runtime_context"], diary_status,
            diary_journal if policy.diary_journal and not bedtime_review else None,
        )
        if rendered_rc:
            dynamic_live_context.append(("diary.context", rendered_rc))

    if day_start_context:
        dynamic_live_context.append(("day_start.diaries", day_start_context))

    if conv_summary:
        dynamic_live_context.append(("history.summary", f"## 本次对话早期内容（摘要）\n{conv_summary}"))

    if recent_operations:
        dynamic_live_context.append(("operations", recent_operations))

    if recalled_memories:
        dynamic_live_context.append(
            ("memory.recall", _format_recalled_memories(recalled_memories))
        )

    if runtime_entry and runtime_entry.kind == "bedtime":
        if not bedtime_review:
            raise RuntimeError("Bedtime review is missing")
        trigger_labels = {
            "explicit": "用户刚刚亲自表达了晚安或准备睡觉",
            "silence": "夜间持续安静后，系统判断用户大概已经睡着",
            "resleep": "用户夜里短暂醒来后再次安静下来",
        }
        dynamic_live_context.extend([
            ("bedtime.trigger", trigger_labels[runtime_entry.trigger]),
            ("bedtime.review", bedtime_review),
        ])
    elif runtime_entry and runtime_entry.kind == "self_reminder":
        reminder_context = get_prompt("self_reminder_entry")
        if not reminder_context:
            raise RuntimeError("Self reminder entry prompt is missing")
        dynamic_live_context.append(
            ("self_reminder", reminder_context.replace(
                "{{intent}}", runtime_entry.intent or "",
            ).replace(
                "{{scheduled_for}}", runtime_entry.scheduled_for or "",
            ))
        )
    elif is_dream:
        dream_prompt = get_prompt("dream_entry")
        if not dream_prompt:
            raise RuntimeError("Dream maintenance entry prompt is missing")
        dynamic_live_context.append(
            ("dream.context", dream_prompt.replace("{{dream_context}}", dream_context))
        )

    from mochi.db import get_last_user_message_time
    last_msg_time = (
        get_last_user_message_time(user_id)
        if policy.temporal_context and not is_dream
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
            dynamic_live_context.append(("time.silence", f"用户上次发消息：{silence_label}"))
        except (ValueError, TypeError):
            pass

    stable = stable_identity + early_runtime_situation + capability_parts
    from mochi.day_start import calendar_horizon
    time_line = f"当前时间：{now_str}"
    horizon = calendar_horizon(now)
    if horizon:
        time_line += f"\n近期日历：{horizon}"
    turn_context = dynamic_live_context + [("time.current", time_line)]
    if token_parts is not None:
        from mochi.token_distribution import joined_parts
        for zone, sections in (("system", stable), ("turn_context", turn_context)):
            token_parts[zone] = [
                part
                for source, body in joined_parts(sections)
                for part in detailed_parts.get(source, [(source, body)])
            ]
    return (
        "\n\n".join(body for _, body in stable),
        "\n\n".join(body for _, body in turn_context),
    )


async def chat(
    message: IncomingMessage | None = None,
    *,
    runtime_entry: MainRuntimeEntry | None = None,
) -> ChatResult:
    from mochi import config
    from mochi.admin.admin_db import get_system_config

    entry = runtime_entry or (message.runtime_entry if message is not None else None)
    if message is None and entry is None:
        raise ValueError("chat requires an incoming message or runtime entry")
    turn_id = (
        entry.idempotency_key
        if entry and entry.kind in {"self_reminder", "dream", "free_time"}
        and entry.idempotency_key
        else uuid.uuid4().hex
    )
    user_id = message.user_id if message is not None else entry.user_id
    kind = entry.kind if entry else "chat"
    metadata = {
        **runtime_trace.code_version(),
        "transport": message.transport if message is not None else entry.transport,
        "owner_authorized": bool(message and message.owner_authorized),
        "input": message.text if message is not None else None,
        "entry": {
            key: value for key, value in asdict(entry).items()
            if key not in {"claim_token", "lease_until"}
        } if entry else None,
        "settings": {
            key: getattr(config, key)
            for key in (
                "AI_CHAT_MAX_COMPLETION_TOKENS", "TOOL_LOOP_MAX_ROUNDS",
                "TOOL_LOOP_TOTAL_TOOL_LIMIT", "TOOL_LOOP_PER_TOOL_LIMIT",
                "TOOL_ROUTER_ENABLED", "TOOL_ESCALATION_ENABLED",
            )
        },
        "bedtime_timeout_s": get_system_config("BEDTIME_ENTRY_TIMEOUT_S"),
        "heartbeat_timeout_s": get_system_config("LLM_HEARTBEAT_TIMEOUT_SECONDS"),
    }
    with runtime_trace.run_scope(turn_id, kind, user_id, metadata) as trace:
        with runtime_trace.span("runtime", "main"):
            result = await _chat(message, runtime_entry=entry, _turn_id=turn_id)
        result._trace_id = trace.trace_id
        runtime_trace.finish_run(
            trace.trace_id,
            "prepared" if result.disposition == "deliver" and (result.text or result.stickers)
            else result.disposition,
            result={
                "disposition": result.disposition,
                "successful_effects": result.successful_effects,
                "tool_audit": result.tool_audit,
                "text": result.text,
                "stickers": result.stickers,
            },
        )
        return result


async def _chat(
    message: IncomingMessage | None = None,
    *,
    runtime_entry: MainRuntimeEntry | None = None,
    _turn_id: str,
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
    preparation_started = time.monotonic()
    from mochi.config import (
        TOOL_LOOP_MAX_ROUNDS, AI_CHAT_MAX_COMPLETION_TOKENS,
        TOOL_ROUTER_ENABLED, TOOL_ESCALATION_ENABLED,
        TOOL_ESCALATION_MAX_PER_TURN, TOOL_LOOP_TOTAL_TOOL_LIMIT,
        TOOL_LOOP_PER_TOOL_LIMIT, DREAM_MAX_COMPLETION_TOKENS,
    )

    runtime_entry = runtime_entry or (
        message.runtime_entry if message is not None else None
    )
    if message is None and runtime_entry is None:
        raise ValueError("chat requires an incoming message or runtime entry")
    if (
        message is not None
        and runtime_entry is not None
        and runtime_entry.kind in {"self_reminder", "free_time", "dream"}
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
    attachment = message.file if message is not None else None
    is_bedtime = bool(runtime_entry and runtime_entry.kind == "bedtime")
    is_self_reminder = bool(
        runtime_entry and runtime_entry.kind == "self_reminder"
    )
    is_dream = bool(
        runtime_entry and runtime_entry.kind == "dream"
    )
    is_autonomous = bool(
        runtime_entry and runtime_entry.kind == "free_time"
    )
    prompt_policy = context_policy(runtime_entry)
    turn_id = _turn_id
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

    # Keep attachment bytes ephemeral. History only records a readable placeholder.
    stored_text = (
        f"[图片] {text}" if image else f"[文件] {text}" if attachment else text
    )
    if message is not None:
        save_message(user_id, "user", stored_text, turn_id=turn_id)

    # ── Parallel pre-fetch: router classification + DB queries ──
    tier = "main"
    with runtime_trace.span("preparation", "model_client"):
        client = get_client_for_tier(tier)
    routed_skill_names: list[str] = []

    # Pre-fetch habits (fast sync DB) — shared by router hint + system prompt
    habits = (
        []
        if is_bedtime or is_self_reminder or is_dream or is_autonomous
        else await runtime_trace.prepare("habit_list", list_habits, user_id)
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
            return await runtime_trace.prepare(
                "conversation",
                get_conversation_context,
                user_id,
                recent_turns,
                include_summary=prompt_policy.conversation_summary,
                include_standalone=prompt_policy.standalone_history,
                reasoning_source="" if is_autonomous else client.reasoning_source,
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
        return await runtime_trace.prepare(
            "memory_recall",
            _retrieve_memories_for_turn, text, user_id,
        )

    # ── Skill mode: /skilloff skips router + non-core tools ──
    from mochi.db import get_skill_mode
    from mochi.turn_tool_policy import (
        build_turn_tool_plan, compose_chat_tools, extend_session_tools,
    )
    with runtime_trace.span("preparation", "tool_plan"):
        skill_mode_off = get_skill_mode() == "off"
        turn_plan = build_turn_tool_plan(transport)
    tracks_tool_session = (
        message is not None and runtime_entry is None and not skill_mode_off
    )
    from mochi.tool_policy import filter_tools
    image_generation_available = bool(
        message is not None
        and runtime_entry is None
        and transport == "wechat"
        and message.owner_authorized
        and message.send_image is not None
        and (turn_plan.router_enabled or turn_plan.request_tools_enabled)
        and filter_tools(skill_registry.get_tools_by_tool_names(
            ["generate_and_send_image"], transport=transport,
        ))
    )
    excluded_skills = (
        frozenset() if image_generation_available
        else frozenset({"image_generation"})
    )
    escalation_available = (
        is_bedtime
        or is_self_reminder
        or is_autonomous
        or (not is_dream and turn_plan.request_tools_enabled)
    )
    _health_warning = ""

    dream_session = None
    if is_dream:
        from mochi.dream import create_dream_session
        dream_session = create_dream_session(
            user_id=user_id,
            logical_date=runtime_entry.logical_date or "",
            period_key=runtime_entry.period_key or "",
            channel_id=channel_id,
            transport=transport,
        )
        tools = dream_session.definitions()
        core_memory, conversation_context = await asyncio.gather(
            runtime_trace.prepare("core", read_core),
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
            runtime_trace.prepare("core", read_core),
            _safe_conversation_context(),
        )
        recalled_memories = []
        habits = []

    elif skill_mode_off:
        tools = list(turn_plan.resident_definitions)

        core_memory, conversation_context, recalled_memories = await asyncio.gather(
            runtime_trace.prepare("core", read_core),
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
            runtime_trace.prepare("core", read_core),
            _safe_conversation_context(),
            _safe_recalled_memories(),
        )

    elif turn_plan.router_enabled:
        from mochi.tool_router import classify_skills
        router_catalog = turn_plan.router_descriptions
        if not image_generation_available:
            router_catalog.pop("image_generation", None)
        # Launch router (with habits hint) + remaining DB fetches concurrently
        skill_names, core_memory, conversation_context, recalled_memories = await asyncio.gather(
            runtime_trace.prepare_async(
                "router",
                classify_skills(text, user_id=user_id, habits=habits,
                                transport=transport, catalog=router_catalog),
            ),
            runtime_trace.prepare("core", read_core),
            _safe_conversation_context(),
            _safe_recalled_memories(),
        )

        routed_skill_names = list(skill_names)

        from mochi.model_health import should_warn_user, get_warning_message
        if should_warn_user("lite"):
            _health_warning = get_warning_message("lite")

        tools = compose_chat_tools(
            user_id,
            turn_plan.resident_definitions,
            skill_registry.get_tools_by_names(
                skill_names, transport=transport, loads={"routed"},
            ),
            transport=transport,
            excluded_skills=excluded_skills,
        )
    else:
        # No explicit Lite assignment means no semantic pre-router. Main still
        # receives resident tools and request_tools when enabled.
        tools = (
            compose_chat_tools(
                user_id, turn_plan.resident_definitions, [],
                transport=transport, excluded_skills=excluded_skills,
            )
            if tracks_tool_session
            else list(turn_plan.resident_definitions)
        )
        core_memory, conversation_context, recalled_memories = await asyncio.gather(
            runtime_trace.prepare("core", read_core),
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
    from mochi.tool_policy import check as policy_check
    tools = filter_tools(tools)
    availability = runtime_trace.prepare_sync(
        "tool_availability", ToolAvailability.from_definitions,
        tools, source="initial",
    )

    # Build context
    active_tool_names = list(availability.names)
    capability_context = runtime_trace.prepare_sync(
        "capability_context", skill_registry.get_capability_context_for_tools,
        active_tool_names,
    )
    requestable_tools = (
        runtime_trace.prepare_sync(
            "requestable_tools", skill_registry.get_requestable_tool_lines,
            active_tool_names,
            transport=transport,
            excluded_skills=excluded_skills,
        )
        if escalation_available else ""
    )
    from mochi.skills.habit.handler import HabitSkill
    habit_skill = skill_registry.all_skills().get("habit")

    async def _habit_progress_context() -> str:
        if not isinstance(habit_skill, HabitSkill):
            return ""
        if is_autonomous:
            if filter_tools(skill_registry.get_tools_by_tool_names(
                ["habit_progress"], transport=transport,
            )):
                return await runtime_trace.prepare(
                    "habit_progress", habit_skill.daily_context, user_id,
                )
        elif "habit_progress" in availability.names:
            return await runtime_trace.prepare(
                "habit_progress", habit_skill.progress_context, user_id,
            )
        return ""

    habit_progress_context = await _habit_progress_context()
    habit_context_loaded = bool(habit_progress_context)

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
    diary_source_date, diary_today, diary_tomorrow = await runtime_trace.prepare(
        "diary",
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
    if dream_session:
        dream_session.expected_core = core_memory

    from mochi.db import get_day_conversation
    bedtime_review = ""
    receipt_history = history
    if is_bedtime:
        conversation = await runtime_trace.prepare(
            "bedtime_conversation",
            get_day_conversation, user_id, diary_source_date,
        )
        bedtime_review = bedtime_context(conversation, diary_today, diary_tomorrow)
        receipt_history = conversation["messages"]

    from mochi.tool_execution import recent_operations_context
    recent_operations = (
        ""
        if not prompt_policy.recent_operations
        else await runtime_trace.prepare(
            "recent_operations",
            recent_operations_context, user_id, receipt_history,
            include_autonomous=True,
        )
    )

    day_start_day: str | None = None
    day_start_context = ""
    if is_autonomous or (
        message is not None and runtime_entry is None and message.owner_authorized
    ):
        from mochi import day_start
        from mochi.config import TZ as _TZ

        day_start_day = await runtime_trace.prepare(
            "day_start_status",
            day_start.pending_day, datetime.now(_TZ),
        )
        if day_start_day:
            day_start_context = await runtime_trace.prepare(
                "day_start_diaries",
                day_start.context, day_start_day,
            )
        if not day_start_context:
            day_start_day = None
        elif not is_autonomous:
            after_delivery.append(
                lambda: day_start.mark_done(day_start_day)
            )

    token_parts: dict[str, list[tuple[str, str]]] = {}
    system_prompt, turn_context = runtime_trace.prepare_sync(
        "prompt", _build_prompt_zones,
        user_id, capability_context=capability_context,
        requestable_tools=requestable_tools, tool_names=active_tool_names,
        core_memory=core_memory, habits=habits, transport=transport,
        recalled_memories=recalled_memories,
        diary_status=_ds, diary_journal=_dj,
        conv_summary=(conv_summary or "") if prompt_policy.conversation_summary else "",
        recent_operations=recent_operations,
        runtime_entry=runtime_entry,
        dream_context=(
            dream_session.context.rendered if dream_session else ""
        ),
        policy=prompt_policy,
        habit_progress_context=habit_progress_context,
        history_timestamps=(
            "" if is_autonomous else _format_history_timestamps(history)
        ),
        day_start_context=day_start_context,
        bedtime_review=bedtime_review,
        token_parts=token_parts,
    )
    # User turns keep the system prompt stable so providers can reuse the
    # system + history prefix; runtime entries keep everything in system.
    split_turn_context = message is not None and runtime_entry is None
    if not split_turn_context:
        system_prompt = f"{system_prompt}\n\n{turn_context}"
        token_parts["system"].extend([
            ("separator", "\n\n"), *token_parts["turn_context"],
        ])
    runtime_trace.register_token_source("system", system_prompt, token_parts["system"])

    # Build messages array
    messages = [{"role": "system", "content": system_prompt}]
    if is_autonomous:
        messages.append({
            "role": "user",
            "content": _format_free_time_activation(history),
        })
    else:
        messages.extend(_expand_history(history))
    if image:
        _replace_current_user_content(messages, stored_text, _image_content(text, image))
        # Image understanding belongs to the configured Main model.
        tier = "main"
    elif attachment:
        _replace_current_user_content(
            messages, stored_text,
            await runtime_trace.prepare("attachment", _file_content, text, attachment),
        )
    current_input_index = None
    if split_turn_context:
        current_index = (
            len(messages) - 1 if messages[-1].get("role") == "user"
            else len(messages)
        )
        messages.insert(current_index, {
            "role": "user",
            "content": (
                '<turn_context source="runtime" role="read_only_context">\n'
                f"{turn_context}\n"
                "</turn_context>"
            ),
        })
        if current_index + 1 < len(messages):
            current_input_index = current_index + 1

    for index, context_message in enumerate(messages[1:], start=1):
        if split_turn_context and index == current_index:
            runtime_trace.register_token_source("user", context_message["content"], [
                ("turn_context.wrapper", '<turn_context source="runtime" role="read_only_context">\n'),
                *token_parts["turn_context"],
                ("turn_context.wrapper", "\n</turn_context>"),
            ])
            continue
        role = context_message["role"]
        source = (
            "runtime.activation_history" if is_autonomous
            else "input.current" if index == current_input_index
            else f"history.{role}"
        )
        body = context_message["content"]
        texts = (
            [body] if isinstance(body, str)
            else [block["text"] for block in body if block.get("type") == "text"]
        )
        for body in texts:
            runtime_trace.register_token_source(role, body, [(source, body)])

    runtime_trace.event("context_ready", {
        "preparation_ms": (time.monotonic() - preparation_started) * 1000,
        "policy": asdict(prompt_policy),
        "history_message_ids": [item["id"] for item in history],
        "core_sha256": hashlib.sha256(core_memory.encode()).hexdigest(),
        "diary_date": diary_source_date,
        "diary_visible": prompt_policy.diary_journal,
        "diary_sha256": hashlib.sha256(diary_today.encode()).hexdigest(),
        "habit_context": habit_progress_context,
        "tools": list(availability.names),
        "message_roles": [item["role"] for item in messages],
    })

    # ── LLM call with tool loop ──
    max_tool_rounds = TOOL_LOOP_MAX_ROUNDS
    tool_names_used: list[str] = []  # track for tool_history persistence
    tool_audit: list[dict] = []
    successful_effects = False
    recall_exposure_recorded = False
    bedtime_requested = False
    tool_budget = ToolLoopBudget()
    on_interim = message.on_interim if message is not None else None
    history_response: LLMResponse | None = None
    image_pending_review = False
    dream_protocol_errors: set[str] = set()

    def _log_main_usage(
        response: LLMResponse,
        *,
        usage_stage: str,
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
                else "dream"
                if is_dream
                else f"chat:{tier}"
            ),
            call_type=call_type,
            usage_stage=usage_stage,
            reasoning_tokens=response.reasoning_tokens,
            cached_prompt_tokens=response.cached_prompt_tokens,
            cache_write_tokens=response.cache_write_tokens,
        )

    def _free_time_cancelled() -> bool:
        if is_dream:
            from mochi.heartbeat import (
                TRANSITIONING, _state, chat_activity_generation, has_active_chat,
            )
            from mochi.admin.admin_db import get_system_config
            return bool(
                has_active_chat() or _state == TRANSITIONING
                or not get_system_config("WEEKLY_MAINTENANCE_ENABLED")
                or (
                    runtime_entry.chat_generation is not None
                    and runtime_entry.chat_generation != chat_activity_generation()
                )
            )
        if not is_autonomous:
            return False
        from mochi.heartbeat import free_time_turn_available

        return not free_time_turn_available(
            runtime_entry.chat_generation, runtime_entry.state_changed_at,
        )

    def _cancelled_result() -> ChatResult:
        return ChatResult(
            tool_audit=tool_audit, successful_effects=successful_effects,
            disposition="invalid" if is_dream else "handled" if successful_effects else "skip",
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
        if is_bedtime or bedtime_requested or is_self_reminder or is_autonomous:
            skipped = reply == "[SKIP]"
            if skipped:
                reply = ""
            if day_start_day and not bedtime_requested and (
                skipped or reply or pending_stickers or successful_effects
            ):
                from mochi import day_start

                day_start.mark_done(day_start_day)
            visible = bool(reply or pending_stickers)
            disposition = (
                "deliver" if visible else "handled" if successful_effects
                else "skip" if skipped else "invalid"
            )
            if is_bedtime or bedtime_requested:
                log.info(
                    "Bedtime Main result: turn=%s disposition=%s effects=%s tools=%s",
                    turn_id, disposition, successful_effects, tool_audit,
                )
            pending_history = (
                None
                if not visible or (is_autonomous and not reply)
                else {
                    "user_id": user_id,
                    "content": reply or "[贴纸]",
                    "tool_history": tool_history_json,
                    "turn_id": turn_id,
                    "processed": message is None,
                    **reasoning_metadata,
                }
            )
            return ChatResult(
                text=reply,
                stickers=pending_stickers,
                tool_audit=tool_audit,
                successful_effects=successful_effects,
                disposition=disposition,
                bedtime_requested=bedtime_requested,
                _after_delivery=list(after_delivery) if final_reply else [],
                _pending_history=pending_history,
            )
        if is_dream:
            complete = bool(
                final_reply and history_response
                and history_response.finish_reason in {"stop", "end_turn", "completed"}
                and not dream_session.failures and not dream_protocol_errors
            )
            return ChatResult(
                tool_audit=tool_audit,
                successful_effects=successful_effects,
                disposition=("handled" if successful_effects else "skip") if complete else "invalid",
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

    for round_num in range(max_tool_rounds + 1):
        if round_num == max_tool_rounds and not image_pending_review:
            break
        reviewing_image = round_num == max_tool_rounds
        image_pending_review = False
        generated_images: list[ImageAttachment] = []
        if _free_time_cancelled():
            return _cancelled_result()
        if dream_session:
            dream_session.advance_visible_context()
        if not habit_context_loaded:
            habit_progress_context = await _habit_progress_context()
            if habit_progress_context:
                messages.append({
                    "role": "system",
                    "content": (
                        f"### 习惯\n{habit_progress_context}" if is_autonomous
                        else f"## 本轮习惯进度快照（只读事实）\n{habit_progress_context}"
                    ),
                })
                runtime_trace.register_token_source("system", messages[-1]["content"], [
                    ("habits.progress", messages[-1]["content"]),
                ])
                habit_context_loaded = True
        document_updates: dict[str, str] = {}
        availability = availability.refresh_extensions(transport)
        round_availability = (
            ToolAvailability() if reviewing_image else availability
        )
        for _attempt in range(2):
            if _free_time_cancelled():
                return _cancelled_result()
            call_started = time.monotonic()
            try:
                with runtime_trace.stage(
                    f"main.round_{round_num + 1}.attempt_{_attempt + 1}",
                    operation_id=f"main.round_{round_num + 1}",
                ):
                    response = await asyncio.to_thread(
                        client.chat,
                        messages=messages,
                        tools=(
                            round_availability.provider_tools()
                            if round_availability.entries
                            else None
                        ),
                        max_tokens=DREAM_MAX_COMPLETION_TOKENS if is_dream else AI_CHAT_MAX_COMPLETION_TOKENS,
                    )
                break
            except asyncio.CancelledError:
                log.warning(
                    "Main call cancelled: entry=%s turn=%s round=%d attempt=%d elapsed=%.1fs",
                    runtime_entry.kind if runtime_entry else "chat",
                    turn_id, round_num + 1, _attempt + 1,
                    time.monotonic() - call_started,
                )
                raise
            except Exception as e:
                if _attempt == 0:
                    log.warning("LLM call failed (attempt 1), retrying: %s", e)
                    continue
                log.error("LLM call failed (attempt 2): %s", e, exc_info=True)
                if is_bedtime or bedtime_requested:
                    return ChatResult(
                        disposition="invalid", bedtime_requested=bedtime_requested,
                        tool_audit=tool_audit, successful_effects=successful_effects,
                    )
                if is_self_reminder or is_autonomous:
                    return ChatResult(disposition="invalid")
                if is_dream:
                    raise
                if image:
                    return ChatResult(
                        text=(
                            "图片处理失败了。请确认管理后台配置的 Chat 模型支持图片，"
                            "或换一张图片再试。"
                        )
                    )
                return ChatResult(text=f"API 报错：{e}")

        _log_main_usage(
            response,
            usage_stage="initial" if round_num == 0 else "tool_continuation",
        )
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
            return _final_result(reply)

        # Add assistant message with tool_calls to context
        tool_messages_start = len(messages)
        executed_call_ids: set[str] = set()
        assistant_msg = {"role": "assistant", "content": response.content or ""}
        if response.reasoning_content:
            assistant_msg["reasoning_content"] = response.reasoning_content
        if response.response_items:
            assistant_msg["response_items"] = response.response_items
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
                        excluded_skills=excluded_skills,
                    )
                next_availability = next_availability.with_definitions(
                    additions, source=f"request_round_{round_num + 1}",
                )
                if tracks_tool_session:
                    extend_session_tools(user_id, additions)
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
                    conversation = await asyncio.to_thread(
                        get_day_conversation, user_id, diary_source_date,
                    )
                    tomorrow_date = diary_target_dates["tomorrow"]
                    visible_tomorrow = document_updates.get(
                        tomorrow_date, diary_expected[tomorrow_date],
                    )
                    if visible_tomorrow is None:
                        visible_tomorrow = diary_tomorrow
                        document_updates.setdefault(tomorrow_date, visible_tomorrow)
                    review = bedtime_context(
                        conversation,
                        document_updates.get(
                            diary_source_date, diary_expected[diary_source_date],
                        ),
                        visible_tomorrow,
                    )
                    bedtime_requested = True
                    result_text = json.dumps({
                        "ok": True,
                        "bedtime_requested": True,
                        "message": "Bedtime will begin after this turn.",
                        "bedtime_context": review,
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

            is_dream_tool = bool(
                dream_session and dream_session.owns(tc["name"])
            )
            if is_dream and not is_dream_tool:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": model_result_for(SkillResult(
                        output="Tool is outside the Dream entry scope.",
                        success=False,
                        error_code="tool_outside_runtime_scope",
                        retryable=False,
                    )),
                })
                continue
            if not is_dream_tool:
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
                action_for, outcome_for, retained_result_for, serialized_arguments,
            )
            skill_name = (
                "memory"
                if is_dream_tool
                else round_availability.binding_for(tc["name"]).name
                if round_availability.binding_for(tc["name"]) is not None
                else skill_registry.get_tool_skill(tc["name"]) or ""
            )
            execution_source = (
                "dream" if is_dream_tool
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
            executed_call_ids.add(tc["id"])
            trace_tool = runtime_trace.start_tool(
                execution_id, tc["name"], arguments,
                document_target=(
                    diary_target_dates[arguments.get("day", "today")]
                    if tc["name"] == "write_diary"
                    else "core" if tc["name"] == "update_core"
                    else None
                ),
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
                if is_dream_tool:
                    result = await dream_session.execute(
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
                if (
                    tc["name"] == "generate_and_send_image"
                    and result.success and result.image is not None
                ):
                    if image_generation_available and message and message.send_image:
                        try:
                            await message.send_image(result.image)
                        except DeliveryError as exc:
                            log.warning("Generated image delivery %s: %s", exc.outcome, exc)
                            result.success = False
                            result.output = "图片已生成，但发送未确认；未自动重试。"
                            result.error_code = exc.outcome
                            result.retryable = False
                            result.state_change_unknown = exc.outcome == "delivery_unknown"
                        except Exception:
                            log.exception("Generated image delivery failed")
                            result.success = False
                            result.output = "图片已生成，但发送未确认；未自动重试。"
                            result.error_code = "delivery_unknown"
                            result.retryable = False
                            result.state_change_unknown = True
                    else:
                        result.success = False
                        result.output = "当前无法发送图片；未调用生图服务。"
                        result.error_code = "image_unavailable"
                        result.retryable = False
                    if image_generation_available:
                        generated_images.append(result.image)
                    if result.success:
                        result.output = "图片已发送。"
                        result.state_changed = True
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
                    result_json=(
                        retained_result_for(tc["name"], result)
                        if not is_dream_tool else None
                    ),
                )
                runtime_trace.finish_tool(trace_tool, result)
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
                if tc["name"] in {"update_core", "view_core_memory", "update_dream_core"}:
                    document_updates["core"] = result.document_snapshot
                elif tc["name"] == "write_diary":
                    target = diary_target_dates[arguments.get("day", "today")]
                    document_updates[target] = result.document_snapshot
                elif tc["name"] == "read_diary" and not arguments.get("date"):
                    document_updates[diary_source_date] = result.document_snapshot

        paired_results = [
            item for item in messages[tool_messages_start:] if item["role"] == "tool"
        ]
        if is_dream:
            names = {call["id"]: call["name"] for call in response.tool_calls}
            for paired in paired_results:
                facts = json.loads(paired["content"])
                name = names[paired["tool_call_id"]]
                if facts.get("ok") is False and paired["tool_call_id"] not in executed_call_ids:
                    dream_protocol_errors.add(name)
                elif facts.get("ok") is True:
                    dream_protocol_errors.discard(name)
        runtime_trace.event(
            "tool_results", {"round": round_num + 1}, paired_results,
            facts=runtime_trace.tool_rejection_facts(
                response.tool_calls, paired_results, executed_call_ids,
            ),
        )
        for generated_image in generated_images:
            messages.append({
                "role": "user",
                "content": _image_content(
                    "这是你刚通过工具生成的图片，不是用户发来的新消息。",
                    generated_image,
                ),
            })
        image_pending_review = bool(generated_images)
        # Only advance snapshots after their results become visible to Main.
        core_expected = document_updates.pop("core", core_expected)
        diary_expected.update(document_updates)
        if dream_session:
            dream_session.expected_core = core_expected
        availability = next_availability

    # If we exhausted tool rounds, return whatever we have
    reply = STICKER_RE.sub("", response.content or "").strip()
    if not reply and not (
        is_bedtime or bedtime_requested or is_self_reminder or is_dream or is_autonomous
    ):
        reply = "处理过程出了点问题，你再说一次试试？"
    if _health_warning and reply:
        reply += _health_warning
    return _final_result(reply, final_reply=False)
