from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import firebase_admin
from firebase_admin import auth, firestore, initialize_app
from firebase_admin.exceptions import FirebaseError

from core.config import FIREBASE_PROJECT_ID


@dataclass(frozen=True)
class AuthenticatedUser:
    uid: str
    claims: dict[str, Any]


@lru_cache(maxsize=1)
def _firebase_app():
    try:
        return initialize_app(options={"projectId": FIREBASE_PROJECT_ID})
    except ValueError:
        return firebase_admin.get_app()


def verify_id_token(token: str) -> AuthenticatedUser:
    if not isinstance(token, str) or not token or len(token) > 8192:
        raise ValueError("Missing or invalid Firebase ID token")
    try:
        claims = auth.verify_id_token(token, app=_firebase_app())
    except FirebaseError as exc:
        raise ValueError("Invalid or expired Firebase ID token") from exc
    except ValueError as exc:
        raise ValueError("Invalid or expired Firebase ID token") from exc
    uid = claims.get("uid")
    if not isinstance(uid, str) or not uid:
        raise ValueError("ID token does not identify a user")
    return AuthenticatedUser(uid=uid, claims=claims)


def get_room_info(room_id: str) -> dict[str, str]:
    try:
        snapshot = firestore.client(_firebase_app()).collection("rooms").document(room_id).get(timeout=5)
    except (FirebaseError, ValueError) as exc:
        raise RuntimeError("Room authorization service is unavailable") from exc
    if not snapshot.exists:
        raise PermissionError("Room does not exist")
    room_data = snapshot.to_dict() or {}
    owner_uid = room_data.get("ownerUid")
    mode = room_data.get("mode", "in_person")
    if not isinstance(owner_uid, str) or not owner_uid or mode not in {"in_person", "online"}:
        raise PermissionError("Room metadata is invalid")
    return {"owner_uid": owner_uid, "mode": mode}


def get_room_owner(room_id: str) -> str:
    return get_room_info(room_id)["owner_uid"]


def get_room_participant(room_id: str, uid: str) -> dict[str, str | bool] | None:
    try:
        participant = (
            firestore.client(_firebase_app())
            .collection("rooms").document(room_id)
            .collection("participants").document(uid).get(timeout=5)
        )
    except (FirebaseError, ValueError) as exc:
        raise RuntimeError("Room authorization service is unavailable") from exc
    if not participant.exists:
        return None
    data = participant.to_dict() or {}
    team = data.get("team")
    name = data.get("name")
    captain = data.get("captain", False)
    if team not in {"Team A", "Team B"} or not isinstance(name, str) or not name.strip() or len(name) > 64 or not isinstance(captain, bool):
        return None
    return {"team": team, "name": name.strip(), "captain": captain}


def get_room_participants(room_id: str) -> dict[str, dict[str, str | bool]]:
    try:
        snapshots = (
            firestore.client(_firebase_app())
            .collection("rooms").document(room_id)
            .collection("participants").stream()
        )
        participants = {}
        for snapshot in snapshots:
            data = snapshot.to_dict() or {}
            team = data.get("team")
            name = data.get("name")
            captain = data.get("captain", False)
            if team in {"Team A", "Team B"} and isinstance(name, str) and name.strip() and len(name) <= 64 and isinstance(captain, bool):
                participants[snapshot.id] = {"team": team, "name": name.strip(), "captain": captain}
        return participants
    except (FirebaseError, ValueError) as exc:
        raise RuntimeError("Room authorization service is unavailable") from exc


def join_online_room(room_id: str, uid: str, name: str, team: str, captain: bool) -> dict[str, str | bool]:
    if team not in {"Team A", "Team B"}:
        raise ValueError("Choose Team A or Team B")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64 or any(ord(char) < 32 for char in name):
        raise ValueError("Player name must be 1 to 64 characters without control characters")
    if not isinstance(captain, bool):
        raise ValueError("Captain selection must be true or false")
    client = firestore.client(_firebase_app())
    room_ref = client.collection("rooms").document(room_id)
    try:
        @firestore.transactional
        def claim(transaction):
            room_snapshot = room_ref.get(transaction=transaction)
            room_data = room_snapshot.to_dict() or {}
            if not room_snapshot.exists or room_data.get("mode", "in_person") != "online":
                raise PermissionError("This is not an online match room")
            participant_ref = room_ref.collection("participants").document(uid)
            participant_snapshot = participant_ref.get(transaction=transaction)
            existing = participant_snapshot.to_dict() if participant_snapshot.exists else {}
            old_team = existing.get("team")
            old_captain = bool(existing.get("captain", False))
            captain_ref = room_ref.collection("participants").document(f"captain-{team}")
            captain_snapshot = next(iter(transaction.get(captain_ref)), None)
            captain_snapshot_exists = captain_snapshot is not None and captain_snapshot.exists
            old_captain_ref = None
            old_captain_snapshot = None
            if old_captain and old_team in {"Team A", "Team B"} and old_team != team:
                old_captain_ref = room_ref.collection("participants").document(f"captain-{old_team}")
                old_captain_snapshot = next(iter(transaction.get(old_captain_ref)), None)
            if captain and captain_snapshot_exists and captain_snapshot.get("uid") != uid:
                raise PermissionError("That team already has a captain")
            if old_captain_ref and old_captain_snapshot and old_captain_snapshot.exists and old_captain_snapshot.get("uid") == uid:
                transaction.delete(old_captain_ref)
            if captain:
                transaction.set(captain_ref, {"uid": uid, "name": name.strip(), "team": team})
            elif captain_snapshot_exists and captain_snapshot.get("uid") == uid:
                transaction.delete(captain_ref)
            transaction.set(participant_ref, {
                "name": name.strip(),
                "team": team,
                "captain": captain,
            })
            return {"name": name.strip(), "team": team, "captain": captain}

        return claim(client.transaction())
    except PermissionError:
        raise
    except (FirebaseError, ValueError) as exc:
        raise RuntimeError("Room authorization service is unavailable") from exc
