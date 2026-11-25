import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List

from fastapi import WebSocket, WebSocketDisconnect

from core.config import (
    INBOUND_QUEUE_SIZE,
    MAX_ROOMS,
    OUTBOUND_QUEUE_SIZE,
    ROOM_IDLE_TTL_SECONDS,
    ROOM_REAP_INTERVAL_SECONDS,
)
from core.events import ONLINE_PUBLIC_OUTPUTS, OWNER_ONLY_OUTPUTS
from services.auth_service import delete_room

logger = logging.getLogger(__name__)


@dataclass(eq=False)
class RoomConnection:
    websocket: WebSocket
    user_id: str
    role: str
    seat: str | None = None
    team: str | None = None
    player_name: str | None = None
    captain: bool = False
    message_times: deque = field(default_factory=deque)
    frame_times: deque = field(default_factory=deque)
    ping_times: deque = field(default_factory=deque)

    def allow_message(self, event_type: str) -> bool:
        now = time.monotonic()
        while self.message_times and now - self.message_times[0] > 1:
            self.message_times.popleft()
        if event_type == "PING":
            while self.ping_times and now - self.ping_times[0] > 1:
                self.ping_times.popleft()
            self.ping_times.append(now)
            return len(self.ping_times) <= 10
        self.message_times.append(now)
        if len(self.message_times) > 60:
            return False
        if event_type == "PROCESS_FRAME":
            while self.frame_times and now - self.frame_times[0] > 1:
                self.frame_times.popleft()
            self.frame_times.append(now)
            if len(self.frame_times) > 6:
                return False
        return True


def arbitrate_online_buzz(moderator, connection: RoomConnection, payload: dict) -> dict:
    if not moderator.online_buzz_open:
        raise PermissionError("Buzzing is currently closed")

    if connection.team not in {"Team A", "Team B"}:
        raise PermissionError("Join a team before buzzing")
    if payload.get("team", connection.team) != connection.team:
        raise PermissionError("Buzz team does not match your joined team")
    if moderator.online_buzz_team and connection.team != moderator.online_buzz_team:
        raise PermissionError("Your team cannot buzz right now")
    if moderator.online_buzz_captain_only and not connection.captain:
        raise PermissionError("Only your team's captain may buzz during a bonus")
    moderator.online_buzz_open = False
    return {
        "team": connection.team,
        "player": connection.player_name or "Player",
        "user_id": connection.user_id,
        "captain": connection.captain,
    }


def online_answer_audio_allowed(moderator, user_id: str, role: str) -> bool:
    return role in {"owner", "player"} and moderator.answer_audio_open and moderator.answering_user_id == user_id


class RoomManager:
    def __init__(self):
        self.rooms: Dict[str, object] = {}
        self.room_modes: Dict[str, str] = {}
        self.active_connections: Dict[str, List[RoomConnection]] = {}
        self.room_tasks: Dict[str, List[asyncio.Task]] = {}
        self.last_activity: Dict[str, float] = {}
        self.reaper_task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.buzz_locks: Dict[str, asyncio.Lock] = {}

    async def connect(self, websocket: WebSocket, room_id: str, user_id: str, role: str, seat: str | None = None, *, mode: str = "in_person", team: str | None = None, player_name: str | None = None, captain: bool = False):
        async with self.lock:
            self.buzz_locks.setdefault(room_id, asyncio.Lock())
            self.touch(room_id)
            moderator = self.rooms.get(room_id)
            if moderator is None:
                if len(self.rooms) >= MAX_ROOMS:
                    raise RuntimeError("Room capacity reached")
                from main import ScienceBowlModerator
                manual_reader = mode == "in_person"
                moderator = ScienceBowlModerator(use_camera=False, use_audio=not manual_reader, manual_reader=manual_reader, initial_bank=[], inbound_queue_size=INBOUND_QUEUE_SIZE, outbound_queue_size=OUTBOUND_QUEUE_SIZE)
                moderator.online_mode = mode == "online"
                moderator.online_participants = {}
                moderator.answering_user_id = None
                self.rooms[room_id] = moderator
                self.room_modes[room_id] = mode
                self.active_connections[room_id] = []
                tasks = [asyncio.create_task(moderator.run_game_loop(), name=f"game:{room_id}"), asyncio.create_task(self.listen_for_outbound(room_id, moderator), name=f"outbound:{room_id}")]
                for task in tasks:
                    task.add_done_callback(self._log_task_failure)
                self.room_tasks[room_id] = tasks
            elif self.room_modes.get(room_id, "in_person") != mode:
                raise PermissionError("Room mode does not match its active session")
            connection = RoomConnection(websocket, user_id, role, seat, team, player_name, captain)
            self.active_connections[room_id].append(connection)
            if mode == "online":
                moderator.online_participants = {item.user_id: item for item in self.active_connections[room_id] if item.role in {"owner", "player"} and item.team}

        self._ensure_reaper()
        await websocket.send_json({"type": "CONNECTED", "payload": {"role": role, "seat": seat, "mode": mode, "team": team, "name": player_name, "captain": captain, "reading_mode": getattr(moderator, "reading_mode", "stations"), "moderator_reading": dict(getattr(moderator, "moderator_reading", {"active": False, "kind": None})), "manual_reader": bool(getattr(moderator, "manual_reader", False)), "manual_phase": dict(getattr(moderator, "manual_phase", {"phase": "IDLE", "seconds": None, "started_at": None})), "camera_buzz": bool(getattr(moderator, "camera_buzz_enabled", False)), "camera_zones": len(getattr(getattr(moderator, "vision", None), "rois", {}) or {})}})
        if role == "owner":
            # Re-arm the moderator's last known control so a reconnect (team change,
            # dropped socket) never leaves the HUD without a usable start button.
            await websocket.send_json({"type": "UI_STATE", "payload": dict(getattr(moderator, "ui_state", {"show": "NONE", "overrides": False}))})
        if mode == "online":
            if moderator.online_buzz_open:
                await websocket.send_json({"type": "OPEN_BUZZ", "payload": {"team": moderator.online_buzz_team, "captain_only": moderator.online_buzz_captain_only}})
            await self.broadcast_to_room(room_id, {"type": "ROOM_ROSTER", "payload": {"players": self._online_roster(room_id)}})
        return moderator, connection

    def _online_roster(self, room_id: str) -> list[dict[str, str | bool]]:
        return [
            {"name": item.player_name, "team": item.team, "captain": item.captain}
            for item in self.active_connections.get(room_id, [])
            if item.player_name and item.team
        ]

    @staticmethod
    def _log_task_failure(task: asyncio.Task):
        if not task.cancelled() and (error := task.exception()):
            logger.error("Room task failed", exc_info=(type(error), error, error.__traceback__))

    async def listen_for_outbound(self, room_id: str, moderator):
        while True:
            message = await moderator.outbound_queue.get()
            if message.get("type") == "ROOM_CLOSE_REQUESTED":
                # Let the owner receive the final acknowledgement before the socket is
                # closed. Teardown runs in its own task because it cancels this one.
                await self.broadcast_to_room(room_id, {"type": "ROOM_CLOSED", "payload": {"reason": "Match ended"}})
                close_task = asyncio.create_task(
                    self.close_room(room_id, code=4411, reason="Match ended"),
                    name=f"close:{room_id}",
                )
                close_task.add_done_callback(self._log_task_failure)
                return
            await self.broadcast_to_room(room_id, message)

    async def disconnect(self, connection: RoomConnection, room_id: str):
        roster_changed = False
        async with self.lock:
            connections = self.active_connections.get(room_id)
            if connections and connection in connections:
                connections.remove(connection)
                moderator = self.rooms.get(room_id)
                if moderator and self.room_modes.get(room_id) == "online":
                    moderator.online_participants = {item.user_id: item for item in connections if item.role in {"owner", "player"} and item.team}
                    roster_changed = True
        if roster_changed:
            await self.broadcast_to_room(room_id, {"type": "ROOM_ROSTER", "payload": {"players": self._online_roster(room_id)}})

    def touch(self, room_id: str):
        """Record player activity so the idle reaper leaves live rooms alone."""
        self.last_activity[room_id] = time.monotonic()

    def _ensure_reaper(self):
        if self.reaper_task is None or self.reaper_task.done():
            self.reaper_task = asyncio.create_task(self._reap_idle_rooms(), name="room-reaper")
            self.reaper_task.add_done_callback(self._log_task_failure)

    async def _reap_idle_rooms(self):
        while True:
            await asyncio.sleep(ROOM_REAP_INTERVAL_SECONDS)
            await self.reap_idle_rooms()

    async def reap_idle_rooms(self):
        """Close every room that has seen no player action for the idle TTL."""
        now = time.monotonic()
        async with self.lock:
            idle = [
                room_id
                for room_id, seen in self.last_activity.items()
                if room_id in self.rooms and now - seen > ROOM_IDLE_TTL_SECONDS
            ]
        for room_id in idle:
            await self._expire_room(room_id)

    async def _expire_room(self, room_id: str):
        await self.close_room(room_id, code=4410, reason="Room closed after inactivity")

    async def close_room(self, room_id: str, *, code: int = 4410, reason: str = "Room closed"):
        """Close every station, stop the room tasks, and free its Firestore code."""
        async with self.lock:
            connections = self.active_connections.pop(room_id, [])
            self.rooms.pop(room_id, None)
            self.room_modes.pop(room_id, None)
            self.last_activity.pop(room_id, None)
            self.buzz_locks.pop(room_id, None)
            tasks = self.room_tasks.pop(room_id, [])
        await asyncio.gather(*(connection.websocket.close(code=code, reason=reason) for connection in connections), return_exceptions=True)
        current_task = asyncio.current_task()
        for task in tasks:
            if task is not current_task:
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task is not current_task), return_exceptions=True)
        # A closed local room must not leave its Firestore document behind: that is
        # what kept codes "already taken" forever and stopped rooms from closing.
        try:
            await asyncio.to_thread(delete_room, room_id)
        except Exception as exc:
            logger.warning("Unable to delete room %s from the room service: %s", room_id, exc)

    async def broadcast_to_room(self, room_id: str, message: dict):
        async with self.lock:
            connections = list(self.active_connections.get(room_id, []))
            mode = self.room_modes.get(room_id, "in_person")

        async def send(connection: RoomConnection):
            kind = message.get("type")
            if mode == "online":
                if kind == "START_READING" and message.get("payload", {}).get("moderator_only") and connection.role != "owner":
                    return True
                if kind in OWNER_ONLY_OUTPUTS and kind not in ONLINE_PUBLIC_OUTPUTS and connection.role != "owner":
                    return True
                if kind == "START_READING":
                    outgoing = dict(message)
                    outgoing["payload"] = {key: value for key, value in message.get("payload", {}).items() if key != "moderator_only"}
                elif kind == "START_LISTENING":
                    target_uid = message.get("payload", {}).get("target_user_id")
                    if target_uid != connection.user_id:
                        return True
                    outgoing = dict(message)
                    outgoing["payload"] = {key: value for key, value in message.get("payload", {}).items() if key != "target_user_id"}
                else:
                    outgoing = message
            else:
                if connection.role != "owner" and kind in OWNER_ONLY_OUTPUTS:
                    return True
                outgoing = message
            try:
                await asyncio.wait_for(connection.websocket.send_json(outgoing), timeout=2)
                return True
            except (WebSocketDisconnect, RuntimeError, asyncio.TimeoutError):
                return False

        results = await asyncio.gather(*(send(connection) for connection in connections))
        for connection, sent in zip(connections, results):
            if not sent:
                await self.disconnect(connection, room_id)


manager = RoomManager()
