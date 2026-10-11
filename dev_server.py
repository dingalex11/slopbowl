"""Local end-to-end harness: the real app with fake identity and in-memory rooms.

Runs the actual server, client, game loop and read/buzz protocol, but swaps the two
things that need credentials:

* Firebase ID tokens -> two fixed development tokens
* Firestore rooms/participants -> in-memory dictionaries

Open ``/dev/owner`` and ``/dev/player`` in two tabs. Each page carries a small shim
that satisfies the client's auth surface, so the two stations hold separate identities
without any browser sign-in or stored session.

    .venv/Scripts/python.exe dev_server.py        # or: PORT=8099 python dev_server.py

This is a development tool. It never touches a real project, and it refuses to run
with APP_ENV=production.
"""

import json
import logging
import os
import sys

os.environ.setdefault("APP_ENV", "development")

from fastapi.responses import HTMLResponse  # noqa: E402  (after the env guard)

import main as moderator_module  # noqa: E402
import services.auth_service as auth_service  # noqa: E402
import services.ui_service as ui_service  # noqa: E402
from core.config import APP_ENV, PROJECT_ROOT  # noqa: E402
from core.events import Event  # noqa: E402
from core.validation import validate_question_bank  # noqa: E402
from services.auth_service import AuthenticatedUser, generate_room_code  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
logger = logging.getLogger("dev-harness")

# --- development identities -------------------------------------------------

DEV_USERS = {
    "dev-owner-token": ("dev-owner", "Modstation"),
    "dev-player-token": ("dev-player", "Buzzstation"),
}

# --- in-memory rooms (stands in for Firestore) ------------------------------

ROOMS: dict[str, dict] = {}
PARTICIPANTS: dict[str, dict[str, dict]] = {}


def verify_dev_token(token):
    if token not in DEV_USERS:
        raise ValueError("Unknown development token")
    uid, _name = DEV_USERS[token]
    return AuthenticatedUser(uid=uid, claims={"uid": uid})


def get_room_info(room_id):
    room = ROOMS.get(room_id)
    if room is None:
        raise PermissionError("Room does not exist")
    return {"owner_uid": room["ownerUid"], "mode": room["mode"]}


def get_room_participant(room_id, uid):
    return PARTICIPANTS.get(room_id, {}).get(uid)


def get_room_participants(room_id):
    return dict(PARTICIPANTS.get(room_id, {}))


def create_online_room(owner_uid, name, team, captain, *, attempts=5):
    """Mirror of the real allocator: unique code, owner registered as a participant."""
    if not isinstance(owner_uid, str) or not owner_uid:
        raise ValueError("Room owner must be a signed-in user")
    for _ in range(attempts):
        room_id = generate_room_code()
        if room_id in ROOMS:
            continue
        ROOMS[room_id] = {"ownerUid": owner_uid, "mode": "online", "captains": {}}
        PARTICIPANTS[room_id] = {}
        profile = join_online_room(room_id, owner_uid, name, team, captain)
        return {"room_id": room_id, **profile}
    raise RuntimeError("Could not allocate a free room code — try again")


def join_online_room(room_id, uid, name, team, captain):
    """Same rules as the Firestore transaction: one captain per team, one seat per uid."""
    if team not in {"Team A", "Team B"}:
        raise ValueError("Choose Team A or Team B")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64 or any(ord(char) < 32 for char in name):
        raise ValueError("Player name must be 1 to 64 characters without control characters")
    if not isinstance(captain, bool):
        raise ValueError("Captain selection must be true or false")
    room = ROOMS.get(room_id)
    if room is None or room["mode"] != "online":
        raise PermissionError("This is not an online match room")

    participants = PARTICIPANTS.setdefault(room_id, {})
    captains = room.setdefault("captains", {})
    existing = participants.get(uid, {})
    old_team = existing.get("team")
    if captain and team in captains and captains[team] != uid:
        raise PermissionError("That team already has a captain")
    if existing.get("captain") and old_team in {"Team A", "Team B"} and old_team != team and captains.get(old_team) == uid:
        captains.pop(old_team, None)
    if captain:
        captains[team] = uid
    elif captains.get(team) == uid:
        captains.pop(team, None)
    profile = {"team": team, "name": name.strip(), "captain": bool(captain)}
    participants[uid] = profile
    logger.info("participant %s joined %s as %s%s", uid, room_id, team, " (captain)" if captain else "")
    return profile


def delete_room(room_id):
    ROOMS.pop(room_id, None)
    PARTICIPANTS.pop(room_id, None)


# --- preloaded packet -------------------------------------------------------


def _load_dev_bank(limit=3):
    bank_path = PROJECT_ROOT / "questions" / "m_round01_bank.json"
    with bank_path.open(encoding="utf-8") as handle:
        bank = json.load(handle)
    return validate_question_bank(bank[:limit])


DEV_BANK = _load_dev_bank()


# --- patches ----------------------------------------------------------------

auth_service.verify_id_token = verify_dev_token
ui_service.verify_id_token = verify_dev_token
ui_service.get_room_info = get_room_info
ui_service.get_room_participant = get_room_participant
ui_service.create_online_room = create_online_room
ui_service.join_online_room = join_online_room
ui_service.delete_room = delete_room

_real_moderator = moderator_module.ScienceBowlModerator


def _moderator_with_dev_packet(*args, **kwargs):
    # The room manager always passes initial_bank=[], so an empty packet is replaced
    # rather than merely defaulted.
    if not kwargs.get("initial_bank"):
        kwargs["initial_bank"] = list(DEV_BANK)
    return _real_moderator(*args, **kwargs)


# ui_service imports the class inside RoomManager.connect, so patch it on the module
# the class is really imported from.
moderator_module.ScienceBowlModerator = _moderator_with_dev_packet


# --- dev pages --------------------------------------------------------------

DEV_SHIM = """
  <script type="module">
    const DEV = { token: "%(token)s", uid: "%(uid)s", name: "%(name)s" };
    window.FIREBASE_CONFIG = { apiKey: "dev", authDomain: "dev.local", projectId: "dev", storageBucket: "dev", messagingSenderId: "dev", appId: "dev" };
    window.firebaseAuthReady = Promise.resolve();
    window.getFirebaseIdToken = async () => DEV.token;
    window.getFirebaseUser = () => ({ uid: DEV.uid, email: DEV.uid + "@dev.local", displayName: DEV.name });
    window.getUserApiKey = () => null;
    window.ensureRoomOwnership = async () => true;
    async function devApi(url, body) {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + DEV.token },
        body: JSON.stringify(body),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || "Request failed");
      return result;
    }
    window.createOnlineRoomApi = (name, team, captain) => devApi("/api/rooms", { name, team, captain });
    window.joinOnlineRoomApi = (roomId, name, team, captain) =>
      devApi("/api/rooms/" + encodeURIComponent(roomId) + "/join", { name, team, captain });
    console.info("[dev harness] station ready as " + DEV.name + " (" + DEV.uid + ")");
  </script>
</body>
"""


def _dev_page(token: str) -> HTMLResponse:
    uid, name = DEV_USERS[token]
    html = (PROJECT_ROOT / "index.html").read_text(encoding="utf-8")
    for marker in ("__FIREBASE_API_KEY__", "__FIREBASE_AUTH_DOMAIN__", "__FIREBASE_PROJECT_ID__",
                   "__FIREBASE_STORAGE_BUCKET__", "__FIREBASE_MESSAGING_SENDER_ID__", "__FIREBASE_APP_ID__"):
        html = html.replace(marker, "dev")
    html = html.replace("</body>", DEV_SHIM % {"token": token, "uid": uid, "name": name})
    return HTMLResponse(html)


@ui_service.app.get("/dev/owner")
async def dev_owner_page():
    return _dev_page("dev-owner-token")


@ui_service.app.get("/dev/player")
async def dev_player_page():
    return _dev_page("dev-player-token")


@ui_service.app.get("/dev/state")
async def dev_state():
    """Inspect live room and read state — the assertion surface for end-to-end runs."""
    rooms = {}
    for room_id, moderator in ui_service.manager.rooms.items():
        rooms[room_id] = {
            "mode": ui_service.manager.room_modes.get(room_id),
            "participants": get_room_participants(room_id),
            "connections": [
                {"uid": item.user_id, "role": item.role, "team": item.team, "name": item.player_name}
                for item in ui_service.manager.active_connections.get(room_id, [])
            ],
            "current_reading_id": moderator.current_reading_id,
            "reading_expected_users": sorted(moderator.reading_expected_users),
            "reading_completions": sorted(moderator.reading_completions),
            "buzz_open": moderator.online_buzz_open,
            "buzz_team": moderator.online_buzz_team,
            "ui_state": dict(moderator.ui_state),
            "questions": len(moderator.question_bank),
            "question_index": moderator.current_q_idx,
        }
    return {"rooms": rooms, "bank": [q["tossup_text"][:70] for q in DEV_BANK]}


@ui_service.app.post("/dev/load-bank")
async def dev_load_bank():
    """Re-arm the dev packet mid-run (after a room has burned through it)."""
    for moderator in ui_service.manager.rooms.values():
        moderator.question_bank = list(DEV_BANK)
        moderator.current_q_idx = 0
        moderator.inbound_queue.put_nowait(Event(type="LOAD_BANK", payload={"bank": list(DEV_BANK)}))
    return {"loaded": len(DEV_BANK)}


if __name__ == "__main__":
    if APP_ENV == "production":
        sys.exit("dev_server.py refuses to run with APP_ENV=production")

    import uvicorn

    port = int(os.environ.get("PORT", "8099"))
    logger.info("Development harness: http://localhost:%d/dev/owner and /dev/player", port)
    uvicorn.run(ui_service.app, host="127.0.0.1", port=port, log_level="info")
