"""Fake transport for E2E tests — collects sent messages in memory."""

from mochi.transport import Transport


class FakeTransport(Transport):
    """In-memory transport that records all sent messages."""

    def __init__(self):
        self.sent_messages: list[tuple[int, str]] = []

    @property
    def name(self) -> str:
        return "fake"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send_message(self, user_id: int, text: str) -> None:
        self.sent_messages.append((user_id, text))
