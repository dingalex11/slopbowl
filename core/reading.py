import asyncio
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

from core.events import READING_REPORTS, Event
from core.room_events import RoomEventPump


@dataclass(frozen=True)
class ReadTimingPolicy:
    """The pacing constants used to decide when a read report can be believed."""

    min_read_seconds: float
    chars_per_second: float
    early_report_fraction: float
    watchdog_factor: float
    watchdog_min_seconds: float
    slow_chars_per_second: float


def event_type(event: Event) -> str:
    return event.type.name if hasattr(event.type, "name") else str(event.type)


class ReadSession:
    """Owns the read clock, report matching, and stale-completion cleanup."""

    def __init__(self, clock: Callable[[], float]):
        self._clock = clock
        self.current_reading_id = None
        self.read_earliest_end = 0.0
        self.read_deadline = 0.0
        self.read_assumed_end = 0.0
        self.read_report_doubted = False
        self.reading_expected_users = set()
        self.reading_completions = set()

    def new_reading(self, *, online_mode: bool, expected_users) -> str | None:
        if not online_mode:
            return None
        self.current_reading_id = uuid.uuid4().hex
        self.reading_expected_users = set(expected_users)
        self.reading_completions = set()
        return self.current_reading_id

    def reset(self) -> None:
        self.current_reading_id = None
        self.reading_expected_users.clear()
        self.reading_completions.clear()

    def drop_stale_reports(self, inbound_queue: asyncio.Queue) -> None:
        """Remove reports left over from the previous read before starting a fresh one."""
        kept = []
        while not inbound_queue.empty():
            try:
                event = inbound_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if event_type(event) not in READING_REPORTS:
                kept.append(event)
        for event in kept:
            inbound_queue.put_nowait(event)

    def set_window(self, text: str, *, immediate: bool, policy: ReadTimingPolicy) -> None:
        now = self._clock()
        self.read_report_doubted = False
        if immediate:
            self.read_earliest_end = now
            self.read_assumed_end = now
            self.read_deadline = now + policy.watchdog_min_seconds
            return
        length = len(text or "")
        estimate = max(policy.min_read_seconds, length / policy.chars_per_second)
        self.read_earliest_end = now + estimate * policy.early_report_fraction
        self.read_assumed_end = now + max(policy.min_read_seconds, length / policy.slow_chars_per_second)
        self.read_deadline = now + estimate * policy.watchdog_factor + policy.watchdog_min_seconds

    def wait_target(self) -> float:
        target = self.read_assumed_end if self.read_report_doubted else self.read_deadline
        return min(target, self.read_deadline)

    def wait_expired(self) -> bool:
        return self._clock() >= self.wait_target()

    def wait_slice(self) -> float:
        return max(0.05, min(0.5, self.wait_target() - self._clock()))

    def report_is_early(self) -> bool:
        return self._clock() < self.read_earliest_end

    def report_is_believable(self) -> bool:
        return not self.report_is_early()

    def note_impossible_report(self) -> None:
        self.read_report_doubted = True

    def classify(self, event: Event, *, online_mode: bool) -> str | None:
        kind = event_type(event)
        if kind not in READING_REPORTS:
            return None
        if online_mode and event.payload.get("reading_id") != self.current_reading_id:
            return None
        return "failed" if kind == "READING_FAILED" else "done"

    async def wait_for_report(
        self,
        inbound_queue: asyncio.Queue,
        event_pump: RoomEventPump,
        *,
        online_mode: bool,
        on_other: Callable[[Event], Awaitable[str | None]] | None = None,
        on_early: Callable[[], None] | None = None,
        requeue_other: bool = False,
    ) -> tuple[str, Event | None]:
        """Wait for this read to end, serving admin events on the way.

        ``on_other`` may return an outcome for an event the caller handles (a buzz
        or an interrupt); otherwise unhandled events are requeued or dropped to
        match the caller's existing behavior.
        """
        while True:
            try:
                reply = await asyncio.wait_for(inbound_queue.get(), timeout=self.wait_slice())
            except asyncio.TimeoutError:
                if not self.wait_expired():
                    continue
                return "timeout", None

            report = self.classify(reply, online_mode=online_mode)
            if report is not None:
                if report == "done" and self.report_is_early():
                    if on_early is not None:
                        on_early()
                    continue
                self.current_reading_id = None
                return report, reply

            if await event_pump.dispatch_admin(reply):
                continue
            if event_type(reply) in READING_REPORTS:
                continue  # A report for an earlier read: it is not this one.
            if on_other is not None:
                outcome = await on_other(reply)
                if outcome is not None:
                    return outcome, reply
                continue
            if requeue_other:
                inbound_queue.put_nowait(reply)
                await asyncio.sleep(0.05)


class TossupSession:
    """Read timing plus the shared timed wait used by buzz and answer windows."""

    HANDLED = object()

    def __init__(self, inbound_queue: asyncio.Queue, event_pump: RoomEventPump, clock: Callable[[], float]):
        self.read = ReadSession(clock)
        self._inbound_queue = inbound_queue
        self._event_pump = event_pump

    async def wait_for_event(self, timeout: float) -> Event | object | None:
        """Return the next actionable event, HANDLED for admin, or None on timeout."""
        try:
            event = await asyncio.wait_for(self._inbound_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        if await self._event_pump.dispatch_admin(event):
            return self.HANDLED
        return event
