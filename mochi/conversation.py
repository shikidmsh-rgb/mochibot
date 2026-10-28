"""One owner conversation, with generation-scoped execution and publication."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field

from mochi import conversation_store as store, db
from mochi.transport import DeliveryError, FileAttachment, ImageAttachment

log = logging.getLogger(__name__)


class Superseded(asyncio.CancelledError):
    """New input or a reset superseded this generation, not its completed actions."""


@dataclass
class Adapter:
    respond: Callable[[dict], Awaitable[None]]
    media: Callable[[dict], Awaitable[tuple[ImageAttachment | None, FileAttachment | None]]]


@dataclass
class Owner:
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    worker: asyncio.Task | None = None
    active: "Generation | None" = None
    activity_token: object | None = None


_owners: dict[int, Owner] = {}
_adapters: dict[str, Adapter] = {}
_current: ContextVar["Generation | None"] = ContextVar("owner_conversation", default=None)
_closing = False


def current() -> "Generation | None":
    return _current.get()


def checkpoint() -> None:
    run = current()
    if run:
        run.check()


def is_current() -> bool:
    try:
        checkpoint()
    except Superseded:
        return False
    return True


async def wait_read(operation):
    run = current()
    return await run.wait_read(operation) if run else await operation


async def deliver(
    kind: str, content: str, operation: Callable[[], Awaitable],
    *, rejected_errors: tuple[type[Exception], ...] = (),
):
    run = current()
    if run is None:
        return await operation()
    run.check()
    delivery_id = store.start_delivery(run.record["id"], kind, content)
    try:
        result = await operation()
    except BaseException as exc:
        status = (
            "failed" if isinstance(exc, rejected_errors) or (
                isinstance(exc, DeliveryError)
                and exc.outcome in {"delivery_unavailable", "delivery_rejected", "expired"}
            )
            else "unknown"
        )
        store.finish_delivery(delivery_id, status)
        run.delivery_unknown |= status == "unknown"
        raise
    store.finish_delivery(delivery_id, "sent")
    return result


@dataclass
class Generation:
    record: dict
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    result: object = None
    delivered: bool = False
    delivery_unknown: bool = False
    context_images: list[ImageAttachment] = field(default_factory=list)
    context_files: dict[int, FileAttachment] = field(default_factory=dict)
    prior_turn_ids: frozenset[str] = frozenset()
    memory_exposure_ids: frozenset[int] = frozenset()

    def check(self) -> None:
        if self.stopped.is_set() or (
            db.get_context_reset(self.record["user_id"]) != self.record["reset_at"]
        ):
            raise Superseded()

    async def wait_read(self, operation):
        try:
            self.check()
        except Superseded:
            if isinstance(operation, asyncio.Future):
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            elif hasattr(operation, "close"):
                operation.close()
            raise
        task = asyncio.ensure_future(operation)
        stop = asyncio.create_task(self.stopped.wait())
        try:
            await asyncio.wait((task, stop), return_when=asyncio.FIRST_COMPLETED)
            self.check()
            return await task
        finally:
            stop.cancel()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, stop, return_exceptions=True)

    def confirm(self, result, *, final: bool) -> bool:
        self.check()
        self.result = result
        if final:
            self.delivered = True
        return True

    async def prepare_context(self) -> dict:
        data = store.continuation(self.record)
        self.prior_turn_ids = frozenset(data.pop("_turn_ids", ()))
        self.memory_exposure_ids = frozenset(data.pop("_memory_ids", ()))
        self.context_images.clear()
        self.context_files.clear()
        for item in data.get("inputs", []):
            item.pop("id")
            encrypted = item.pop("payload", None)
            if not encrypted:
                continue
            adapter = _adapters.get(item["transport"])
            if adapter is None:
                item["media_available"] = False
                continue
            try:
                image, attachment = await self.wait_read(adapter.media(
                    store.payload({"payload": encrypted}),
                ))
            except Exception as exc:
                log.warning(
                    "Prior conversation media unavailable: message=%s (%s)",
                    item["message_id"], type(exc).__name__,
                )
                item["media_available"] = False
                continue
            if image:
                item["image_index"] = len(self.context_images)
                self.context_images.append(image)
            if attachment:
                self.context_files[item["message_id"]] = attachment
        return data


def invalidate(user_id: int) -> None:
    owner = _owners.get(user_id)
    if owner:
        if owner.active:
            owner.active.stopped.set()
        owner.wake.set()


def receive(*, user_id: int, transport: str, source_id: str, text: str, payload: dict) -> bool:
    if not source_id:
        raise ValueError("A conversation input requires a stable source ID")
    accepted = store.accept(user_id, transport, source_id, text, payload)
    if not accepted:
        return False
    owner = _owners.setdefault(user_id, Owner())
    _hold_activity(owner)
    if owner.active:
        owner.active.stopped.set()
    owner.wake.set()
    _ensure_worker(user_id, owner)
    return True


def register(transport: str, adapter: Adapter) -> None:
    _adapters[transport] = adapter
    with store._connection() as conn:
        users = [row[0] for row in conn.execute(
            "SELECT DISTINCT user_id FROM conversation_inputs "
            "WHERE run_id IS NULL AND payload IS NOT NULL",
        )]
    for user_id in users:
        owner = _owners.setdefault(user_id, Owner())
        _hold_activity(owner)
        owner.wake.set()
        _ensure_worker(user_id, owner)


def _ensure_worker(user_id: int, owner: Owner) -> None:
    if not _closing and (owner.worker is None or owner.worker.done()):
        owner.worker = asyncio.create_task(_work(user_id, owner), name=f"owner-chat:{user_id}")
        owner.worker.add_done_callback(lambda task: _worker_finished(owner, task))


def _hold_activity(owner: Owner) -> None:
    if owner.activity_token is None:
        from mochi.heartbeat import begin_active_chat
        owner.activity_token = begin_active_chat()


def _release_activity(owner: Owner) -> None:
    if owner.activity_token is not None:
        from mochi.heartbeat import end_active_chat
        end_active_chat(owner.activity_token)
        owner.activity_token = None


def _worker_finished(owner: Owner, task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("Owner conversation worker failed", exc_info=task.exception())
        if owner.worker is task:
            owner.active = None
            _release_activity(owner)


async def _work(user_id: int, owner: Owner) -> None:
    from mochi.heartbeat import active_chat
    from mochi.ai_client import _schedule_continuous_memory
    from mochi.runtime_trace import finish_turn

    while not _closing:
        await owner.wake.wait()
        owner.wake.clear()
        pending = store.pending(user_id)
        if _closing or not pending or pending[-1]["transport"] not in _adapters:
            _release_activity(owner)
            continue
        record = store.claim(user_id)
        if record is None:
            _release_activity(owner)
            continue
        run = Generation(record)
        owner.active = run
        token = _current.set(run)
        status = "failed"
        try:
            with active_chat():
                run.check()
                newest = record["inputs"][-1]
                await _adapters[newest["transport"]].respond(store.payload(newest))
                run.check()
                status = (
                    "completed" if run.delivered or getattr(run.result, "disposition", "") in {"skip", "handled"}
                    else "failed"
                )
        except Superseded:
            status = "superseded"
        except asyncio.CancelledError:
            status = "interrupted"
            raise
        except Exception:
            log.exception("Owner conversation failed: %s", record["id"])
        finally:
            if run.delivery_unknown and status not in {"superseded", "completed"}:
                status = "unknown"
            store.finish(record, status, run.result)
            finish_turn(record["id"], status)
            run.context_images.clear()
            run.context_files.clear()
            _current.reset(token)
            owner.active = None
            if status == "completed":
                _schedule_continuous_memory(user_id)
            if store.pending(user_id):
                owner.wake.set()
            else:
                _release_activity(owner)


def initialize() -> None:
    global _closing
    _closing = False
    store.recover()


async def close() -> None:
    global _closing
    _closing = True
    for owner in _owners.values():
        if owner.active:
            owner.active.stopped.set()
        owner.wake.set()
    tasks = [owner.worker for owner in _owners.values() if owner.worker]
    if tasks:
        await asyncio.gather(*tasks)
    for owner in _owners.values():
        _release_activity(owner)
    _owners.clear()
    _adapters.clear()
