"""Sticker skill — send Telegram stickers from the learned registry."""

import asyncio
import logging
import random

from mochi.skills.base import Skill, SkillContext, SkillResult

log = logging.getLogger(__name__)


async def generate_sticker_tags(emoji: str, set_name: str, caption: str) -> str:
    """Generate semantic tags for a sticker using LITE tier.

    Returns comma-separated tags (Chinese), e.g. "开心,撒娇,晚安".
    Falls back to emoji string on failure.
    """
    try:
        from mochi.llm import get_client_for_tier

        prompt = (
            "Generate 3-5 short semantic tags (in Chinese) for a Telegram sticker.\n"
            f"Emoji: {emoji}\n"
            f"Sticker set: {set_name}\n"
            f"User caption: {caption}\n\n"
            "Return ONLY comma-separated tags, no explanation. "
            "Tags should describe mood/emotion/situation, e.g.: 开心,撒娇,晚安,加油"
        )

        client = get_client_for_tier("lite")
        response = await asyncio.to_thread(
            client.chat,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
        )

        tags = (response.content or "").strip()
        tags = tags.strip("\"'[]()").strip()
        return tags if tags else (emoji or "sticker")

    except Exception as e:
        log.warning("Failed to generate sticker tags: %s", e)
        return emoji or "sticker"


def _get_all_stickers(user_id: int) -> list[dict]:
    """Get all stickers for a user (used as fallback)."""
    from mochi.db import _connect
    conn = _connect()
    rows = conn.execute(
        "SELECT id, user_id, file_id, set_name, emoji, tags, created_at "
        "FROM sticker_registry WHERE user_id = ? OR user_id = 0",
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# In-memory record of last sent sticker per chat (for delete support)
_last_sent_sticker: dict[int, str] = {}


def record_last_sent_sticker(chat_id: int, file_id: str) -> None:
    """Record the last sticker sent in a chat (for delete support)."""
    _last_sent_sticker[chat_id] = file_id


def get_last_sent_sticker(chat_id: int) -> str | None:
    """Get the file_id of the last sticker sent in a chat."""
    return _last_sent_sticker.get(chat_id)


class StickerSkill(Skill):

    async def learn_sticker(self, user_id: int, file_id: str,
                            set_name: str, emoji: str,
                            caption: str = "") -> dict:
        """Learn a new sticker. Returns {learned, tags, count}."""
        from mochi.db import save_sticker, get_sticker_count

        tags = await generate_sticker_tags(emoji, set_name, caption)
        row_id = save_sticker(
            user_id=user_id, file_id=file_id,
            set_name=set_name, emoji=emoji, tags=tags,
        )
        count = get_sticker_count(user_id)
        if row_id:
            log.info("Sticker learned: %.20s... tags=%s count=%d", file_id, tags, count)
        return {"learned": row_id is not None, "tags": tags, "count": count}

    async def execute(self, context: SkillContext) -> SkillResult:
        if context.tool_name == "delete_last_sticker":
            return self._delete_last(context.channel_id)

        if context.tool_name != "send_sticker":
            return SkillResult(output=f"Unknown sticker tool: {context.tool_name}", success=False)

        mood = context.args.get("mood", "").strip().lower()
        if not mood:
            return SkillResult(output="Please specify a mood or tag for the sticker.", success=False)

        from mochi.db import get_stickers_by_tag, get_sticker_count

        total = get_sticker_count(context.user_id)
        log.info("send_sticker: mood=%s, user_id=%s, total=%d", mood, context.user_id, total)
        if total == 0:
            return SkillResult(
                output="No stickers learned yet. Forward some stickers to me and I'll learn them!"
            )

        # Try exact tag match first
        matches = get_stickers_by_tag(mood, context.user_id)

        # Fallback: try matching individual characters of mood against tags
        if not matches and len(mood) >= 2:
            all_stickers = _get_all_stickers(context.user_id)
            for s in all_stickers:
                tags = s.get("tags", "")
                if any(ch in tags for ch in mood if ch.strip()):
                    matches.append(s)

        # Final fallback: pick random from entire library
        if not matches:
            matches = _get_all_stickers(context.user_id)
            log.info("send_sticker: no tag match, using random fallback")

        if not matches:
            return SkillResult(output="No stickers available.")

        chosen = random.choice(matches)
        file_id = chosen["file_id"]

        log.info("Sticker matched: mood=%s, file_id=%.20s..., tags=%s",
                 mood, file_id, chosen.get("tags", ""))

        return SkillResult(
            output=f"[STICKER:{file_id}] Sticker queued. You MUST also write a text reply to accompany it."
        )

    @staticmethod
    def _delete_last(chat_id: int) -> SkillResult:
        from mochi.db import delete_sticker

        file_id = get_last_sent_sticker(chat_id)
        if not file_id:
            return SkillResult(output="没有找到最近发过的表情包，无法删除。")

        deleted = delete_sticker(file_id)
        if deleted:
            log.info("Deleted sticker: %.20s...", file_id)
            return SkillResult(output="已删除该表情包，以后不会再发了！")
        return SkillResult(output="该表情包已经不在库中了。")
