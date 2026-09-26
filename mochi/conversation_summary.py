"""Non-blocking continuous summaries over complete ordinary chat turns."""

from __future__ import annotations

import asyncio
import logging

from mochi.config import CONV_SUMMARY_BATCH_TURNS
from mochi.db import (
    get_conversation_summary_batch,
    get_conversation_summary_status,
    list_conversation_summary_users,
    log_usage,
    record_conversation_summary_error,
    save_conversation_summary,
)
from mochi.llm import get_client_for_tier
from mochi.prompt_loader import get_prompt
from mochi.token_estimator import estimate_tokens


log = logging.getLogger(__name__)

SUMMARY_BATCH_SIZE = CONV_SUMMARY_BATCH_TURNS
SUMMARY_CONTEXT_MAX_TOKENS = 16_000
SUMMARY_GENERATION_MIN_TOKENS = 1_200
_TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})
_tasks: dict[int, asyncio.Task] = {}


class SummaryContextError(ValueError):
    """The complete batch does not fit the bounded summary context."""


def _summary_input(claim: dict) -> str:
    previous = claim["summary"].strip() or "(empty)"
    lines = [
        "<previous_summary>",
        previous,
        "</previous_summary>",
        "",
        "<new_turns>",
    ]
    for turn in claim["turns"]:
        user = turn["user"]
        assistant = turn["assistant"]
        lines.extend([
            f"[{user['created_at']}] user: {user['content']}",
            f"[{assistant['created_at']}] assistant: {assistant['content']}",
        ])
    lines.append("</new_turns>")
    return "\n".join(lines)


def _normalize_summary(value: str) -> str:
    return " ".join((value or "").split())


def _fits_context(prompt: str, summary_input: str, output_tokens: int) -> bool:
    return (
        estimate_tokens(prompt) + estimate_tokens(summary_input) + output_tokens + 256
        <= SUMMARY_CONTEXT_MAX_TOKENS
    )


def _needs_compression(response, summary: str, max_tokens: int) -> bool:
    return (
        (response.finish_reason or "").strip().lower() in _TRUNCATED_FINISH_REASONS
        or estimate_tokens(summary) > max_tokens
    )


def _log_response_usage(response, stage: str) -> None:
    if response.total_tokens:
        log_usage(
            response.prompt_tokens, response.completion_tokens, response.total_tokens,
            model=response.model, purpose="conversation_summary",
            model_role="LITE", call_type="background", usage_stage=stage,
            reasoning_tokens=response.reasoning_tokens,
            cached_prompt_tokens=response.cached_prompt_tokens,
            cache_write_tokens=response.cache_write_tokens,
        )


async def _generate_summary(claim: dict) -> str:
    from mochi.config import CONV_SUMMARY_MAX_TOKENS

    base_prompt = get_prompt("conv_summary")
    if not base_prompt:
        raise RuntimeError("Conversation summary prompt is missing")
    prompt = (
        f"{base_prompt}\n\n"
        f"最终摘要控制在约 {CONV_SUMMARY_MAX_TOKENS} 个中文字符以内，用完整句子收束。"
    )
    generation_tokens = max(SUMMARY_GENERATION_MIN_TOKENS, CONV_SUMMARY_MAX_TOKENS * 4)
    summary_input = _summary_input(claim)
    if not _fits_context(prompt, summary_input, generation_tokens):
        raise SummaryContextError("Conversation summary input exceeds bounded context")
    client = get_client_for_tier("lite")
    response = await asyncio.to_thread(
        client.chat,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": summary_input},
        ],
        tools=None,
        max_tokens=generation_tokens,
        temperature=0.2,
    )
    _log_response_usage(response, "rolling_update")
    summary = _normalize_summary(response.content or "")
    if not _needs_compression(response, summary, CONV_SUMMARY_MAX_TOKENS):
        return summary
    prompt += (
        "\n\n上一次输出触及长度限制。请重新阅读全部输入，用更凝练的完整句子"
        "覆盖其中的重要事实；不要续写或依赖上一次未完成的草稿。"
    )
    if not _fits_context(prompt, summary_input, generation_tokens):
        raise SummaryContextError("Conversation summary retry exceeds bounded context")
    response = await asyncio.to_thread(
        client.chat,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": summary_input},
        ],
        tools=None, max_tokens=generation_tokens, temperature=0.1,
    )
    _log_response_usage(response, "compression_retry")
    summary = _normalize_summary(response.content or "")
    if _needs_compression(response, summary, CONV_SUMMARY_MAX_TOKENS):
        raise ValueError("Lite returned another truncated conversation summary")
    return summary


async def _drain_user(user_id: int) -> None:
    failed = False
    batch_size = SUMMARY_BATCH_SIZE
    try:
        while True:
            claim = await asyncio.to_thread(
                get_conversation_summary_batch,
                user_id,
                batch_size,
            )
            if claim is None:
                if batch_size < SUMMARY_BATCH_SIZE:
                    status = await asyncio.to_thread(
                        get_conversation_summary_status, user_id, SUMMARY_BATCH_SIZE,
                    )
                    if status["pending_turns"]:
                        batch_size = min(batch_size, status["pending_turns"])
                        continue
                return
            try:
                summary = await _generate_summary(claim)
                if not summary:
                    raise ValueError(
                        "Lite returned an empty conversation summary"
                    )
            except Exception as exc:
                if isinstance(exc, SummaryContextError) and batch_size > 1:
                    batch_size = max(1, batch_size // 2)
                    log.info("Reducing summary batch to %d complete turns", batch_size)
                    continue
                failed = True
                await asyncio.to_thread(
                    record_conversation_summary_error,
                    claim,
                    f"{type(exc).__name__}: {exc}",
                )
                log.warning(
                    "Conversation summary failed for user %d: %s",
                    user_id,
                    exc,
                )
                return

            saved = await asyncio.to_thread(
                save_conversation_summary, claim, summary,
            )
            if saved:
                log.info(
                    "Conversation summary advanced for user %d through message %d",
                    user_id,
                    claim["next_through_message_id"],
                )
    finally:
        current = asyncio.current_task()
        if _tasks.get(user_id) is current:
            del _tasks[user_id]
        if not failed:
            status = await asyncio.to_thread(
                get_conversation_summary_status,
                user_id,
                SUMMARY_BATCH_SIZE,
            )
            if status["pending_turns"] >= SUMMARY_BATCH_SIZE:
                asyncio.get_running_loop().call_soon(
                    schedule_conversation_summary, user_id,
                )


def schedule_conversation_summary(user_id: int) -> asyncio.Task | None:
    """Start one non-blocking worker per user; duplicate wakeups coalesce."""
    existing = _tasks.get(user_id)
    if existing is not None and not existing.done():
        return existing
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.debug("Conversation summary scheduling skipped without an event loop")
        return None
    task = loop.create_task(
        _drain_user(user_id),
        name=f"conversation-summary-{user_id}",
    )
    _tasks[user_id] = task
    return task


def resume_conversation_summaries() -> None:
    """Recover per-user backlog from durable state after startup."""
    for user_id in list_conversation_summary_users():
        schedule_conversation_summary(user_id)
