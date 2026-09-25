"""Typed context for system-owned turns entering the Main runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal


BedtimeTrigger = Literal["explicit", "silence", "resleep"]
BEDTIME_ROUTED_SKILLS = ("todo", "reminder")
RuntimeEntryKind = Literal[
    "bedtime",
    "self_reminder",
    "weekly_maintenance",
    "free_time",
]


@dataclass(frozen=True)
class ContextPolicy:
    """Explicit prompt and message context policy for one Main entry."""

    early_runtime_situation: bool = False
    diary_status: bool = False
    diary_journal: bool = True
    conversation_summary: bool = True
    recent_history: bool = True
    standalone_history: bool = True
    recent_turns: int | None = None
    trailing_history: bool = True
    auto_recall: bool = True
    recent_operations: bool = True
    prompt_sections: bool = True
    temporal_context: bool = True


def context_policy(entry: "MainRuntimeEntry | None") -> ContextPolicy:
    if entry is None:
        return ContextPolicy()
    if entry.kind == "bedtime":
        return ContextPolicy(
            conversation_summary=False,
            recent_history=False,
            auto_recall=False,
        )
    if entry.kind == "free_time":
        return ContextPolicy(
            early_runtime_situation=True,
            diary_journal=False,
            conversation_summary=False,
            recent_history=True,
            recent_turns=2,
            trailing_history=False,
            auto_recall=False,
            prompt_sections=False,
            temporal_context=True,
        )
    if entry.kind == "weekly_maintenance":
        return ContextPolicy(
            diary_journal=False,
            conversation_summary=True,
            standalone_history=False,
            auto_recall=False,
            recent_operations=False,
            prompt_sections=False,
            temporal_context=False,
        )
    return ContextPolicy()


@dataclass(frozen=True)
class MainRuntimeEntry:
    """A non-chat situation handled by the standard Main personality."""

    kind: RuntimeEntryKind
    user_id: int
    channel_id: int
    transport: str
    trigger: BedtimeTrigger | None = None
    logical_date: str | None = None
    period_key: str | None = None
    reminder_id: int | None = None
    scheduled_for: str | None = None
    intent: str | None = None
    idempotency_key: str | None = None
    claim_token: str | None = None
    lease_until: str | None = None
    run_key: str | None = None
    wake_reason: str | None = None
    chat_generation: int | None = None
    state_changed_at: str | None = None

    @classmethod
    def bedtime(
        cls,
        *,
        trigger: BedtimeTrigger,
        user_id: int,
        channel_id: int,
        transport: str,
    ) -> "MainRuntimeEntry":
        return cls(
            kind="bedtime",
            user_id=user_id,
            channel_id=channel_id,
            transport=transport,
            trigger=trigger,
        )

    @classmethod
    def self_reminder(
        cls,
        *,
        reminder_id: int,
        scheduled_for: str,
        intent: str,
        user_id: int,
        channel_id: int,
        transport: str,
        claim_token: str,
        lease_until: str,
    ) -> "MainRuntimeEntry":
        if isinstance(reminder_id, bool) or not isinstance(reminder_id, int):
            raise ValueError("self reminder id must be an integer")
        if reminder_id <= 0:
            raise ValueError("self reminder id must be positive")
        if not isinstance(scheduled_for, str) or not scheduled_for.strip():
            raise ValueError("self reminder scheduled time must not be empty")
        try:
            datetime.fromisoformat(scheduled_for)
        except ValueError as exc:
            raise ValueError(
                "self reminder scheduled time must be ISO 8601"
            ) from exc
        if not isinstance(intent, str) or not intent.strip():
            raise ValueError("self reminder intent must not be empty")
        if not isinstance(claim_token, str) or not claim_token.strip():
            raise ValueError("self reminder claim token must not be empty")
        if not isinstance(lease_until, str) or not lease_until.strip():
            raise ValueError("self reminder lease must not be empty")
        try:
            datetime.fromisoformat(lease_until)
        except ValueError as exc:
            raise ValueError("self reminder lease must be ISO 8601") from exc
        stable_key = (
            f"self-reminder:{reminder_id}:{scheduled_for.strip()}"
        )
        return cls(
            kind="self_reminder",
            user_id=user_id,
            channel_id=channel_id,
            transport=transport,
            reminder_id=reminder_id,
            scheduled_for=scheduled_for.strip(),
            intent=intent.strip(),
            idempotency_key=stable_key,
            claim_token=claim_token.strip(),
            lease_until=lease_until.strip(),
        )

    @classmethod
    def weekly_maintenance(
        cls,
        *,
        logical_date: str,
        period_key: str,
        user_id: int,
        channel_id: int,
        transport: str,
    ) -> "MainRuntimeEntry":
        logical_day = date.fromisoformat(logical_date)
        if logical_day.weekday() != 0:
            raise ValueError("weekly logical date must be Monday")
        iso = logical_day.isocalendar()
        expected_period = f"{iso.year}-W{iso.week:02d}"
        if period_key != expected_period:
            raise ValueError("weekly period key does not match logical date")
        return cls(
            kind="weekly_maintenance",
            user_id=user_id,
            channel_id=channel_id,
            transport=transport,
            logical_date=logical_date,
            period_key=period_key,
            idempotency_key=f"weekly-maintenance:{user_id}:{period_key}",
        )

    @classmethod
    def free_time(
        cls,
        *,
        run_key: str,
        wake_reason: str,
        user_id: int,
        channel_id: int,
        transport: str,
        claim_token: str,
        lease_until: str,
        chat_generation: int | None = None,
        state_changed_at: str | None = None,
    ) -> "MainRuntimeEntry":
        kind = "free_time"
        for label, value in (
            ("run key", run_key),
            ("wake reason", wake_reason),
            ("claim token", claim_token),
            ("lease", lease_until),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{kind} {label} must not be empty")
        datetime.fromisoformat(lease_until)
        return cls(
            kind=kind,
            user_id=user_id,
            channel_id=channel_id,
            transport=transport,
            idempotency_key=run_key.strip(),
            run_key=run_key.strip(),
            wake_reason=wake_reason.strip(),
            claim_token=claim_token.strip(),
            lease_until=lease_until.strip(),
            chat_generation=chat_generation,
            state_changed_at=state_changed_at,
        )


@dataclass(frozen=True)
class DurableChatResult:
    """Serializable Main output stored before crossing a transport boundary."""

    text: str = ""
    stickers: tuple[str, ...] = ()
    pending_history: dict | None = None
    tool_audit: tuple[dict, ...] = ()
    successful_effects: bool = False
    disposition: Literal["deliver", "skip", "handled", "invalid"] = "deliver"

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": 1,
                "text": self.text,
                "stickers": list(self.stickers),
                "pending_history": self.pending_history,
                "tool_audit": list(self.tool_audit),
                "successful_effects": self.successful_effects,
                "disposition": self.disposition,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> "DurableChatResult":
        payload = json.loads(value)
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("unsupported durable chat result")
        text = payload.get("text", "")
        stickers = payload.get("stickers", [])
        pending_history = payload.get("pending_history")
        tool_audit = payload.get("tool_audit", [])
        disposition = payload.get("disposition", "invalid")
        if not isinstance(text, str):
            raise ValueError("durable chat result text must be a string")
        if not isinstance(stickers, list) or not all(
            isinstance(item, str) and item for item in stickers
        ):
            raise ValueError("durable chat result stickers are invalid")
        if pending_history is not None and not isinstance(pending_history, dict):
            raise ValueError("durable chat result history is invalid")
        if not isinstance(tool_audit, list) or not all(
            isinstance(item, dict) for item in tool_audit
        ):
            raise ValueError("durable chat result tool audit is invalid")
        if disposition not in {"deliver", "skip", "handled", "invalid"}:
            raise ValueError("durable chat result disposition is invalid")
        return cls(
            text=text,
            stickers=tuple(stickers),
            pending_history=pending_history,
            tool_audit=tuple(tool_audit),
            successful_effects=bool(payload.get("successful_effects")),
            disposition=disposition,
        )
