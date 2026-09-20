"""Transport abstraction — base class for message transports.

A transport handles sending and receiving messages via this abstraction.
"""

import logging
import base64
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from mochi.main_runtime import MainRuntimeEntry

log = logging.getLogger(__name__)


class DeliveryError(RuntimeError):
    """A safe diagnostic with an explicit transport delivery outcome."""

    def __init__(
        self,
        reason: str,
        *,
        outcome: Literal[
            "delivery_unavailable", "delivery_rejected", "delivery_unknown", "expired",
        ],
    ):
        super().__init__(reason)
        self.outcome = outcome


def ensure_delivery_allowed(can_deliver: Callable[[], bool] | None) -> None:
    if can_deliver is not None and not can_deliver():
        raise DeliveryError("delivery window ended", outcome="expired")


@dataclass(frozen=True)
class ImageAttachment:
    """An in-memory image attached to the current incoming message."""
    data: bytes = field(repr=False)
    media_type: str = "image/jpeg"

    def data_url(self) -> str:
        encoded = base64.b64encode(self.data).decode("ascii")
        return f"data:{self.media_type};base64,{encoded}"


@dataclass
class IncomingMessage:
    """A message received from any transport."""
    user_id: int
    channel_id: int
    text: str
    transport: str  # "telegram"
    raw: dict | None = None  # transport-specific raw data
    owner_authorized: bool = False
    image: ImageAttachment | None = field(default=None, repr=False)
    runtime_entry: "MainRuntimeEntry | None" = field(default=None, repr=False)
    # Optional callback fired during tool execution (set by transport layer).
    # Signature: async def on_interim(text=None, *, tool_name=None) -> None
    on_interim: Callable[..., Awaitable[None]] | None = field(
        default=None, repr=False,
    )


class Transport(ABC):
    """Abstract base class for message transports.

    Transports are "dumb pipes" — they handle message I/O only.
    Business logic lives in the AI client / skills layer.
    """

    @abstractmethod
    async def start(self) -> None:
        """Start the transport (connect, listen for messages)."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully stop the transport."""
        ...

    @abstractmethod
    async def send_message(self, user_id: int, text: str) -> bool:
        """Send a text message and report whether it was delivered."""
        ...

    async def send_chat_result(self, user_id: int, result) -> bool:
        """Deliver a ChatResult through this transport."""
        if not result.text:
            return False
        delivered = await self.send_message(user_id, result.text)
        if delivered:
            result.confirm_delivered()
        return delivered

    async def send_chat_result_checked(
        self, user_id: int, result, *, can_deliver: Callable[[], bool] | None = None,
    ) -> bool:
        """Preserve detailed failures where supported, otherwise report uncertainty."""
        ensure_delivery_allowed(can_deliver)
        if not await self.send_chat_result(user_id, result):
            raise DeliveryError(
                f"{self.name}: transport did not confirm delivery",
                outcome="delivery_unknown",
            )
        return True

    async def send_proactive_result_checked(
        self, user_id: int, result, *, can_deliver: Callable[[], bool] | None = None,
    ) -> bool:
        """Deliver a runtime-initiated result using transport-specific formatting."""
        return await self.send_chat_result_checked(
            user_id, result, can_deliver=can_deliver,
        )

    @property
    @abstractmethod
    def name(self) -> str:
        """Transport identifier (e.g., 'telegram')."""
        ...
