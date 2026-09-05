"""Validation and provenance helpers for durable Memory Items."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence


MAX_MEMORY_CONTENT_CHARS = 80
MAX_EVIDENCE_MESSAGE_IDS = 20

_ATOMIC_SEPARATOR_RE = re.compile(r"[。！？!?；;\n\r]")
_SUBJECT_PREFIX_RE = re.compile(
    r"^(?:用户|user\b|the user\b)",
    re.IGNORECASE,
)


def memory_contents_equal(left: str, right: str) -> bool:
    """Ignore whitespace differences, but preserve all other content."""
    words = left.split()
    return bool(words) and words == right.split()


def validate_memory_content(content: object) -> str:
    if not isinstance(content, str):
        raise ValueError("memory content must be a string")
    cleaned = content.strip()
    if not 1 <= len(cleaned) <= MAX_MEMORY_CONTENT_CHARS:
        raise ValueError(
            f"memory content must contain 1-{MAX_MEMORY_CONTENT_CHARS} characters"
        )
    if cleaned.startswith(("-", "*", "•")) or _SUBJECT_PREFIX_RE.match(cleaned):
        raise ValueError("memory content must be one subject-free fact")
    body = cleaned.rstrip("。！？!?")
    if _ATOMIC_SEPARATOR_RE.search(body):
        raise ValueError("memory content must contain one atomic fact")
    return cleaned


def validate_memory_importance(importance: object) -> int:
    if isinstance(importance, bool) or not isinstance(importance, int):
        raise ValueError("memory importance must be an integer")
    if importance not in (1, 2, 3):
        raise ValueError("memory importance must be 1, 2, or 3")
    return importance


def normalize_evidence_message_ids(
    message_ids: Sequence[int] | None,
) -> tuple[int, ...]:
    if message_ids is None:
        return ()
    if isinstance(message_ids, (str, bytes)) or not isinstance(
        message_ids, Sequence,
    ):
        raise ValueError("evidence message IDs must be an array")
    normalized: list[int] = []
    seen: set[int] = set()
    for message_id in message_ids:
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or message_id <= 0
        ):
            raise ValueError("evidence message IDs must be positive integers")
        if message_id not in seen:
            seen.add(message_id)
            normalized.append(message_id)
    if len(normalized) > MAX_EVIDENCE_MESSAGE_IDS:
        raise ValueError(
            f"evidence message IDs exceed the {MAX_EVIDENCE_MESSAGE_IDS}-ID limit"
        )
    return tuple(normalized)


def encode_evidence_message_ids(
    message_ids: Sequence[int] | None,
) -> str:
    return json.dumps(
        normalize_evidence_message_ids(message_ids),
        separators=(",", ":"),
    )


def decode_evidence_message_ids(raw: str | None) -> tuple[int, ...]:
    if raw is None:
        return ()
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("stored evidence message IDs contain invalid JSON") from exc
    if not isinstance(parsed, list):
        raise ValueError("stored evidence message IDs must be a JSON array")
    return normalize_evidence_message_ids(parsed)


def merge_evidence_message_ids(
    *groups: Sequence[int],
) -> tuple[int, ...]:
    merged: list[int] = []
    seen: set[int] = set()
    for group in groups:
        for message_id in normalize_evidence_message_ids(group):
            if message_id not in seen:
                seen.add(message_id)
                merged.append(message_id)
    return tuple(merged[-MAX_EVIDENCE_MESSAGE_IDS:])
