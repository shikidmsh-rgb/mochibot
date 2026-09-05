"""Continuous personality-free Lite extraction of durable Memory Items."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading

from mochi.config import MEMORY_EXTRACTION_BATCH_TURNS, OWNER_USER_ID
from mochi.core_store import read_core
from mochi.db import (
    commit_memory_extraction_batch,
    get_memory_extraction_batch,
    get_memory_extraction_references,
    get_memory_extraction_status,
    list_memory_extraction_users,
    log_usage,
    record_memory_extraction_error,
)
from mochi.llm import get_client_for_tier
from mochi.memory_contract import (
    memory_contents_equal,
    normalize_evidence_message_ids,
    validate_memory_content,
    validate_memory_importance,
)
from mochi.model_pool import get_pool
from mochi.prompt_loader import get_prompt


log = logging.getLogger(__name__)

EXTRACTION_BATCH_SIZE = MEMORY_EXTRACTION_BATCH_TURNS
_RUNNING_TASKS: dict[int, asyncio.Task] = {}
_PENDING_WAKEUPS: set[int] = set()
_TASKS_LOCK = threading.Lock()


class MemoryExtractionContractError(ValueError):
    """The Lite response cannot safely advance the extraction cursor."""


def _tool_receipts(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [
        str(item["name"])[:100]
        for item in parsed
        if isinstance(item, dict) and item.get("name")
    ][:20]


def _conversation_payload(batch: list[dict]) -> list[dict]:
    return [
        {
            "id": message["id"],
            "role": message["role"],
            "content": message["content"],
            "created_at": message["created_at"],
            "tool_receipts": (
                _tool_receipts(message.get("tool_history"))
                if message["role"] == "assistant"
                else []
            ),
        }
        for message in batch
    ]


def validate_extraction_response(raw: str, batch: list[dict]) -> list[dict]:
    """Parse and validate the complete response or reject the whole batch."""
    text = (raw or "").strip()
    fence = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise MemoryExtractionContractError(f"invalid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise MemoryExtractionContractError(
            "top-level response must be an array"
        )

    batch_user_ids = {
        message["id"] for message in batch if message["role"] == "user"
    }
    required = {"content", "importance", "evidence_message_ids"}
    validated: list[dict] = []
    for index, candidate in enumerate(parsed):
        if not isinstance(candidate, dict) or set(candidate) != required:
            raise MemoryExtractionContractError(
                f"candidate {index}: fields must be exactly "
                f"{', '.join(sorted(required))}"
            )
        try:
            content = validate_memory_content(candidate["content"])
            importance = validate_memory_importance(candidate["importance"])
            evidence = normalize_evidence_message_ids(
                candidate["evidence_message_ids"]
            )
        except ValueError as exc:
            raise MemoryExtractionContractError(
                f"candidate {index}: {exc}"
            ) from exc
        if (
            not evidence
            or any(message_id not in batch_user_ids for message_id in evidence)
        ):
            raise MemoryExtractionContractError(
                f"candidate {index}: evidence must reference same-user "
                "user messages from this batch"
            )
        validated.append({
            "content": content,
            "importance": importance,
            "evidence_message_ids": list(evidence),
        })
    return validated


def _filter_batch_duplicates(candidates: list[dict]) -> list[dict]:
    kept: list[dict] = []
    for candidate in candidates:
        if not any(
            memory_contents_equal(candidate["content"], existing["content"])
            for existing in kept
        ):
            kept.append(candidate)
    return kept


def _filter_core_duplicates(candidates: list[dict], core: str) -> list[dict]:
    core_lines = [
        re.sub(r"^[-*]\s+", "", line.strip())
        for line in core.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return [
        candidate
        for candidate in candidates
        if not memory_contents_equal(candidate["content"], core)
        and not any(
            memory_contents_equal(candidate["content"], line)
            for line in core_lines
        )
    ]


def _attach_embeddings(candidates: list[dict]) -> list[dict]:
    """Optionally embed candidates before opening the write transaction."""
    if not candidates:
        return candidates
    try:
        embeddings = get_pool().embed_batch([
            candidate["content"] for candidate in candidates
        ])
    except Exception as exc:
        log.warning("Memory extraction embedding failed: %s", exc)
        embeddings = [None] * len(candidates)
    if len(embeddings) != len(candidates):
        log.warning(
            "Memory extraction embedding count mismatch: expected %d, got %d",
            len(candidates),
            len(embeddings),
        )
        embeddings = [None] * len(candidates)
    return [
        {**candidate, "embedding": embedding}
        for candidate, embedding in zip(candidates, embeddings)
    ]


def _run_batch(user_id: int, cursor: int, batch: list[dict]) -> list[int]:
    prompt = get_prompt("memory_extract")
    if not prompt:
        raise RuntimeError("memory_extract prompt is unavailable")

    core = read_core()
    payload = {
        "conversation_batch": _conversation_payload(batch),
        "existing_facts_reference": {
            "core": core[:6000],
            "memory_items": get_memory_extraction_references(user_id),
        },
    }
    response = get_client_for_tier("lite").chat(
        messages=[
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ],
        tools=None,
        temperature=0.1,
        max_tokens=1200,
    )
    if response.total_tokens:
        log_usage(
            response.prompt_tokens,
            response.completion_tokens,
            response.total_tokens,
            model=response.model,
            purpose="memory_extract",
            model_role="LITE",
            call_type="background",
            usage_stage="fixed_batch",
            reasoning_tokens=response.reasoning_tokens,
            cached_prompt_tokens=response.cached_prompt_tokens,
        )

    candidates = validate_extraction_response(response.content, batch)
    candidates = _filter_batch_duplicates(candidates)
    candidates = _filter_core_duplicates(candidates, core)
    candidates = _attach_embeddings(candidates)
    inserted = commit_memory_extraction_batch(
        user_id,
        expected_cursor=cursor,
        through_message_id=batch[-1]["id"],
        batch_user_message_ids=[
            message["id"]
            for message in batch
            if message["role"] == "user"
        ],
        memories=candidates,
    )
    log.info(
        "Memory extraction processed messages %d-%d: %d candidates, %d inserted",
        batch[0]["id"],
        batch[-1]["id"],
        len(candidates),
        len(inserted),
    )
    return inserted


def drain_memory_extraction(user_id: int = 0) -> int:
    """Drain exact complete-turn batches, stopping safely on extraction failure."""
    uid = user_id or OWNER_USER_ID
    inserted_count = 0
    while True:
        cursor, batch = get_memory_extraction_batch(
            uid, EXTRACTION_BATCH_SIZE,
        )
        if not batch:
            return inserted_count
        try:
            inserted = _run_batch(uid, cursor, batch)
            inserted_count += len(inserted)
        except Exception as exc:
            log.error(
                "Memory extraction batch failed: %s",
                exc,
                exc_info=True,
            )
            record_memory_extraction_error(
                uid, f"{type(exc).__name__}: {exc}",
            )
            return inserted_count
def schedule_memory_extraction(user_id: int = 0) -> bool:
    """Start one non-blocking worker per user when work is durable."""
    uid = user_id or OWNER_USER_ID
    try:
        status = get_memory_extraction_status(
            uid, EXTRACTION_BATCH_SIZE,
        )
    except Exception:
        log.exception("Could not inspect memory extraction state")
        return False
    if status["pending_turns"] < EXTRACTION_BATCH_SIZE:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False

    with _TASKS_LOCK:
        current = _RUNNING_TASKS.get(uid)
        if current and not current.done():
            _PENDING_WAKEUPS.add(uid)
            return False
        task = loop.create_task(
            asyncio.to_thread(drain_memory_extraction, uid),
            name=f"memory-extraction-{uid}",
        )
        _RUNNING_TASKS[uid] = task

    def _finished(finished: asyncio.Task) -> None:
        should_recheck = False
        with _TASKS_LOCK:
            if _RUNNING_TASKS.get(uid) is finished:
                _RUNNING_TASKS.pop(uid, None)
            if uid in _PENDING_WAKEUPS:
                _PENDING_WAKEUPS.remove(uid)
                should_recheck = True
        try:
            finished.result()
        except Exception:
            log.exception("Unexpected memory extraction worker failure")
        if should_recheck:
            schedule_memory_extraction(uid)

    task.add_done_callback(_finished)
    return True


def resume_memory_extractions() -> None:
    """Recover pending extraction work for every known user."""
    for user_id in list_memory_extraction_users():
        schedule_memory_extraction(user_id)
