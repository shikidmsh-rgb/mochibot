"""Shared transport utilities — bubble splitting & marker cleaning.

Used by both Telegram and WeChat transports to avoid code duplication.
"""

import re

# ── Marker cleaning ─────────────────────────────────────────────────────────
# Side-channel markers embedded in LLM replies (sticker, image, etc.)

_IMAGE_FILE_RE = re.compile(r"\[IMAGE_FILE:[^\]]+\]")
_STICKER_RE = re.compile(r"\[STICKER:[^\]]+\]")
_SKIP_RE = re.compile(r"\[SKIP\]")


def clean_reply_markers(text: str) -> str:
    """Strip side-channel markers from LLM reply text.

    Removes [IMAGE_FILE:...], [STICKER:...], [SKIP] markers
    that are meant for transport-specific handling, not display.
    """
    text = _IMAGE_FILE_RE.sub("", text)
    text = _STICKER_RE.sub("", text)
    text = _SKIP_RE.sub("", text)
    return text.strip()


# ── Text splitting ──────────────────────────────────────────────────────────

def split_text(text: str, limit: int) -> list[str]:
    """Split text into chunks respecting a character limit."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks


def split_bubbles(text: str, max_bubbles: int = 4,
                  delimiter: str = "|||",
                  min_chars: int = 8) -> list[str]:
    """Split text into chat bubbles for a natural multi-message feel.

    Primary split: explicit delimiter (LLM-controlled).
    Fallback: double-newline split when no delimiter found.
    Merge short fragments into previous bubble.
    """
    # Try explicit delimiter first
    if delimiter and delimiter in text:
        parts = [p.strip() for p in text.split(delimiter) if p.strip()]
    else:
        # Fallback: double-newline split
        parts = [p.strip() for p in text.split("\n\n") if p.strip()]

    if len(parts) <= 1:
        return [text.strip()]

    # Merge short fragments into previous bubble
    bubbles: list[str] = [parts[0]]
    for part in parts[1:]:
        if len(part) < min_chars:
            bubbles[-1] += "\n\n" + part
        else:
            bubbles.append(part)

    return bubbles[:max_bubbles]
