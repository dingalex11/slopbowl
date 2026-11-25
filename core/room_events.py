import asyncio
from typing import Awaitable, Callable, Iterable

from core.events import ADMIN_EVENTS, MANUAL_START_EVENTS, PASSIVE_ADMIN_EVENTS, Event


class RoomEventPump:
    """One place for reading room events and dispatching the admin contract.

    The queue still belongs to the moderator. The pump owns the shared rule that
    admin events are processed (except passive heartbeats) while the caller continues
    waiting for the event it actually needs. The admin handler is injected so read,
    manual-reader, and question waits all use the exact same dispatcher.
    """

    def __init__(self, inbound_queue: asyncio.Queue, process_admin_event: Callable[[Event], Awaitable[None]]):
        self._inbound_queue = inbound_queue
        self._process_admin_event = process_admin_event

    @staticmethod
    def _event_type(event: Event) -> str:
        return event.type.name if hasattr(event.type, "name") else str(event.type)

    async def dispatch_admin(self, event: Event) -> bool:
        """Process an admin event and report whether the caller should move on."""
        evt_type = self._event_type(event)
        if evt_type not in ADMIN_EVENTS:
            return False
        if evt_type not in PASSIVE_ADMIN_EVENTS:
            await self._process_admin_event(event)
        return True

    async def wait_for_action(self, expected_event_types: Iterable[str] | str):
        """Wait for one of the caller's actions, serving admin events on the way.

        The expected action is checked before the admin set because manual starts are
        both actions here and admin events elsewhere. FORCE_RESET_STATE and CHALLENGE
        remain the two interrupts that end the wait with an abort action.
        """
        if isinstance(expected_event_types, str):
            expected_event_types = [expected_event_types]
        expected = frozenset(expected_event_types)

        while True:
            event = await self._inbound_queue.get()
            evt_type = self._event_type(event)
            if evt_type in expected:
                return event
            if evt_type in MANUAL_START_EVENTS:
                # A duplicate start is not the action this wait is for. The old wait
                # ignored it; keep that behavior instead of emitting an in-progress
                # status out of sequence. Expected starts returned above.
                continue
            if await self.dispatch_admin(event):
                continue
            if evt_type in ("FORCE_RESET_STATE", "CHALLENGE"):
                return Event(type="FORCE_RESET_STATE")