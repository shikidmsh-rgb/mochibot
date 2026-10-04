"""Dream material progress; source records remain in their original stores."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta
from uuid import uuid4

from mochi import config
from mochi.db import _connect

THRESHOLD = 8
MAX_IDLE_DAYS = 7
MEMORY_LIMIT = 40
DIARY_CHARS = 12_000


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


def memory_version(row) -> str:
    return digest([row["content"], sorted(json.loads(row["evidence_message_ids"]))])


def record_memory_versions(conn, user_id: int, item_ids) -> None:
    for item_id in item_ids:
        row = conn.execute(
            "SELECT content, evidence_message_ids FROM memory_items "
            "WHERE user_id = ? AND id = ?", (user_id, item_id),
        ).fetchone()
        if row:
            conn.execute(
                "INSERT OR REPLACE INTO dream_memory_versions VALUES (?, ?, ?)",
                (user_id, item_id, memory_version(row)),
            )


def _state(conn, user_id: int, now: datetime) -> dict:
    row = conn.execute("SELECT * FROM dream_state WHERE user_id = ?", (user_id,)).fetchone()
    if row:
        return dict(row)
    day = config.logical_today(now)
    start = date.fromisoformat(day) - timedelta(days=7)
    boundary = datetime.combine(
        start, time(config._effective_maintenance_hour()), tzinfo=config.TZ,
    ).isoformat()
    older = conn.execute(
        "SELECT id FROM memory_items WHERE user_id = ? "
        "AND julianday(updated_at) < julianday(?)", (user_id, boundary),
    ).fetchall()
    record_memory_versions(conn, user_id, [row["id"] for row in older])
    conn.execute(
        "INSERT INTO dream_state(user_id, initial_date, backlog) VALUES (?, ?, 1)",
        (user_id, start.isoformat()),
    )
    return dict(conn.execute(
        "SELECT * FROM dream_state WHERE user_id = ?", (user_id,),
    ).fetchone())


def _pending_memory(conn, user_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT m.*, v.version AS seen_version FROM memory_items m "
        "LEFT JOIN dream_memory_versions v ON v.user_id = m.user_id AND v.item_id = m.id "
        "WHERE m.user_id = ? ORDER BY julianday(m.updated_at), m.id", (user_id,),
    ).fetchall()
    result = []
    for row in rows:
        version = memory_version(row)
        if row["seen_version"] != version:
            result.append({**dict(row), "version": version})
    return result


def _pending_diary(conn, user_id: int, start: str, day: str) -> list[dict]:
    from mochi.diary import diary

    progress = {
        row["date"]: dict(row) for row in conn.execute(
            "SELECT * FROM dream_diary_progress WHERE user_id = ?", (user_id,),
        )
    }
    result = []
    current = date.fromisoformat(start)
    end = date.fromisoformat(day)
    months: dict[str, dict[str, str]] = {}
    with diary._lock:
        while current < end:
            date_string = current.isoformat()
            month = date_string[:7]
            if month not in months:
                path = diary.path.parent / "diary_archive" / f"{month}.md"
                months[month] = dict(diary._archive_blocks(
                    path.read_text(encoding="utf-8") if path.exists() else "",
                ))
            block = months[month].get(date_string)
            if block:
                body = diary._section_content(block, "今日日記")
                if body.strip():
                    version = digest(body)
                    previous = progress.get(date_string, {})
                    offset = previous.get("offset", 0) if previous.get("version") == version else 0
                    if offset < len(body):
                        result.append({
                            "date": date_string, "version": version, "offset": offset,
                            "total_chars": len(body), "content": body,
                        })
            current += timedelta(days=1)
    return result


def _pressure(conn, user_id: int, now: datetime) -> tuple[dict, list, list]:
    state = _state(conn, user_id, now)
    day = config.logical_today(now)
    memory = _pending_memory(conn, user_id)
    diary = _pending_diary(conn, user_id, state["initial_date"], day)
    pending = conn.execute(
        "SELECT id FROM dream_batches WHERE user_id = ? AND status = 'pending' "
        "ORDER BY created_at LIMIT 1", (user_id,),
    ).fetchone()
    total = len(memory) + len(diary)
    waiting = state["waiting_since"]
    if total and not waiting:
        waiting = day
    elif not total and not pending:
        waiting = None
        conn.execute("UPDATE dream_state SET backlog = 0 WHERE user_id = ?", (user_id,))
    conn.execute(
        "UPDATE dream_state SET waiting_since = ? WHERE user_id = ?", (waiting, user_id),
    )
    anchor = state["last_success_day"] or waiting or day
    age = (date.fromisoformat(day) - date.fromisoformat(anchor)).days
    reason = (
        "pending_retry" if pending else
        "backlog" if total and state["backlog"] else
        "evidence_pressure" if total >= THRESHOLD else
        "max_age" if total and age >= MAX_IDLE_DAYS else
        "below_threshold" if total else "no_evidence"
    )
    return {
        "eligible": reason not in {"below_threshold", "no_evidence"},
        "reason": reason, "memory": len(memory), "diary": len(diary),
        "total": total, "last_success_day": state["last_success_day"],
        "last_attempt_day": state["last_attempt_day"],
        "pending_batch_id": pending["id"] if pending else None,
    }, memory, diary


def inspect_pressure(user_id: int, now: datetime | None = None) -> dict:
    now = now or datetime.now(config.TZ)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        pressure, _, _ = _pressure(conn, user_id, now)
        conn.execute(
            "UPDATE dream_state SET status_json = ? WHERE user_id = ?",
            (encode(pressure), user_id),
        )
        conn.commit()
        return pressure
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def prepare_batch(user_id: int, now: datetime | None = None) -> str | None:
    now = now or datetime.now(config.TZ)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        pressure, memory, diary = _pressure(conn, user_id, now)
        if pressure["pending_batch_id"]:
            conn.commit()
            return pressure["pending_batch_id"]
        if not pressure["eligible"]:
            conn.commit()
            return None
        remaining = DIARY_CHARS
        fragments = []
        for item in diary:
            if not remaining:
                break
            end = min(item["total_chars"], item["offset"] + remaining)
            fragments.append({
                **item, "next_offset": end,
                "content": item["content"][item["offset"]:end],
            })
            remaining -= end - item["offset"]
        batch_id = f"dream-{uuid4().hex}"
        material = {
            "memory": [
                {"id": row["id"], "version": row["version"]} for row in memory[:MEMORY_LIMIT]
            ],
            "diary": fragments,
            "memory_total": len(memory),
            "diary_days": [item["date"] for item in diary],
            "diary_total_chars": sum(item["total_chars"] - item["offset"] for item in diary),
        }
        from mochi.db import get_recent_user_messages_in_window
        material["evidence_messages"] = get_recent_user_messages_in_window(
            user_id, now - timedelta(days=7), now,
        )
        conn.execute(
            "INSERT INTO dream_batches(id, user_id, material_json, created_at) VALUES (?, ?, ?, ?)",
            (batch_id, user_id, encode(material), now.isoformat()),
        )
        conn.commit()
        return batch_id
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def load_batch(user_id: int, batch_id: str) -> dict:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM dream_batches WHERE id = ? AND user_id = ? AND status = 'pending'",
            (batch_id, user_id),
        ).fetchone()
        if row is None:
            raise ValueError("Dream batch is unavailable")
        return {**dict(row), "material": json.loads(row["material_json"])}
    finally:
        conn.close()


def claim_attempt(user_id: int, day: str) -> bool:
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE dream_state SET last_attempt_day = ? WHERE user_id = ? "
            "AND (last_attempt_day IS NULL OR last_attempt_day != ?)", (day, user_id, day),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def complete_batch(user_id: int, batch_id: str, now: datetime | None = None) -> None:
    now = now or datetime.now(config.TZ)
    batch = load_batch(user_id, batch_id)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for item in batch["material"]["memory"]:
            row = conn.execute(
                "SELECT * FROM memory_items WHERE user_id = ? AND id = ?", (user_id, item["id"]),
            ).fetchone()
            if row and memory_version(row) == item["version"]:
                record_memory_versions(conn, user_id, [item["id"]])
        for fragment in batch["material"]["diary"]:
            conn.execute(
                "INSERT OR REPLACE INTO dream_diary_progress VALUES (?, ?, ?, ?)",
                (user_id, fragment["date"], fragment["version"], fragment["next_offset"]),
            )
        conn.execute(
            "UPDATE dream_batches SET status = 'complete', completed_at = ? WHERE id = ?",
            (now.isoformat(), batch_id),
        )
        conn.execute(
            "UPDATE dream_state SET last_success_day = ?, waiting_since = NULL, "
            "backlog = ? WHERE user_id = ?",
            (
                config.logical_today(now),
                int(
                    batch["material"]["memory_total"] > len(batch["material"]["memory"])
                    or batch["material"]["diary_total_chars"]
                    > sum(len(item["content"]) for item in batch["material"]["diary"])
                ),
                user_id,
            ),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def read_operation(conn, user_id: int, batch_id: str, tool: str, key: str) -> dict | None:
    row = conn.execute(
        "SELECT result_json FROM dream_operations WHERE user_id = ? "
        "AND batch_id = ? AND tool = ? AND operation_key = ?",
        (user_id, batch_id, tool, key),
    ).fetchone()
    return json.loads(row["result_json"]) if row else None


def write_operation(conn, user_id: int, batch_id: str, tool: str, key: str, result: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO dream_operations VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, batch_id, tool, key, encode(result), datetime.now(config.TZ).isoformat()),
    )


def previous_operations(user_id: int, batch_id: str) -> list[dict]:
    conn = _connect()
    try:
        results = [
            {"tool": row["tool"], "status": json.loads(row["result_json"]).get("status"),
             "result": json.loads(row["result_json"]), "recorded_at": row["created_at"]}
            for row in conn.execute(
                "SELECT * FROM dream_operations WHERE user_id = ? AND batch_id = ? "
                "ORDER BY created_at", (user_id, batch_id),
            )
        ]
        results.extend(
            {"tool": row["tool_name"], "status": row["status"],
             "result": row["result_summary"], "recorded_at": row["finished_at"] or row["started_at"]}
            for row in conn.execute(
                "SELECT tool_name, status, result_summary, started_at, finished_at "
                "FROM tool_executions WHERE user_id = ? AND turn_id = ? AND source = 'dream' "
                "AND status != 'success' ORDER BY id", (user_id, f"dream:{user_id}:{batch_id}"),
            )
        )
        memory = conn.execute(
            "SELECT result_json, created_at FROM weekly_curation_batches "
            "WHERE user_id = ? AND period_key = ?", (user_id, batch_id),
        ).fetchone()
        if memory:
            results.append({
                "tool": "curate_dream_memory", "status": "committed",
                "result": json.loads(memory["result_json"]), "recorded_at": memory["created_at"],
            })
        return results
    finally:
        conn.close()


def status(user_id: int) -> dict:
    """Read existing maintenance facts without preparing work or invoking Main."""
    from mochi.admin.admin_db import get_system_config
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM dream_state WHERE user_id = ?", (user_id,)).fetchone()
        result = json.loads(row["status_json"]) if row else {"reason": "not_checked"}
        result["enabled"] = bool(get_system_config("WEEKLY_MAINTENANCE_ENABLED"))
        result["last_run"] = None
        if row and row["last_attempt_day"]:
            last = conn.execute(
                "SELECT status, started_at, finished_at, error FROM scheduled_runs "
                "WHERE job_name = 'dream' AND period_key = ?", (row["last_attempt_day"],),
            ).fetchone()
            result["last_run"] = dict(last) if last else None
        return result
    finally:
        conn.close()
