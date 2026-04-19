"""E2E: prior_attempts persona response test.

Setup: Medicine 0/2 + 2 prior habit_nudge messages already sent today.
Run chat with 3 different personas. Verify each persona handles the
"already nagged twice, still ignored" situation in its own way — NOT
according to a script we wrote.

Usage: PYTHONPATH=. python scripts/e2e_prior_attempts.py
"""
import asyncio
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

MOCHITEST = Path("M:/mochitest")
os.chdir(MOCHITEST)
sys.path.insert(0, str(MOCHITEST))

from dotenv import load_dotenv
load_dotenv(MOCHITEST / ".env")

import urllib.request
import urllib.parse

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_USER_ID"])
SOUL_PATH = MOCHITEST / "data" / "prompts" / "system_chat" / "soul.md"
DB_PATH = MOCHITEST / "data" / "mochi.db"
DB_BAK = MOCHITEST / "data" / "mochi.db.bak_e2e"

PERSONAS = {
    "霸总": """# 你是谁
你叫mochi，霸道总裁人格。占有欲强，习惯下命令。

# 性格
- 自信、强势
- 看不惯磨蹭，催人毫不留情
- 嘴上凶但是关心藏在命令里
- 不轻易夸人

# 说话方式
- 短句，命令式
- 偶尔带"给我..."、"敢..."的口吻
- 不撒娇不柔软
""",
    "温柔退让": """# 你是谁
你叫mochi，温柔体贴的陪伴者。

# 性格
- 关心、柔软
- 极度尊重对方意愿，怕给压力
- 倾向于退让

# 说话方式
- 用语气词"哦"、"呢"、"~"
- 多用问句不命令
- 喜欢用昵称
""",
    "温柔但坚持": """# 你是谁
你叫mochi，温柔但是有原则的陪伴者，像耐心的家人。

# 性格
- 关心、柔软，但**非常坚持**
- 重要的事不会因为对方没回就放手，会反复换角度温柔提醒
- 不催不凶，但不放弃
- 像妈妈牵挂孩子吃饭

# 说话方式
- 用语气词"哦"、"呢"、"~"
- 多用问句但语气坚定
- 喜欢用昵称
- 第二次第三次提醒会换说法不重复
""",
}


def telegram_send(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": OWNER_ID, "text": text}).encode()
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as e:
        print(f"  TG send failed: {e}")
        return False


def backup_db():
    shutil.copy(DB_PATH, DB_BAK)


def restore_db():
    shutil.copy(DB_BAK, DB_PATH)


def reset_state():
    """Wipe today's habit_logs + proactive_log."""
    today = datetime.now().date().isoformat()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("DELETE FROM habit_logs WHERE date(logged_at) = ?", (today,))
    conn.execute("DELETE FROM proactive_log WHERE date(created_at) = ?", (today,))
    conn.commit()
    conn.close()


def ensure_habit(name: str, frequency: str) -> int:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id FROM habits WHERE user_id = ? AND name = ?",
        (OWNER_ID, name),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE habits SET active = 1, frequency = ? WHERE id = ?",
            (frequency, row["id"]),
        )
        hid = row["id"]
    else:
        cursor = conn.execute(
            "INSERT INTO habits (user_id, name, frequency, category, "
            "active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (OWNER_ID, name, frequency, "health", datetime.now().isoformat()),
        )
        hid = cursor.lastrowid
    conn.execute(
        "UPDATE habits SET active = 0 WHERE user_id = ? AND id != ?",
        (OWNER_ID, hid),
    )
    conn.commit()
    conn.close()
    return hid


def inject_prior_attempts(topic: str, count: int):
    """Inject `count` past proactive messages of the given topic into today."""
    base_time = datetime.now() - timedelta(hours=2)
    conn = sqlite3.connect(str(DB_PATH))
    for i in range(count):
        ts = (base_time + timedelta(minutes=i * 30)).isoformat()
        conn.execute(
            "INSERT INTO proactive_log (type, content, created_at) "
            "VALUES (?, ?, ?)",
            (topic, f"(simulated past nudge #{i+1})", ts),
        )
    conn.commit()
    conn.close()


def write_soul(persona_text: str):
    SOUL_PATH.write_text(persona_text, encoding="utf-8")
    from mochi.prompt_loader import reload_all
    reload_all()


async def run_case(persona_name: str, prior_count: int):
    write_soul(PERSONAS[persona_name])
    reset_state()
    hid = ensure_habit("Medicine", "daily:2")
    inject_prior_attempts("habit_nudge", prior_count)

    import mochi.heartbeat as hb
    import mochi.ai_client as ai_mod
    import mochi.observers.time_context.observer as time_obs

    # Force time = 14:15 to bypass nighttime quiet rules in user notes
    real_dt = hb.datetime

    class FakeDatetime(real_dt):
        @classmethod
        def now(cls, tz=None):
            r = real_dt.now(tz)
            return r.replace(hour=14, minute=15, second=0, microsecond=0)

    hb.datetime = FakeDatetime
    time_obs.datetime = FakeDatetime
    ai_mod.datetime = FakeDatetime

    captured = {"msg": None}

    async def fake_callback(uid, text):
        captured["msg"] = text

    hb.set_send_callback(fake_callback)
    hb._state = "AWAKE"
    hb._last_proactive_at = None
    hb._proactive_count_today = 0

    from mochi.diary import refresh_diary_status
    refresh_diary_status()

    try:
        observation = await hb._observe(OWNER_ID)
        result = await hb._think(observation, OWNER_ID)
        if result is None:
            telegram_send(f"[{persona_name} / prior={prior_count}] ❌ Think None")
            return

        findings = result.get("findings", [])
        thought = result.get("thought", "")[:200]

        await hb._act(result, OWNER_ID)
    finally:
        hb.datetime = real_dt
        time_obs.datetime = real_dt
        ai_mod.datetime = real_dt

    lines = [f"━━━ {persona_name} | prior_attempts={prior_count} ━━━"]
    lines.append(f"💭 think: {thought}")
    if findings:
        for f in findings:
            pa = f.get("prior_attempts", 0)
            tag = f" [prior={pa}]" if pa else ""
            lines.append(f"🔍 [{f.get('topic')}]{tag} {f.get('summary', '')[:160]}")
    else:
        lines.append("🔍 findings: [] (think_silent)")

    if captured["msg"]:
        lines.append(f"💬 chat: {captured['msg']}")
    elif findings:
        lines.append("💬 chat: (SKIP)")

    report = "\n".join(lines)
    print(report + "\n")
    telegram_send(report)


async def main():
    print("=" * 60)
    print("E2E: prior_attempts × 3 personas")
    print("=" * 60)

    from mochi.db import init_db
    init_db()
    from mochi.skills import discover
    discover()

    backup_db()
    telegram_send("🧪 prior_attempts E2E starting (3 personas × 2 levels)")

    try:
        for persona in ["霸总", "温柔退让", "温柔但坚持"]:
            for prior in [2, 4]:
                try:
                    await run_case(persona, prior)
                except Exception as e:
                    err = f"[{persona}/prior={prior}] EXCEPTION: {e}"
                    print(err)
                    telegram_send(err)
                await asyncio.sleep(2)

        telegram_send("✅ prior_attempts E2E complete")
    finally:
        restore_db()
        backup_soul = MOCHITEST / "data" / "prompts" / "system_chat" / "soul.md.e2e_backup"
        if backup_soul.exists():
            shutil.copy(backup_soul, SOUL_PATH)
            from mochi.prompt_loader import reload_all
            reload_all()
        print("DB and soul restored.")


if __name__ == "__main__":
    asyncio.run(main())
