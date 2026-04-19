"""E2E: notes-driven think gating.

Scenario:
  Medicine 0/2 at 14:15. Run think twice with same conditions but
  different notes.md state.

  A. Baseline notes (no exception) → think SHOULD emit habit_nudge.
  B. Add note "今天在外面，要9点以后吃药" → think SHOULD see the note
     and NOT emit habit_nudge (or emit it with the exception acknowledged).

This proves:
  - Think reads notes.md
  - Think uses notes to gate event-type findings, not just ambience

Usage: PYTHONPATH=. python scripts/e2e_notes_gating.py
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
NOTES_PATH = MOCHITEST / "data" / "notes.md"
NOTES_BAK = MOCHITEST / "data" / "notes.md.bak_e2e"

# Use a neutral persona to focus on think's gating, not chat's演绎
NEUTRAL_PERSONA = """# 你是谁
你叫mochi，是小林的男朋友。

# 性格
- 温柔体贴
- 不强势

# 说话方式
- 自然亲切
"""

EXCEPTION_NOTE = "- 小林今天不在家没带药，明早再吃 (2026-04-20)"

# Minimal notes content for case A (baseline) — strip the existing
# "晚上10点不提醒" rule so think isn't pre-gated by quiet hours.
BASELINE_NOTES = """# Notes

## Notes
- 红糖（英短金渐层，5岁，甲亢）需要监测饮水量 (2026-04-15)
"""


def telegram_send(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": OWNER_ID, "text": text}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10):
            return True
    except Exception as e:
        print(f"  TG send failed: {e}")
        return False


def reset_state():
    from mochi.config import logical_today
    today = logical_today()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("DELETE FROM habit_logs WHERE date(logged_at) = ?", (today,))
    conn.execute("DELETE FROM proactive_log WHERE date(created_at) = ?", (today,))
    conn.commit()
    conn.close()


def ensure_medicine_only() -> int:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id FROM habits WHERE user_id = ? AND name = 'Medicine'",
        (OWNER_ID,),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE habits SET active = 1, frequency = 'daily:2' WHERE id = ?",
            (row["id"],),
        )
        hid = row["id"]
    else:
        cursor = conn.execute(
            "INSERT INTO habits (user_id, name, frequency, category, active, created_at) "
            "VALUES (?, 'Medicine', 'daily:2', 'health', 1, ?)",
            (OWNER_ID, datetime.now().isoformat()),
        )
        hid = cursor.lastrowid
    conn.execute(
        "UPDATE habits SET active = 0 WHERE user_id = ? AND id != ?",
        (OWNER_ID, hid),
    )
    conn.commit()
    conn.close()
    return hid


def write_baseline_notes():
    NOTES_PATH.write_text(BASELINE_NOTES, encoding="utf-8")


def add_note_line(line: str):
    """Append note line into ## Notes section (baseline notes are short
    enough that 300-char compact won't truncate)."""
    text = NOTES_PATH.read_text(encoding="utf-8")
    NOTES_PATH.write_text(text.rstrip() + "\n" + line + "\n", encoding="utf-8")


def write_soul(text: str):
    SOUL_PATH.write_text(text, encoding="utf-8")
    from mochi.prompt_loader import reload_all
    reload_all()


async def run_case(case_id: str, description: str, mutate_notes=None):
    reset_state()
    ensure_medicine_only()
    write_soul(NEUTRAL_PERSONA)
    write_baseline_notes()

    if mutate_notes:
        mutate_notes()

    import mochi.heartbeat as hb

    captured = {"msg": None}

    async def fake_callback(uid, text):
        captured["msg"] = text

    hb.set_send_callback(fake_callback)
    hb._state = "AWAKE"
    hb._last_proactive_at = None
    hb._proactive_count_today = 0

    from mochi.diary import refresh_diary_status
    refresh_diary_status()

    observation = await hb._observe(OWNER_ID)
    notes_in_obs = (observation.get("notes") or "")[:500]
    result = await hb._think(observation, OWNER_ID)
    if result is None:
        telegram_send(f"[{case_id}] ❌ Think None")
        return

    findings = result.get("findings", [])
    thought = result.get("thought", "")[:300]
    await hb._act(result, OWNER_ID)

    lines = [f"━━━ {case_id} ━━━", description]
    lines.append(f"📒 notes (think 看到的，前 300 字):")
    lines.append(notes_in_obs if notes_in_obs else "(empty)")
    lines.append("")
    lines.append(f"💭 think: {thought}")
    if findings:
        for f in findings:
            pa = f.get("prior_attempts", 0)
            tag = f" [prior={pa}]" if pa else ""
            lines.append(f"🔍 [{f.get('topic')}]{tag} {f.get('summary', '')[:180]}")
    else:
        lines.append("🔍 findings: [] (think_silent — 没催)")

    if captured["msg"]:
        lines.append(f"💬 chat: {captured['msg']}")
    elif findings:
        lines.append("💬 chat: (SKIP)")

    report = "\n".join(lines)
    try:
        print(report + "\n")
    except UnicodeEncodeError:
        print(report.encode("ascii", "replace").decode("ascii") + "\n")
    telegram_send(report)


async def main():
    print("=" * 60)
    print("E2E: notes-driven think gating")
    print("=" * 60)

    from mochi.db import init_db
    init_db()
    from mochi.skills import discover
    discover()

    shutil.copy(DB_PATH, DB_BAK)
    shutil.copy(NOTES_PATH, NOTES_BAK)
    telegram_send("🧪 notes-gating E2E starting (2 cases)")

    try:
        # Case A: baseline notes — should nag
        await run_case("A", "Baseline notes — Medicine 0/2 at 14:15 应该催药")
        await asyncio.sleep(2)

        # Case B: add exception note — should NOT nag
        def add_exception():
            add_note_line(EXCEPTION_NOTE)
        await run_case(
            "B",
            f"加 note：「{EXCEPTION_NOTE.lstrip('- ')}」— think 应该看到并放过",
            mutate_notes=add_exception,
        )

        telegram_send("✅ notes-gating E2E complete")
    finally:
        shutil.copy(DB_BAK, DB_PATH)
        shutil.copy(NOTES_BAK, NOTES_PATH)
        backup_soul = MOCHITEST / "data" / "prompts" / "system_chat" / "soul.md.e2e_backup"
        if backup_soul.exists():
            shutil.copy(backup_soul, SOUL_PATH)
            from mochi.prompt_loader import reload_all
            reload_all()
        print("DB, notes, and soul restored.")


if __name__ == "__main__":
    asyncio.run(main())
