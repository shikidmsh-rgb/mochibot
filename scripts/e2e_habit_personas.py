"""E2E heartbeat test: Habit matrix × 2 personas + silent verification.

Runs against mochitest DB + LLM. Sends results to telegram so user can
visually verify persona演绎 quality.

Usage: PYTHONPATH=. python scripts/e2e_habit_personas.py
"""
import asyncio
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# Force mochitest paths
MOCHITEST = Path("M:/mochitest")
os.chdir(MOCHITEST)
sys.path.insert(0, str(MOCHITEST))

# Bring in env
from dotenv import load_dotenv
load_dotenv(MOCHITEST / ".env")

import urllib.request
import urllib.parse
import json

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
- 不轻易夸人，夸人也是淡淡的

# 说话方式
- 短句，命令式
- 偶尔带"给我..."、"敢..."的口吻
- 不撒娇不柔软
""",
    "温柔": """# 你是谁
你叫mochi，温柔体贴的陪伴者。

# 性格
- 关心、柔软
- 不催不压，留余地
- 商量的语气
- 容易夸人

# 说话方式
- 用语气词如"哦"、"呢"、"~"
- 多用问句而不是命令
- 喜欢用昵称
""",
}


def telegram_send(text: str):
    """Send a plain message via telegram bot API."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": OWNER_ID,
        "text": text,
    }).encode()
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"  TG send failed: {e}")
        return False


def backup_db():
    shutil.copy(DB_PATH, DB_BAK)


def restore_db():
    shutil.copy(DB_BAK, DB_PATH)


def reset_habits_today():
    """Wipe today's habit_logs so we can inject fresh state."""
    today = datetime.now().date().isoformat()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("DELETE FROM habit_logs WHERE date(logged_at) = ?", (today,))
    conn.commit()
    conn.close()


def ensure_habit(name: str, frequency: str) -> int:
    """Get-or-create habit, return id.

    `frequency` should encode count, e.g. 'daily:2' for 2x/day.
    """
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
        conn.commit()
        hid = row["id"]
    else:
        cursor = conn.execute(
            "INSERT INTO habits (user_id, name, frequency, category, "
            "active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (OWNER_ID, name, frequency, "health", datetime.now().isoformat()),
        )
        hid = cursor.lastrowid
        conn.commit()
    conn.close()
    return hid


def add_checkin(habit_id: int, period: str, count: int = 1):
    """Add `count` check-ins for today."""
    now = datetime.now().isoformat()
    conn = sqlite3.connect(str(DB_PATH))
    for _ in range(count):
        conn.execute(
            "INSERT INTO habit_logs (habit_id, user_id, note, logged_at, period) "
            "VALUES (?, ?, ?, ?, ?)",
            (habit_id, OWNER_ID, "", now, period),
        )
    conn.commit()
    conn.close()


def disable_all_other_habits(keep: list[int]):
    keep_str = ",".join(str(i) for i in keep)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        f"UPDATE habits SET active = 0 WHERE user_id = ? AND id NOT IN ({keep_str})",
        (OWNER_ID,),
    )
    conn.commit()
    conn.close()


def reactivate_all_habits():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE habits SET active = 1 WHERE user_id = ?", (OWNER_ID,))
    conn.commit()
    conn.close()


def write_soul(persona_text: str):
    SOUL_PATH.write_text(persona_text, encoding="utf-8")
    # Force prompt loader cache invalidation
    from mochi.prompt_loader import reload_all
    reload_all()


def force_silence_lookback():
    """Push the last user message back 4 hours so think sees long silence."""
    conn = sqlite3.connect(str(DB_PATH))
    backdated = (datetime.now() - timedelta(hours=4)).isoformat()
    conn.execute(
        "UPDATE messages SET created_at = ? WHERE id = (SELECT MAX(id) FROM messages WHERE role = 'user')",
        (backdated,),
    )
    conn.commit()
    conn.close()


async def run_case(case_id: str, description: str, persona: str, setup_fn,
                   fake_hour: int = 14):
    """Setup state, run think+act, capture proactive output.

    fake_hour: pretend it's this hour (LOCAL) to bypass nighttime rules.
    """
    write_soul(PERSONAS[persona])
    reset_habits_today()
    setup_fn()
    force_silence_lookback()

    # Re-import heartbeat each round to ensure fresh state would be ideal
    # but we can just use module-level. Force diary refresh too.
    import mochi.heartbeat as hb
    from mochi.diary import diary

    # Monkey-patch datetime in heartbeat + observation builders so think
    # sees fake_hour (avoid 22:00 quiet-hour rules in user notes).
    real_datetime = hb.datetime

    class FakeDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            real = real_datetime.now(tz)
            return real.replace(hour=fake_hour, minute=15, second=0, microsecond=0)

    import mochi.observers.time_context.observer as time_obs
    import mochi.ai_client as ai_mod
    hb.datetime = FakeDatetime
    time_obs.datetime = FakeDatetime
    ai_mod.datetime = FakeDatetime

    # Capture chat_proactive output instead of sending via _send_callback
    captured = {"msg": None}

    async def fake_callback(uid, text):
        captured["msg"] = text

    hb.set_send_callback(fake_callback)
    # Force AWAKE
    hb._state = "AWAKE"
    hb._last_proactive_at = None  # bypass cooldown
    hb._proactive_count_today = 0  # bypass daily limit

    # Refresh diary status (so think sees current habit state)
    from mochi.diary import refresh_diary_status
    refresh_diary_status()

    try:
        # Observe → think → act
        observation = await hb._observe(OWNER_ID)
        result = await hb._think(observation, OWNER_ID)
    finally:
        # Restore datetime so we don't pollute subsequent cases
        hb.datetime = real_datetime
        time_obs.datetime = real_datetime

    if result is None:
        out = f"[{case_id}/{persona}] {description}\n  ❌ Think returned None"
        print(out)
        telegram_send(out)
        return

    findings = result.get("findings", [])
    thought = result.get("thought", "")[:120]

    await hb._act(result, OWNER_ID)

    # Build report
    report_lines = [f"━━━ {case_id} / {persona} ━━━", description]
    report_lines.append(f"💭 think: {thought}")
    if findings:
        f_str = ", ".join(f.get("topic", "?") for f in findings)
        report_lines.append(f"🔍 findings: [{f_str}]")
        for f in findings:
            report_lines.append(f"  - [{f.get('topic')}] {f.get('summary', '')[:140]}")
    else:
        report_lines.append("🔍 findings: [] (think_silent)")

    if captured["msg"]:
        report_lines.append(f"💬 chat: {captured['msg']}")
    elif findings:
        report_lines.append("💬 chat: (SKIP or no message)")

    report = "\n".join(report_lines)
    print(report)
    print()
    telegram_send(report)


def setup_a1():
    """Medicine 0/2 at 14:00."""
    hid = ensure_habit("Medicine", "daily:2")
    disable_all_other_habits([hid])
    # 0 checkins


def setup_a2():
    """Medicine 1/2 at evening."""
    hid = ensure_habit("Medicine", "daily:2")
    disable_all_other_habits([hid])
    add_checkin(hid, datetime.now().date().isoformat(), 1)


def setup_a3():
    """Medicine 2/2 fully done."""
    hid = ensure_habit("Medicine", "daily:2")
    disable_all_other_habits([hid])
    add_checkin(hid, datetime.now().date().isoformat(), 2)


def setup_a4():
    """Exercise done + Medicine 1/2."""
    h_med = ensure_habit("Medicine", "daily:2")
    h_ex = ensure_habit("Exercise", "daily")
    disable_all_other_habits([h_med, h_ex])
    add_checkin(h_med, datetime.now().date().isoformat(), 1)
    add_checkin(h_ex, datetime.now().date().isoformat(), 1)


def setup_a5():
    """Multiple habits, 0 done."""
    h_med = ensure_habit("Medicine", "daily:2")
    h_ex = ensure_habit("Exercise", "daily")
    h_water = ensure_habit("Water", "daily:8")
    disable_all_other_habits([h_med, h_ex, h_water])


def setup_a6():
    """All done."""
    h_med = ensure_habit("Medicine", "daily:2")
    h_ex = ensure_habit("Exercise", "daily")
    disable_all_other_habits([h_med, h_ex])
    add_checkin(h_med, datetime.now().date().isoformat(), 2)
    add_checkin(h_ex, datetime.now().date().isoformat(), 1)


def setup_f1():
    """Same as a6 — verify silent (no findings)."""
    setup_a6()


CASES = [
    ("A1", "Medicine 0/2 — overdue, mid-day", setup_a1),
    ("A2", "Medicine 1/2 — half done, evening", setup_a2),
    ("A3", "Medicine 2/2 — all done", setup_a3),
    ("A4", "Exercise done + Medicine 1/2", setup_a4),
    ("A5", "0/many — late evening, big miss", setup_a5),
    ("A6", "All habits done", setup_a6),
]


async def main():
    print("=" * 60)
    print("E2E: Habit matrix × 2 personas + silent")
    print("=" * 60)

    # Ensure schema is current (admin server may not have been started)
    from mochi.db import init_db
    init_db()
    # Discover skills so diary_status integration works
    from mochi.skills import discover
    discover()

    backup_db()
    print(f"DB backed up to {DB_BAK}")
    telegram_send("🧪 E2E heartbeat refactor test starting (13 cases)")

    try:
        for persona in ["霸总", "温柔"]:
            for case_id, desc, setup_fn in CASES:
                try:
                    await run_case(case_id, desc, persona, setup_fn)
                except Exception as e:
                    err = f"[{case_id}/{persona}] EXCEPTION: {e}"
                    print(err)
                    telegram_send(err)
                # Small delay so telegram messages stay in order and we don't burst
                await asyncio.sleep(2)

        # F1: silent verification (uses last persona, but topic is generic)
        try:
            await run_case("F1", "All done, recently active — should be silent", "温柔", setup_f1)
        except Exception as e:
            telegram_send(f"[F1] EXCEPTION: {e}")

        telegram_send("✅ E2E suite complete. Restoring DB...")
    finally:
        restore_db()
        # Restore original soul
        backup_soul = MOCHITEST / "data" / "prompts" / "system_chat" / "soul.md.e2e_backup"
        if backup_soul.exists():
            shutil.copy(backup_soul, SOUL_PATH)
            from mochi.prompt_loader import reload_all
            reload_all()
        reactivate_all_habits()
        print("DB and soul restored.")


if __name__ == "__main__":
    asyncio.run(main())
