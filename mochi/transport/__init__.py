"""Transport abstraction — base class for message transports.

A transport handles sending and receiving messages via this abstraction.
"""

import logging
import base64
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mochi.main_runtime import MainRuntimeEntry

log = logging.getLogger(__name__)


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

    @property
    @abstractmethod
    def name(self) -> str:
        """Transport identifier (e.g., 'telegram')."""
        ...
