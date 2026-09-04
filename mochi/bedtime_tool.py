"""Framework tool for Main-requested bedtime transitions."""

ENTER_BEDTIME_TOOL_NAME = "enter_bedtime"

ENTER_BEDTIME_DEF = {
    "type": "function",
    "function": {
        "name": ENTER_BEDTIME_TOOL_NAME,
        "description": (
            "深夜聊天自然收尾时，你可以在当前告别送达后也进入休息；"
            "适合用户明确说自己要睡了、不再继续聊的时刻。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}
