"""Owner input and delivery facts; tool outcomes remain in the execution ledger."""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import datetime

from mochi import db
from mochi.config import TZ


@contextmanager
def _connection():
    conn = db._connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_schema(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversation_runs (
            id TEXT PRIMARY KEY, root_id TEXT NOT NULL, user_id INTEGER NOT NULL,
            reset_at TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL,
            finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_conversation_runs_owner
            ON conversation_runs(user_id, root_id);
        CREATE TABLE IF NOT EXISTS conversation_inputs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL, transport TEXT NOT NULL, content TEXT NOT NULL,
            payload TEXT, received_at TEXT NOT NULL, reset_at TEXT,
            run_id TEXT, message_id INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_conversation_inputs_pending
            ON conversation_inputs(user_id, run_id, id);
        CREATE TABLE IF NOT EXISTS conversation_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            kind TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT
        );
    """)


def accept(user_id: int, transport: str, source_id: str, content: str, payload: dict) -> bool:
    from mochi.admin.admin_crypto import encrypt_api_key, is_encrypted

    encrypted = encrypt_api_key(json.dumps(payload, ensure_ascii=False))
    if not is_encrypted(encrypted):
        raise RuntimeError("Conversation input encryption is unavailable")
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        epoch = db._current_context_reset(conn, user_id)
        received_at = datetime.now(TZ).isoformat()
        cursor = conn.execute(
            "INSERT INTO conversation_inputs "
            "(source_id,user_id,transport,content,payload,received_at,reset_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(source_id) DO NOTHING",
            (source_id, user_id, transport, content, encrypted, received_at, epoch),
        )
        if cursor.rowcount != 1:
            return False
        input_id = cursor.lastrowid
        message = conn.execute(
            "INSERT INTO messages(user_id,role,content,created_at,turn_id,processed) "
            "VALUES (?,'user',?,?,?,0)",
            (user_id, content, received_at, f"input:{uuid.uuid4().hex}"),
        )
        conn.execute(
            "UPDATE conversation_inputs SET message_id=? WHERE id=?",
            (message.lastrowid, input_id),
        )
        return True


def pending(user_id: int) -> list[dict]:
    with _connection() as conn:
        epoch = db._current_context_reset(conn, user_id)
        return [dict(row) for row in conn.execute(
            "SELECT id,transport FROM conversation_inputs "
            "WHERE user_id=? AND run_id IS NULL AND payload IS NOT NULL AND reset_at IS ? "
            "ORDER BY id", (user_id, epoch),
        )]


def claim(user_id: int) -> dict | None:
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        epoch = db._current_context_reset(conn, user_id)
        inputs = [dict(row) for row in conn.execute(
            "SELECT * FROM conversation_inputs WHERE user_id=? AND run_id IS NULL "
            "AND payload IS NOT NULL AND reset_at IS ? ORDER BY id", (user_id, epoch),
        )]
        if not inputs:
            return None
        previous = conn.execute(
            "SELECT root_id,status FROM conversation_runs WHERE user_id=? AND reset_at IS ? "
            "ORDER BY rowid DESC LIMIT 1", (user_id, epoch),
        ).fetchone()
        run_id = uuid.uuid4().hex
        root_id = (
            previous["root_id"] if previous and previous["status"] != "completed" else run_id
        )
        conn.execute(
            "INSERT INTO conversation_runs(id,root_id,user_id,reset_at,status,created_at) "
            "VALUES (?,?,?,?, 'running',?)",
            (run_id, root_id, user_id, epoch, datetime.now(TZ).isoformat()),
        )
        for item in inputs:
            conn.execute(
                "UPDATE messages SET turn_id=? WHERE id=? AND user_id=?",
                (run_id, item["message_id"], user_id),
            )
            conn.execute(
                "UPDATE conversation_inputs SET run_id=?,message_id=? WHERE id=?",
                (run_id, item["message_id"], item["id"]),
            )
        return {
            "id": run_id, "root_id": root_id, "user_id": user_id, "reset_at": epoch,
            "inputs": inputs,
        }


def payload(item: dict) -> dict:
    from mochi.admin.admin_crypto import decrypt_api_key

    decoded = decrypt_api_key(item["payload"])
    if not decoded:
        raise RuntimeError("Conversation input cannot be decrypted")
    return json.loads(decoded)


def prepare_input(run: dict, content: str) -> None:
    item = run["inputs"][-1]
    with _connection() as conn:
        conn.execute(
            "UPDATE conversation_inputs SET content=? WHERE id=?", (content, item["id"]),
        )
        conn.execute(
            "UPDATE messages SET content=? WHERE id=? AND user_id=?",
            (content, item["message_id"], run["user_id"]),
        )


def start_delivery(run_id: str, kind: str, content: str) -> int:
    with _connection() as conn:
        cursor = conn.execute(
            "INSERT INTO conversation_deliveries(run_id,kind,content,status,started_at) "
            "VALUES (?,?,?,'sending',?)",
            (run_id, kind, content, datetime.now(TZ).isoformat()),
        )
        return int(cursor.lastrowid)


def finish_delivery(delivery_id: int, status: str) -> None:
    with _connection() as conn:
        conn.execute(
            "UPDATE conversation_deliveries SET status=?,finished_at=? WHERE id=?",
            (status, datetime.now(TZ).isoformat(), delivery_id),
        )


def finish(run: dict, status: str, result=None) -> None:
    now = datetime.now(TZ).isoformat()
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT finished_at FROM conversation_runs WHERE id=?", (run["id"],),
        ).fetchone()
        if existing["finished_at"] is not None:
            return
        conn.execute(
            "UPDATE conversation_deliveries SET status='unknown',finished_at=? "
            "WHERE run_id=? AND status='sending'", (now, run["id"]),
        )
        conn.execute(
            "UPDATE tool_executions SET status='unknown' WHERE turn_id=? AND status='running'",
            (run["id"],),
        )
        sent = "\n\n".join(row["content"] for row in conn.execute(
            "SELECT content FROM conversation_deliveries "
            "WHERE run_id=? AND kind='text' AND status='sent' ORDER BY id", (run["id"],),
        ))
        epoch_matches = db._current_context_reset(conn, run["user_id"]) == run["reset_at"]
        if not epoch_matches:
            status = "cleared"
        confirmed_media = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM conversation_deliveries "
            "WHERE run_id=? AND kind<>'text' AND status='sent')", (run["id"],),
        ).fetchone()[0]
        if epoch_matches and (sent or confirmed_media):
            pending_history = getattr(result, "_pending_history", None) or {}
            conn.execute(
                "INSERT INTO messages(user_id,role,content,created_at,turn_id,processed,"
                "reasoning_content,reasoning_source,tool_history) "
                "VALUES (?,'assistant',?,?,?,0,?,?,?)",
                (
                    run["user_id"], sent, now, run["id"],
                    pending_history.get("reasoning_content") if status == "completed" else None,
                    pending_history.get("reasoning_source", "") if status == "completed" else "",
                    pending_history.get("tool_history"),
                ),
            )
        conn.execute(
            "UPDATE conversation_runs SET status=?,finished_at=? WHERE id=?",
            (status, now, run["id"]),
        )
        if status in {"completed", "cleared"}:
            conn.execute(
                "UPDATE conversation_inputs SET payload=NULL WHERE run_id IN "
                "(SELECT id FROM conversation_runs WHERE root_id=? AND user_id=?)",
                (run["root_id"], run["user_id"]),
            )


def continuation(run: dict) -> dict:
    with _connection() as conn:
        if db._current_context_reset(conn, run["user_id"]) != run["reset_at"]:
            return {}
        inputs = [dict(row) for row in conn.execute(
            "SELECT i.id,i.transport,i.content,i.received_at,i.payload,i.message_id "
            "FROM conversation_inputs i JOIN conversation_runs r ON r.id=i.run_id "
            "WHERE r.root_id=? AND r.user_id=? AND i.id<>? ORDER BY i.id",
            (run["root_id"], run["user_id"], run["inputs"][-1]["id"]),
        )]
        deliveries = [dict(row) for row in conn.execute(
            "SELECT d.kind,d.content,d.status,d.started_at,d.finished_at FROM conversation_deliveries d "
            "JOIN conversation_runs r ON r.id=d.run_id "
            "WHERE r.root_id=? AND r.user_id=? AND r.id<>? ORDER BY d.id",
            (run["root_id"], run["user_id"], run["id"]),
        )]
        for item in deliveries:
            if item["status"] == "failed":
                item.pop("content", None)
        operations = []
        memory_ids: set[int] = set()
        for row in conn.execute(
            "SELECT t.id,t.tool_name,t.status,t.result_json,t.result_summary,t.started_at,t.finished_at "
            "FROM tool_executions t JOIN conversation_runs r ON r.id=t.turn_id "
            "WHERE r.root_id=? AND r.user_id=? AND t.user_id=? ORDER BY t.id",
            (run["root_id"], run["user_id"], run["user_id"]),
        ):
            item = {
                "tool": row["tool_name"], "status": row["status"],
                "recorded_at": row["finished_at"] or row["started_at"],
            }
            raw = row["result_json"]
            if raw:
                item["receipt_id"] = row["id"]
                snapshot = json.loads(raw)
                snapshot.pop("history_foldable", None)
                memory_ids.update(snapshot.pop("exposed_memory_ids", ()))
                item["result"] = snapshot
            elif row["result_summary"]:
                item["summary"] = row["result_summary"]
            operations.append(item)
        turn_ids = [row["id"] for row in conn.execute(
            "SELECT id FROM conversation_runs WHERE root_id=? AND user_id=?",
            (run["root_id"], run["user_id"]),
        )]
        return {
            "inputs": inputs, "deliveries": deliveries, "operations": operations,
            "_turn_ids": turn_ids, "_memory_ids": sorted(memory_ids),
        }


def recover() -> None:
    with _connection() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM conversation_runs WHERE finished_at IS NULL",
        )]
    for run in rows:
        with _connection() as conn:
            had_effects = conn.execute(
                "SELECT EXISTS(SELECT 1 FROM tool_executions WHERE turn_id=?) OR "
                "EXISTS(SELECT 1 FROM conversation_deliveries WHERE run_id=?)",
                (run["id"], run["id"]),
            ).fetchone()[0]
            conn.execute(
                "UPDATE tool_executions SET status='unknown' WHERE turn_id=? AND status='running'",
                (run["id"],),
            )
            if not had_effects:
                conn.execute(
                    "UPDATE conversation_inputs SET run_id=NULL WHERE run_id=?", (run["id"],),
                )
        finish(run, "interrupted")
