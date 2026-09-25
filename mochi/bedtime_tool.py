"""Framework tool for Main-requested bedtime transitions."""

ENTER_BEDTIME_TOOL_NAME = "enter_bedtime"

ENTER_BEDTIME_DEF = {
    "type": "function",
    "function": {
        "name": ENTER_BEDTIME_TOOL_NAME,
        "description": "本轮回复发送结束后进入休息，暂停 Free Time。",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}
