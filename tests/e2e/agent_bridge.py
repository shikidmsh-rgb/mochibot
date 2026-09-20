"""Opt-in, local evaluation adapter for a real agent driving Main.

Run ``python -m tests.e2e.agent_bridge --task "..." --output <new-directory>``.
Read the printed URL (also in bridge.json), GET /round, and POST /round with
{"round_id": 1, "content": "", "tool_calls": [
    {"id": "unique-call-id", "name": "advertised-tool", "arguments": {}}]}.
Poll again after each accepted response. A waiting response with round_id=null
means Main is starting or executing tools. GET /result returns the evidence.
There are no scripted replies, additional model instructions, or task assertions.
``done`` means Main returned, not that the assignment passed an evaluation.
Tool execution evidence comes from the durable ledger, not the audit fields
reserved for autonomous runtime entries in ChatResult.

Only a fresh output-contained runtime is used; .env is never loaded. The shared
admin_db storage helpers may be imported by Main, but no Admin application,
transport, scheduler, or model service is started. Built-in tools are restricted
to development and skill listing/toggling; dynamically registered personal tools
remain available through the real registry. Development is available by default.

This is NOT a sandbox: authored Python runs as the local user, including live
handlers. Only trusted local agents should use this unauthenticated loopback
endpoint. Output contains complete authored code and model-visible messages.
The bridge supplies no credentials; explicit host file/network access by authored
code is not contained. Stop the printed PID when finished. Active evaluation
has per-response and whole-session deadlines; results remain served until the
whole-session deadline. Use a dedicated process, not an existing Mochi runtime.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
import os
from pathlib import Path
import queue
import sqlite3
import sys
import threading
import time
from unittest.mock import patch


_MAX_BODY = 2 * 1024 * 1024
_CLEANUP_SECONDS = 10


def _write_json(path: Path, value: object) -> None:
    sibling = path.with_suffix(".writing")
    with sibling.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    sibling.replace(path)


def _positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive finite number")
    return seconds


def _reject_constant(value: str):
    raise ValueError("non-finite JSON numbers are not accepted")


class Bridge:
    """Own the immutable request snapshots and one reply slot per round."""

    def __init__(self, output: Path, task: str, response_timeout: float,
                 session_timeout: float):
        self.output = output
        self.task = task
        self.response_timeout = response_timeout
        self.deadline = time.monotonic() + session_timeout
        self.lock = threading.RLock()
        self.aborted = threading.Event()
        self.finished = threading.Event()
        self.status = "waiting"
        self.error = None
        self.pending = None
        self.replies: queue.Queue = queue.Queue(maxsize=1)
        self.rounds: list[dict] = []
        self.interim: list[dict] = []
        self.result = None
        self.evidence: dict = {}
        self.loop = None
        self.chat_task = None
        self.paths = {
            "database": str(output / "runtime" / "mochi.db"),
            "core": str(output / "runtime" / "core_data"),
            "mochi_files": str(output / "runtime" / "files_data"),
            "extensions": str(output / "runtime" / "extensions"),
            "transcript": str(output / "transcript.json"),
            "result": str(output / "result.json"),
        }

    def _summary(self) -> dict:
        return {
            "status": self.status,
            "round_id": self.pending["round_id"] if self.pending else None,
            "error": self.error,
            "result": self.result,
            "round_count": len(self.rounds),
            "paths": self.paths,
            **self.evidence,
        }

    def _persist(self) -> None:
        # All writers hold the same lock, including the HTTP and runtime threads.
        _write_json(self.output / "transcript.json", {
            "task": self.task, "rounds": self.rounds, "interim": self.interim,
        })
        _write_json(self.output / "result.json", self._summary())

    def round_snapshot(self) -> dict:
        with self.lock:
            if self.status == "waiting" and self.pending is not None:
                return deepcopy({"status": "waiting", **self.pending})
            return deepcopy(self._summary())

    def result_snapshot(self) -> dict:
        with self.lock:
            return deepcopy({
                **self._summary(),
                "transcript": {
                    "task": self.task, "rounds": self.rounds,
                    "interim": self.interim,
                },
            })

    def abort(self, reason: str) -> None:
        with self.lock:
            if self.status == "done":
                return
            self.status = "failed"
            self.error = self.error or reason
            self.pending = None
            self.aborted.set()
            self._persist()
            if self.loop is not None and self.chat_task is not None:
                try:
                    self.loop.call_soon_threadsafe(self.chat_task.cancel)
                except RuntimeError:
                    pass  # The runtime loop has already closed.

    def request(self, messages, tools, temperature, max_tokens, json_mode):
        with self.lock:
            if self.aborted.is_set() or time.monotonic() >= self.deadline:
                raise TimeoutError("Evaluation session is no longer active")
            if self.pending is not None:
                raise RuntimeError("Only one Main provider request may be pending")
            wait_until = min(
                self.deadline, time.monotonic() + self.response_timeout,
            )
            record = deepcopy({
                "round_id": len(self.rounds) + 1,
                "messages": messages,
                "tools": tools,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "json_mode": json_mode,
                "requested_at": datetime.now(timezone.utc).isoformat(),
            })
            self.rounds.append(record)
            self.pending = record
            self._persist()
        while not self.aborted.is_set():
            remaining = wait_until - time.monotonic()
            if remaining <= 0:
                self.abort("Timed out waiting for an agent provider response")
                break
            try:
                response = self.replies.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                continue
            if self.aborted.is_set():
                break
            return response
        raise TimeoutError("Evaluation aborted; no provider response was invented")

    def submit(self, payload: object) -> int:
        if not isinstance(payload, dict) or set(payload) != {
            "round_id", "content", "tool_calls",
        }:
            raise ValueError("Expected exactly round_id, content, and tool_calls")
        round_id = payload["round_id"]
        if type(round_id) is not int or round_id < 1:
            raise ValueError("round_id must be a positive integer")
        if not isinstance(payload["content"], str):
            raise ValueError("content must be a string")
        calls = payload["tool_calls"]
        if not isinstance(calls, list):
            raise ValueError("tool_calls must be an array")
        ids = set()
        for call in calls:
            if not isinstance(call, dict) or set(call) != {"id", "name", "arguments"}:
                raise ValueError("Each tool call requires exactly id, name, arguments")
            if any(not isinstance(call[key], str) or not call[key].strip()
                   for key in ("id", "name")):
                raise ValueError("Tool call id and name must be nonempty strings")
            if not isinstance(call["arguments"], dict):
                raise ValueError("Tool arguments must be a JSON object")
            if call["id"] in ids:
                raise ValueError("Tool call ids must be unique within the response")
            ids.add(call["id"])
        with self.lock:
            if (self.status != "waiting" or self.pending is None
                    or self.pending["round_id"] != round_id):
                return 409
            if time.monotonic() >= self.deadline:
                self.abort("Whole-session deadline exceeded")
                return 409
            from mochi.llm import LLMResponse

            response = LLMResponse(
                content=payload["content"],
                tool_calls=[{**deepcopy(call), "argument_error": None} for call in calls],
                model="external-agent-bridge",
                finish_reason="tool_calls" if calls else "stop",
                tool_calls_complete=True,
            )
            self.pending["response"] = deepcopy(payload)
            self.pending["responded_at"] = datetime.now(timezone.utc).isoformat()
            self.pending = None
            self._persist()
            self.replies.put_nowait(response)
            return 200


class InteractiveProvider:
    """Synchronous provider adapter; HTTP only transfers JSON, never executes it."""

    def __init__(self, bridge: Bridge):
        self.bridge = bridge

    def chat(self, messages, tools=None, temperature=None, max_tokens=2048,
             json_mode=False):
        return self.bridge.request(messages, tools, temperature, max_tokens, json_mode)

    def provider_name(self):
        return "external-agent-bridge"


class _DiscoveryDirectories:
    """Restrict discovery before importing any unrelated skill handlers."""

    def __init__(self, root: Path):
        self.root = root

    def iterdir(self):
        return iter(self.root / name for name in (
            "development", "skill_management", "habit",
        ))


def _prepare_runtime(stack: ExitStack, bridge: Bridge):
    if any(name == "mochi" or name.startswith("mochi.") for name in sys.modules):
        raise RuntimeError("Run the bridge in a fresh process, before importing mochi")
    runtime = bridge.output / "runtime"
    for name in ("core_data", "files_data", "extensions", "prompts", "scratch"):
        (runtime / name).mkdir(parents=True, exist_ok=True)

    # Clear ambient settings before config imports or model-pool construction.
    clean_env = {
        key: os.environ[key] for key in ("SYSTEMROOT", "SystemRoot", "WINDIR")
        if key in os.environ
    }
    clean_env.update({
        "EMBEDDING_PROVIDER": "none",
        "OWNER_USER_ID": "1",
        "TOOL_ROUTER_ENABLED": "false",
        "TOOL_ESCALATION_ENABLED": "true",
        "MEMORY_AUTO_RECALL": "false",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "TMP": str(runtime / "scratch"),
        "TEMP": str(runtime / "scratch"),
        "TMPDIR": str(runtime / "scratch"),
    })
    stack.enter_context(patch.dict(os.environ, clean_env, clear=True))
    stack.enter_context(patch("dotenv.load_dotenv", return_value=False))
    import mochi.config as config

    stack.enter_context(patch.object(config, "DB_PATH", runtime / "mochi.db"))
    stack.enter_context(patch.object(config, "_PROJECT_ROOT", runtime))
    stack.enter_context(patch.object(config, "TZ", timezone.utc))
    stack.enter_context(patch.object(config, "TIMEZONE_OFFSET_HOURS", 0))
    import mochi.db as db
    import mochi.core_store as core_store
    import mochi.mochi_files_store as files_store
    import mochi.prompt_loader as prompts
    import mochi.diary as diary_module
    from mochi.extensions import store

    # Patch both the source config and import-time aliases before init/discovery.
    stack.enter_context(patch.object(db, "DB_PATH", runtime / "mochi.db"))
    stack.enter_context(patch.object(core_store, "DATA_DIR", runtime / "core_data"))
    stack.enter_context(patch.object(files_store, "DATA_DIR", runtime / "files_data"))
    stack.enter_context(patch.object(prompts, "_DATA_PROMPTS_DIR", runtime / "prompts"))
    stack.enter_context(patch.object(diary_module, "_DATA_DIR", runtime))
    stack.enter_context(patch.object(diary_module.diary, "path", runtime / "diary.md"))
    stack.enter_context(patch.object(store, "ROOT", runtime / "extensions"))
    db.init_db()

    import mochi.skills as registry
    with patch.object(registry, "_SKILLS_DIR", _DiscoveryDirectories(registry._SKILLS_DIR)):
        registry.discover()
    registry.init_all_skill_schemas()
    # Main always queries habits. Keep its empty schema, not its unrelated tools.
    registry._skills.pop("habit", None)
    for tool, owner in list(registry._tool_map.items()):
        if owner == "habit":
            registry._tool_map.pop(tool)
    registry._prompt_hooks.pop("habit", None)
    registry.refresh_capability_summary()
    if "development" in db.get_disabled_skills():
        raise RuntimeError("Fresh development state must be enabled by default")

    import mochi.tool_policy as policy
    ordinary_filter = policy.filter_tools

    def evaluation_tools(definitions):
        def allowed(definition):
            name = definition.get("function", {}).get("name")
            if name in {"request_tools", "list_skills", "toggle_skill"}:
                return True
            skill = registry.get_skill(registry.skill_for_tool(name))
            return bool(skill and (skill.name == "development" or skill.external))
        return ordinary_filter([item for item in definitions if allowed(item)])

    stack.enter_context(patch.object(policy, "filter_tools", evaluation_tools))
    import mochi.heartbeat as heartbeat
    stack.enter_context(patch.object(heartbeat, "bedtime_tool_available", return_value=False))
    import mochi.ai_client as ai_client
    provider = InteractiveProvider(bridge)
    stack.enter_context(patch.object(
        ai_client, "get_client_for_tier", lambda tier="main": provider,
    ))
    # Delivery persistence is real; only ancillary post-delivery Lite jobs are off.
    stack.enter_context(patch.object(ai_client, "_schedule_continuous_memory"))
    return ai_client, config


def _collect_evidence(bridge: Bridge) -> dict:
    database = Path(bridge.paths["database"])
    evidence = {"messages": [], "tool_ledger": [], "artifacts": []}
    if database.is_file():
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            connection.row_factory = sqlite3.Row
            for table, key in (("messages", "messages"), ("tool_executions", "tool_ledger")):
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
                ).fetchone()
                if exists:
                    evidence[key] = [
                        dict(row) for row in connection.execute(
                            f"SELECT * FROM {table} WHERE user_id=1 ORDER BY rowid",
                        )
                    ]
        finally:
            connection.close()
    for root, directories, files in os.walk(bridge.output / "runtime", followlinks=False):
        directories[:] = [
            name for name in directories
            if not Path(root, name).is_symlink()
            and not getattr(Path(root, name), "is_junction", lambda: False)()
        ]
        evidence["artifacts"].extend(
            str(Path(root, name).relative_to(bridge.output)) for name in files
        )
    evidence["tool_summary"] = {
        "attempts": len(evidence["tool_ledger"]),
        "succeeded": sum(item["status"] == "success" for item in evidence["tool_ledger"]),
        "failed": sum(item["status"] == "failed" for item in evidence["tool_ledger"]),
        "state_changes": sum(bool(item["state_changed"]) for item in evidence["tool_ledger"]),
    }
    return evidence


async def _run_chat(bridge: Bridge, ai_client) -> None:
    from mochi.transport import IncomingMessage

    async def interim(text=None, *, tool_name=None):
        with bridge.lock:
            bridge.interim.append({"text": text, "tool_name": tool_name})
            bridge._persist()

    with bridge.lock:
        bridge.loop = asyncio.get_running_loop()
        bridge.chat_task = asyncio.current_task()
    result = await asyncio.wait_for(
        ai_client.chat(IncomingMessage(
            user_id=1, channel_id=1, text=bridge.task,
            transport="agent_bridge", owner_authorized=True, on_interim=interim,
        )),
        timeout=max(0.001, bridge.deadline - time.monotonic()),
    )
    if bridge.aborted.is_set():
        return
    result.confirm_delivered()
    with bridge.lock:
        bridge.result = {
            "text": result.text,
            "stickers": result.stickers,
            "disposition": result.disposition,
            "delivery_confirmed": result._delivery_confirmed,
        }


def _runtime_worker(bridge: Bridge) -> None:
    try:
        with ExitStack() as stack:
            ai_client, config = _prepare_runtime(stack, bridge)
            with bridge.lock:
                bridge.evidence["budgets"] = {
                    "provider_rounds": config.TOOL_LOOP_MAX_ROUNDS,
                    "ordinary_tools": config.TOOL_LOOP_TOTAL_TOOL_LIMIT,
                    "per_tool_name": config.TOOL_LOOP_PER_TOOL_LIMIT,
                    "request_tools": config.TOOL_ESCALATION_MAX_PER_TURN,
                }
                bridge._persist()
            asyncio.run(_run_chat(bridge, ai_client))
    except (Exception, asyncio.CancelledError) as exc:
        # Diagnostics deliberately omit exception strings/tracebacks: extension
        # exceptions may include arbitrary host data. Actual tool results remain
        # unmodified in the transcript.
        bridge.abort(f"Runtime stopped ({type(exc).__name__})")
    finally:
        try:
            evidence = _collect_evidence(bridge)
            with bridge.lock:
                bridge.evidence.update(evidence)
                if not bridge.aborted.is_set():
                    bridge.status = "done"
                bridge._persist()
        except Exception as exc:
            bridge.abort(f"Evidence collection failed ({type(exc).__name__})")
        finally:
            bridge.finished.set()


def _handler(bridge: Bridge):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentEvaluationBridge"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_args):
            pass

        def _reply(self, status, payload):
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)

        def _local_request(self):
            port = self.server.server_port
            if (self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}
                    or self.headers.get("Origin") is not None):
                self._reply(403, {"error": "Local non-browser requests only"})
                return False
            return True

        def do_GET(self):
            if not self._local_request():
                return
            if self.path == "/round":
                self._reply(200, bridge.round_snapshot())
            elif self.path == "/result":
                self._reply(200, bridge.result_snapshot())
            else:
                self._reply(404, {"error": "Unknown endpoint"})

        def do_POST(self):
            if not self._local_request():
                return
            if self.path != "/round":
                self._reply(404, {"error": "Unknown endpoint"})
                return
            if self.headers.get_content_type() != "application/json":
                self._reply(415, {"error": "Use application/json"})
                return
            try:
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("Chunked bodies are not supported")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= _MAX_BODY:
                    raise ValueError("JSON body must be between 1 byte and 2 MiB")
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("Incomplete request body")
                payload = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
                status = bridge.submit(payload)
            except (ValueError, UnicodeError, RecursionError, TimeoutError):
                self._reply(400, {"error": "Invalid response JSON or response shape"})
                return
            self._reply(status, {
                "accepted": status == 200,
                "round_id": payload["round_id"],
            })

    return Handler


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--task", required=True, help="Owner's assignment, passed unchanged to Main")
    parser.add_argument("--output", required=True, type=Path, help="New, empty evidence directory")
    parser.add_argument("--port", type=int, default=0, help="Loopback port; 0 selects an unused port")
    parser.add_argument("--response-timeout", type=_positive_seconds, default=600,
                        help="Maximum seconds to wait for each agent response (default: 600)")
    parser.add_argument("--session-timeout", type=_positive_seconds, default=3600,
                        help="Maximum session/server lifetime in seconds (default: 3600)")
    args = parser.parse_args(argv)
    if not args.task.strip():
        parser.error("--task must not be blank")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    output = args.output.absolute()
    if any(path.is_symlink() or getattr(path, "is_junction", lambda: False)()
           for path in (output, *output.parents)):
        parser.error("--output may not traverse filesystem links")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        parser.error("--output must be a new or empty directory")
    output.mkdir(parents=True, exist_ok=True)
    output = output.resolve()
    sys.dont_write_bytecode = True
    logging.disable(logging.CRITICAL)
    bridge = Bridge(output, args.task, args.response_timeout, args.session_timeout)
    with ThreadingHTTPServer(("127.0.0.1", args.port), _handler(bridge)) as server:
        server.daemon_threads = True
        server.timeout = 0.25
        connection = {
            "pid": os.getpid(),
            "url": f"http://127.0.0.1:{server.server_port}",
            "output": str(output),
            "response_timeout_seconds": args.response_timeout,
            "session_timeout_seconds": args.session_timeout,
        }
        _write_json(output / "bridge.json", connection)
        with bridge.lock:
            bridge._persist()
        worker = threading.Thread(target=_runtime_worker, args=(bridge,), daemon=True)
        worker.start()
        print(json.dumps(connection), flush=True)
        try:
            while time.monotonic() < bridge.deadline:
                server.handle_request()
                if bridge.aborted.is_set():
                    break
        except KeyboardInterrupt:
            bridge.abort("Evaluation interrupted")
        if not bridge.finished.is_set():
            bridge.abort("Whole-session deadline exceeded")
            worker.join(_CLEANUP_SECONDS)
            if worker.is_alive():
                # A live handler can block the event loop or a Python executor.
                # Do not let those non-daemon threads defeat the outer deadline.
                os._exit(1)
    return 1 if bridge.aborted.is_set() else 0


if __name__ == "__main__":
    raise SystemExit(main())
