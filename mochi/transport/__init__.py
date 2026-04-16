"""Transport abstraction — base class for message transports.

A transport handles sending and receiving messages via this abstraction.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class IncomingMessage:
    """A message received from any transport."""
    user_id: int
    channel_id: int
    text: str
    transport: str  # "telegram"
    raw: dict | None = None  # transport-specific raw data
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
    async def send_message(self, user_id: int, text: str) -> None:
        """Send a text message to a user."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Transport identifier (e.g., 'telegram')."""
        ...
