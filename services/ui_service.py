import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from core.config import APP_ENV, INBOUND_QUEUE_SIZE, MAX_ROOMS, MAX_WEBSOCKET_MESSAGE_BYTES, OUTBOUND_QUEUE_SIZE, PROJECT_ROOT, ROOM_IDLE_TTL_SECONDS, ROOM_REAP_INTERVAL_SECONDS
from core.events import Event
from core.validation import validate_client_event, validate_room_id
from services.auth_service import create_online_room, delete_room, get_room_info, get_room_participant, join_online_room, verify_id_token

logger = logging.getLogger(__name__)
app = FastAPI()
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")


@app.middleware("http")
async def _revalidate_client_code(request: Request, call_next):
    """Make browsers revalidate the client code instead of caching it heuristically.

    A tab holding an older websocket.js keeps speaking the older read protocol, which
    makes an already-deployed fix look like a live bug. no-cache still permits 304s.
    """
    response = await call_next(request)
    cacheable = request.url.path.startswith("/static/") or response.headers.get("content-type", "").startswith("text/html")
    if cacheable:
        response.headers["Cache-Control"] = "no-cache"
    return response

OWNER_ONLY_EVENTS = {"SAVE_ROSTER", "LOAD_BANK", "SAVE_CALIBRATION", "RESET_CALIBRATION", "CALIBRATE_BASELINE", "PROCESS_FRAME", "ADJUST_SCORE", "EXPORT_CSV", "EDIT_LOG_ENTRY", "CHALLENGE", "FORCE_RESET_STATE", "FORCE_JUDGMENT", "FORCE_ACCEPT", "MANUAL_START_TOSSUP", "MANUAL_START_BONUS", "MANUAL_REREAD_TOSSUP",    "PAUSE_MATCH", "MATCH_BEGIN", "READING_DONE", "READING_FAILED", "ANSWER_AUDIO", "LOG_BUZZPOINT", "AUTH_INIT"}
OWNER_ONLY_OUTPUTS = {"START_READING", "START_LISTENING", "UPDATE_QUESTION", "UI_STATE", "NEW_LOG_ENTRY", "SYNC_LOG", "DOWNLOAD_CSV", "DEBUG_FRAME"}
ONLINE_PUBLIC_OUTPUTS = {"START_READING", "START_LISTENING", "UPDATE_QUESTION", "UI_STATE", "NEW_LOG_ENTRY", "SYNC_LOG", "UPDATE_STATUS", "UPDATE_SCORE", "BUZZ", "ROOM_ROSTER", "CONNECTED", "ERROR", "OPEN_BUZZ", "CLOSE_BUZZ"}
ONLINE_PLAYER_EVENTS = {"BUZZ", "ANSWER_AUDIO", "READING_DONE", "READING_FAILED", "PING"}


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
    return {"team": connection.team, "player": connection.player_name or "Player", "user_id": connection.user_id, "captain": connection.captain}



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
                moderator = ScienceBowlModerator(use_camera=(mode == "in_person"), use_audio=True, initial_bank=[], inbound_queue_size=INBOUND_QUEUE_SIZE, outbound_queue_size=OUTBOUND_QUEUE_SIZE)
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
        await websocket.send_json({"type": "CONNECTED", "payload": {"role": role, "seat": seat, "mode": mode, "team": team, "name": player_name, "captain": captain}})
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
            await self.broadcast_to_room(room_id, await moderator.outbound_queue.get())

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
        async with self.lock:
            connections = self.active_connections.pop(room_id, [])
            self.rooms.pop(room_id, None)
            self.room_modes.pop(room_id, None)
            self.last_activity.pop(room_id, None)
            self.buzz_locks.pop(room_id, None)
            tasks = self.room_tasks.pop(room_id, [])
        await asyncio.gather(*(connection.websocket.close(code=4410, reason="Room closed after inactivity") for connection in connections), return_exceptions=True)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
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


class HTMLGUIService:
    def __init__(self, inbound_queue: asyncio.Queue, outbound_queue: asyncio.Queue, vision_service=None):
        self.inbound_queue = inbound_queue
        self.outbound_queue = outbound_queue
        self.vision_service = vision_service
        self.active_connections: list[WebSocket] = []
        app.state.ui_service = self

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        if websocket not in self.active_connections:
            self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        connections = list(self.active_connections)
        results = await asyncio.gather(*(asyncio.wait_for(ws.send_json(message), timeout=2) for ws in connections), return_exceptions=True)
        self.active_connections = [ws for ws, result in zip(connections, results) if not isinstance(result, BaseException)]

    async def start_server(self):
        port = int(os.environ.get("PORT", 8080))
        config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning", ws_max_size=MAX_WEBSOCKET_MESSAGE_BYTES)
        await uvicorn.Server(config).serve()

    async def update_ui_from_queue(self):
        while True:
            event = await self.outbound_queue.get()
            if isinstance(event, dict):
                await self.broadcast(event)
            else:
                event_type = event.type.name if hasattr(event.type, "name") else str(event.type)
                await self.broadcast({"type": event_type, "payload": getattr(event, "payload", {})})


def _render_index(filename: str) -> HTMLResponse:
    html = (PROJECT_ROOT / filename).read_text(encoding="utf-8")
    config_values = {
        "__FIREBASE_API_KEY__": os.environ.get("FIREBASE_API_KEY", ""),
        "__FIREBASE_AUTH_DOMAIN__": os.environ.get("FIREBASE_AUTH_DOMAIN", ""),
        "__FIREBASE_PROJECT_ID__": os.environ.get("FIREBASE_PROJECT_ID", ""),
        "__FIREBASE_STORAGE_BUCKET__": os.environ.get("FIREBASE_STORAGE_BUCKET", ""),
        "__FIREBASE_MESSAGING_SENDER_ID__": os.environ.get("FIREBASE_MESSAGING_SENDER_ID", ""),
        "__FIREBASE_APP_ID__": os.environ.get("FIREBASE_APP_ID", ""),
    }
    for marker, value in config_values.items():
        encoded = json.dumps(value)[1:-1].replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        html = html.replace(marker, encoded)
    return HTMLResponse(html)


@app.get("/")
async def get_index():
    return _render_index("index.html")


@app.get("/legacy")
async def get_legacy_index():
    return _render_index("index_legacy.html")


def _bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Sign in required")
    return token


async def _authenticated_user(request: Request):
    """Resolve the signed-in Firebase user, mapping auth failures to honest HTTP codes."""
    token = _bearer_token(request)
    try:
        return await asyncio.to_thread(verify_id_token, token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Sign in required") from exc
    except Exception as exc:
        # A missing/invalid service account must not look like a broken client request.
        logger.warning("Unable to verify Firebase identity: %s", exc)
        raise HTTPException(status_code=503, detail="Authentication service unavailable") from exc


@app.post("/api/rooms")
async def create_generated_online_room(request: Request):
    """Allocate a fresh room code so creating a match cannot collide with a taken one."""
    user = await _authenticated_user(request)
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("Invalid room setup")
        claims = getattr(user, "claims", {}) or {}
        room = await asyncio.to_thread(
            create_online_room,
            user.uid,
            data.get("name") or claims.get("name") or "Moderator",
            data.get("team", "Team A"),
            data.get("captain", False),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.warning("Unable to create an online room: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("Unable to create an online room: %s", exc)
        raise HTTPException(status_code=503, detail="Room service unavailable") from exc
    return {"mode": "online", **room}


@app.post("/api/rooms/{room_id}/online")
async def create_online_room_endpoint(room_id: str, request: Request):
    if not validate_room_id(room_id):
        raise HTTPException(status_code=400, detail="Invalid room code")
    user = await _authenticated_user(request)
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("Invalid room setup")
        from services.auth_service import _firebase_app
        from firebase_admin import firestore
        room_ref = firestore.client(_firebase_app()).collection("rooms").document(room_id)
        room_ref.create({"ownerUid": user.uid, "createdAt": time.time(), "mode": "online"})
        profile = await asyncio.to_thread(join_online_room, room_id, user.uid, data.get("name") or user.claims.get("name") or "Moderator", data.get("team", "Team A"), data.get("captain", False))
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("Unable to create online room %s: %s", room_id, exc)
        raise HTTPException(status_code=503, detail="Room service unavailable or room code already taken") from exc
    return {"room_id": room_id, "mode": "online", **profile}



@app.post("/api/rooms/{room_id}/join")
async def join_online_room_api(room_id: str, request: Request):
    if not validate_room_id(room_id):
        raise HTTPException(status_code=400, detail="Invalid room code")
    user = await _authenticated_user(request)
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("Invalid join request")
        participant = await asyncio.to_thread(join_online_room, room_id, user.uid, data.get("name"), data.get("team"), data.get("captain", False))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Room service unavailable") from exc
    return participant


@app.websocket("/ws")
async def legacy_root_websocket_endpoint(websocket: WebSocket):
    await websocket.close(code=4404, reason="Use the authenticated room endpoint")


@app.websocket("/ws/{room_id}")
async def room_websocket_endpoint(websocket: WebSocket, room_id: str):
    if not validate_room_id(room_id):
        await websocket.close(code=4400, reason="Invalid room identifier")
        return
    origin = websocket.headers.get("origin")
    parsed_origin = urlparse(origin or "")
    host = (websocket.headers.get("host") or "").lower()
    if not origin or parsed_origin.netloc.lower() != host or parsed_origin.scheme not in ("http", "https"):
        await websocket.close(code=4403, reason="Origin is not allowed")
        return
    if APP_ENV == "production" and parsed_origin.scheme != "https":
        await websocket.close(code=4403, reason="Secure WebSocket required")
        return

    connection = None
    try:
        await websocket.accept()
        try:
            first = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        except asyncio.TimeoutError:
            await websocket.close(code=4408, reason="Authentication handshake timed out")
            return
        if len(first.encode("utf-8")) > MAX_WEBSOCKET_MESSAGE_BYTES:
            await websocket.close(code=4409, reason="Message too large")
            return
        try:
            handshake = json.loads(first)
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid authentication message") from exc
        if not isinstance(handshake, dict):
            raise ValueError("Invalid authentication message")
        auth_payload = handshake.get("payload", {})
        if handshake.get("type") != "AUTH_INIT" or not isinstance(auth_payload, dict):
            await websocket.close(code=4401, reason="Authentication required")
            return
        user = verify_id_token(auth_payload.get("id_token", ""))
        room_info = await asyncio.to_thread(get_room_info, room_id)
        mode = room_info["mode"]
        owner_uid = room_info["owner_uid"]
        api_key = auth_payload.get("api_key", "")
        if api_key:
            validate_client_event("AUTH_INIT", {"api_key": api_key})

        if user.uid == owner_uid:
            role = "owner"
            profile = await asyncio.to_thread(get_room_participant, room_id, user.uid) if mode == "online" else {"name": "Moderator", "team": "Team A", "captain": False}
            if mode == "online" and not profile:
                await websocket.close(code=4403, reason="Join the online room as a player before connecting")
                return
        else:
            if mode != "online":
                await websocket.close(code=4403, reason="Only the room owner may connect to an in-person moderator station")
                return
            profile = await asyncio.to_thread(get_room_participant, room_id, user.uid)
            if not profile:
                await websocket.close(code=4403, reason="Join the online room before connecting")
                return
            role = "player"
        team = profile.get("team") if mode == "online" else None
        player_name = profile.get("name") if mode == "online" else None
        captain = bool(profile.get("captain", False)) if mode == "online" else False
        moderator, connection = await manager.connect(websocket, room_id, user.uid, role, mode=mode, team=team, player_name=player_name, captain=captain)
        if api_key and role == "owner":
            try:
                moderator.inbound_queue.put_nowait(Event(type="AUTH_INIT", payload={"api_key": api_key}, user_id=user.uid, role=role))
            except asyncio.QueueFull:
                await websocket.close(code=4429, reason="Room is busy; retry later")
                return

        while True:
            raw = await websocket.receive_text()
            if len(raw.encode("utf-8")) > MAX_WEBSOCKET_MESSAGE_BYTES:
                await websocket.close(code=4409, reason="Message too large")
                break
            try:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError("Message must be an object")
                event_type = message.get("type")
                event_payload = validate_client_event(event_type, message.get("payload", {}))
            except (json.JSONDecodeError, AttributeError, ValueError) as exc:
                await websocket.send_json({"type": "ERROR", "payload": {"message": str(exc)}})
                continue
            if event_type != "PING":
                # Heartbeats are not action: a forgotten open tab must still time out.
                manager.touch(room_id)
            if not connection.allow_message(event_type):
                await websocket.close(code=4429, reason="Rate limit exceeded")
                break
            if event_type == "MATCH_BEGIN" and role != "owner":
                await websocket.send_json({"type": "ERROR", "payload": {"message": "Only the moderator can begin the match"}})
                continue
            if mode == "online" and event_type == "PING":
                continue
            if mode == "online" and event_type in ("READING_DONE", "READING_FAILED"):
                expected = moderator.current_reading_id
                if not expected or event_payload.get("reading_id") != expected:
                    continue
                if event_type == "READING_FAILED" and user.uid not in moderator.reading_expected_users:
                    # A listener whose own browser could not speak does not end the room's
                    # read; only the station that reads the question decides when it is over.
                    logger.warning("Read %s could not be spoken by %s", expected, user.uid)
                    await websocket.send_json({"type": "ERROR", "payload": {"message": "Your browser could not read this question aloud."}})
                    continue
                if user.uid not in moderator.online_participants or user.uid in moderator.reading_completions:
                    continue
                if event_type == "READING_DONE" and not moderator.read_report_is_believable():
                    # Too fast to be the question being spoken (a station on older code
                    # reports it the moment the read starts). Dropped before it can mark the
                    # read finished, so the real report still lands.
                    logger.info("Ignored an impossibly fast read report for %s from %s", expected, user.uid)
                    moderator.note_impossible_read_report()
                    continue
                moderator.reading_completions.add(user.uid)
                if moderator.reading_expected_users.issubset(moderator.reading_completions):
                    # Names the station that ended the read, so a read that ends too early
                    # can be traced from the server logs alone.
                    logger.info("Read %s ended by %s (%s)", expected, user.uid, event_type)
                    try:
                        moderator.inbound_queue.put_nowait(Event(type=event_type, payload={"reading_id": expected}, user_id=user.uid, role=role))
                    except asyncio.QueueFull:
                        await websocket.send_json({"type": "ERROR", "payload": {"message": "Room is busy"}})
                continue
            if mode == "online" and event_type == "ANSWER_AUDIO":
                if not online_answer_audio_allowed(moderator, user.uid, role):
                    await websocket.send_json({"type": "ERROR", "payload": {"message": "Only the recognized player may submit answer audio during the answer window"}})
                    continue
                moderator.answer_audio_open = False
            if role == "player":
                if mode != "online" or event_type not in ONLINE_PLAYER_EVENTS:
                    await websocket.send_json({"type": "ERROR", "payload": {"message": "Room owner control required"}})
                    continue
                if event_type == "BUZZ":
                    async with manager.buzz_locks.setdefault(room_id, asyncio.Lock()):
                        try:
                            event_payload = arbitrate_online_buzz(moderator, connection, event_payload)
                        except PermissionError as exc:
                            await websocket.send_json({"type": "ERROR", "payload": {"message": str(exc)}})
                            continue
            elif role == "owner" and mode == "online" and event_type == "BUZZ":
                async with manager.buzz_locks.setdefault(room_id, asyncio.Lock()):
                    try:
                        event_payload = arbitrate_online_buzz(moderator, connection, event_payload)
                    except PermissionError as exc:
                        await websocket.send_json({"type": "ERROR", "payload": {"message": str(exc)}})
                        continue
            elif event_type == "BUZZ" and mode == "in_person":
                player = event_payload.get("player")
                expected_team = "Team A" if player == "Manual-A" else "Team B"
                if player not in ("Manual-A", "Manual-B") or event_payload.get("team") != expected_team:
                    await websocket.send_json({"type": "ERROR", "payload": {"message": "Manual buzz must match Team A or Team B"}})
                    continue
            elif role != "owner" and event_type in OWNER_ONLY_EVENTS and not (mode == "online" and event_type in ONLINE_PLAYER_EVENTS):
                await websocket.send_json({"type": "ERROR", "payload": {"message": "Room owner control required"}})
                continue
            try:
                moderator.inbound_queue.put_nowait(Event(type=event_type, payload=event_payload, user_id=user.uid, role=role))
            except asyncio.QueueFull:
                await websocket.close(code=4429, reason="Room is busy; retry later")
                break
    except WebSocketDisconnect:
        pass
    except RuntimeError as exc:
        logger.warning("Room or Firebase authorization service unavailable for %s: %s", room_id, exc)
        try:
            await websocket.close(code=4503, reason="Room or authorization service unavailable")
        except RuntimeError:
            pass
    except (ValueError, PermissionError) as exc:
        logger.info("Rejected WebSocket authorization for %s", room_id)
        try:
            await websocket.close(code=4401, reason="Authentication failed")
        except RuntimeError:
            pass
    except Exception as exc:
        # Unexpected authorization failures (for example a missing service account) must
        # still reach the client as a labelled close instead of a generic 1011 drop.
        logger.warning("Unexpected WebSocket authorization failure for %s: %s", room_id, exc)
        try:
            await websocket.close(code=4503, reason="Authorization service unavailable")
        except RuntimeError:
            pass
    finally:
        if connection:
            await manager.disconnect(connection, room_id)
