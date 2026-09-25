"""Framework tool for Main-requested bedtime transitions."""

import json

from mochi.prompt_loader import get_prompt


ENTER_BEDTIME_TOOL_NAME = "enter_bedtime"
EMPTY_DIARY = "今天的日记还是空的。"

ENTER_BEDTIME_DEF = {
    "type": "function",
    "function": {
        "name": ENTER_BEDTIME_TOOL_NAME,
        "description": "准备休息，回看今天的聊天和日记；本轮结束后进入休息，暂停 Free Time。",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}


def bedtime_context(conversation: dict, journal: str, tomorrow: str) -> str:
    """One review surface for an explicit or heartbeat-initiated bedtime."""
    situation = get_prompt("bedtime_entry")
    protocol = get_prompt("runtime_silence_protocol")
    if not situation or not protocol:
        raise RuntimeError("Bedtime prompts are missing")
    visible = {
        **conversation,
        "messages": [
            {key: row[key] for key in ("id", "role", "created_at", "content")}
            for row in conversation["messages"]
        ],
    }
    parts = [
        situation,
        "## 今天的聊天\n" + json.dumps(visible, ensure_ascii=False),
        "## 今天的日记\n" + (journal or EMPTY_DIARY),
    ]
    if tomorrow:
        parts.append("## 明日日记草稿\n" + tomorrow)
    parts.append(protocol)
    return "\n\n".join(parts)
