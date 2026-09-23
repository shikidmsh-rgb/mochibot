"""Shared transport utilities — bubble splitting & marker cleaning.

Used by both Telegram and WeChat transports to avoid code duplication.
"""

import re

# ── Marker cleaning ─────────────────────────────────────────────────────────
# Side-channel markers embedded in LLM replies (sticker, image, etc.)

_IMAGE_FILE_RE = re.compile(r"\[IMAGE_FILE:[^\]]+\]")
_STICKER_RE = re.compile(r"\[STICKER:[^\]]+\]")


def clean_reply_markers(text: str) -> str:
    """Strip side-channel markers from LLM reply text.

    Removes image and sticker markers handled before transport delivery.
    Runtime silence is resolved before replies reach this layer.
    """
    text = _IMAGE_FILE_RE.sub("", text)
    text = _STICKER_RE.sub("", text)
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


def split_bubbles(text: str, max_bubbles: int = 8,
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

    limit = max(1, max_bubbles)
    if len(bubbles) > limit:
        bubbles = bubbles[:limit - 1] + ["\n\n".join(bubbles[limit - 1:])]
    return bubbles


def format_usage_summary(summary: dict) -> str:
    """Format the shared /cost response for chat transports."""
    blocks = []
    for title, period in (("📊 今日", summary["today"]), ("📊 本月", summary["month"])):
        lines = [f"{title} · 总计 {period['total']:,} tokens"]
        if not period["by_model"]:
            lines.append("  (无记录)")
        for model, data in sorted(period["by_model"].items()):
            lines.append(f"  {model}")
            line = f"    input {data['prompt']:,}  |  output {data['completion']:,}"
            if data.get("reasoning", 0) > 0:
                line += f"  (其中 reasoning {data['reasoning']:,})"
            lines.append(line)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
