import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from services import auth_service, ui_service


class FakeUser:
    def __init__(self, uid):
        self.uid = uid


class FakeModerator:
    def __init__(self):
        from types import SimpleNamespace
        self.inbound_queue = asyncio.Queue()
        self.online_buzz_open = False
        self.online_buzz_team = None
        self.online_buzz_captain_only = False
        self.answer_audio_open = False
        self.answering_user_id = None
        self.state = SimpleNamespace(current_state=SimpleNamespace(name="BONUS_CONFERRING"))
        self.online_participants = {}


class FakeConnection:
    def __init__(self):
        self.role = "owner"
        self.user_id = "owner"
        self.team = "Team A"
        self.player_name = "Ada"
        self.captain = True

    def allow_message(self, event_type):
        return True


class FakeSocket:
    def __init__(self):
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)

    async def close(self, code=None, reason=None):
        self.closed = True


class WebSocketAuthorizationTests(unittest.TestCase):
    def test_get_room_info_reads_room_defaults_from_snapshot_data(self):
        class Snapshot:
            exists = True

            def to_dict(self):
                return {"ownerUid": "owner"}

        class Document:
            def get(self, **kwargs):
                return Snapshot()

        class Collection:
            def document(self, room_id):
                return Document()

        class Client:
            def collection(self, name):
                return Collection()

        with patch.object(auth_service, "_firebase_app", return_value=object()), patch.object(
            auth_service.firestore, "client", return_value=Client()
        ):
            self.assertEqual(auth_service.get_room_info("room"), {"owner_uid": "owner", "mode": "in_person"})

    def test_moderator_interface_and_static_client_load(self):
        client = TestClient(ui_service.app)
        page = client.get("/")
        client_script = client.get("/static/js/websocket.js")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Science Bowl", page.text)
        self.assertEqual(client_script.status_code, 200)
        self.assertIn("CONNECTING TO MATCH", client_script.text)
        # The online buzzer control is bound by class since the MASSIVE FIX rename.
        self.assertIn("js-online-buzz", page.text)
        # The create-match card no longer asks for a hand-typed room code.
        self.assertNotIn("create-room-code", page.text)
        self.assertIn("online-player-name", page.text)
        # The captain-claim control is bound by claimCaptain since the MASSIVE FIX rename.
        self.assertIn('onclick="claimCaptain(', page.text)
        self.assertIn('id="online-roster"', page.text)

    def test_match_controls_and_camera_frames_are_only_sent_to_moderator_station(self):
        async def verify_broadcasts():
            manager = ui_service.RoomManager()
            owner = FakeSocket()
            spectator = FakeSocket()
            manager.active_connections["science-bowl"] = [
                ui_service.RoomConnection(owner, "moderator", "owner"),
                ui_service.RoomConnection(spectator, "viewer", "viewer"),
            ]
            for kind in ("DEBUG_FRAME", "START_READING", "START_LISTENING", "UI_STATE", "UPDATE_QUESTION", "UPDATE_SCORE"):
                await manager.broadcast_to_room("science-bowl", {"type": kind, "payload": {}})
            self.assertEqual([item["type"] for item in owner.messages], ["DEBUG_FRAME", "START_READING", "START_LISTENING", "UI_STATE", "UPDATE_QUESTION", "UPDATE_SCORE"])
            self.assertEqual([item["type"] for item in spectator.messages], ["UPDATE_SCORE"])

        asyncio.run(verify_broadcasts())

    def test_online_buzz_is_arbitrated_by_open_window_team_and_captain(self):
        moderator = FakeModerator()
        moderator.online_buzz_open = True
        moderator.online_buzz_team = "Team A"
        moderator.online_buzz_captain_only = True
        non_captain = ui_service.RoomConnection(FakeSocket(), "player-2", "player", team="Team A", player_name="Bea", captain=False)
        with self.assertRaisesRegex(PermissionError, "captain"):
            ui_service.arbitrate_online_buzz(moderator, non_captain, {})
        self.assertTrue(moderator.online_buzz_open)

        captain = ui_service.RoomConnection(FakeSocket(), "player-1", "player", team="Team A", player_name="Ada", captain=True)
        event = ui_service.arbitrate_online_buzz(moderator, captain, {})
        self.assertEqual(event, {"team": "Team A", "player": "Ada", "user_id": "player-1", "captain": True})
        self.assertFalse(moderator.online_buzz_open)
        with self.assertRaisesRegex(PermissionError, "closed"):
            ui_service.arbitrate_online_buzz(moderator, captain, {})

    def test_online_buzz_rejects_wrong_team_even_when_payload_is_forged(self):
        moderator = FakeModerator()
        moderator.online_buzz_open = True
        moderator.online_buzz_team = "Team A"
        player = ui_service.RoomConnection(FakeSocket(), "player-b", "player", team="Team B", player_name="Bo", captain=True)
        with self.assertRaisesRegex(PermissionError, "team"):
            ui_service.arbitrate_online_buzz(moderator, player, {"team": "Team A"})
        self.assertTrue(moderator.online_buzz_open)

    def test_online_buzz_window_events_are_visible_to_players(self):
        async def verify_broadcasts():
            manager = ui_service.RoomManager()
            socket = FakeSocket()
            manager.room_modes["online-room"] = "online"
            manager.active_connections["online-room"] = [ui_service.RoomConnection(socket, "player", "player", team="Team A", player_name="Ada")]
            await manager.broadcast_to_room("online-room", {"type": "OPEN_BUZZ", "payload": {"team": "Team A", "captain_only": False}})
            self.assertEqual(socket.messages[0]["type"], "OPEN_BUZZ")

    def test_online_roster_updates_for_active_connections_and_disconnects(self):
        async def verify_roster():
            manager = ui_service.RoomManager()
            owner_socket = FakeSocket()
            player_socket = FakeSocket()
            owner = ui_service.RoomConnection(owner_socket, "owner", "owner", team="Team A", player_name="Host", captain=True)
            player = ui_service.RoomConnection(player_socket, "player", "player", team="Team B", player_name="Ada")
            manager.room_modes["roster-room"] = "online"
            manager.active_connections["roster-room"] = [owner, player]
            manager.rooms["roster-room"] = FakeModerator()
            await manager.disconnect(player, "roster-room")
            self.assertEqual(manager.rooms["roster-room"].online_participants, {"owner": owner})
            self.assertEqual(owner_socket.messages[-1]["type"], "ROOM_ROSTER")
            self.assertEqual(owner_socket.messages[-1]["payload"]["players"], [{"name": "Host", "team": "Team A", "captain": True}])

        asyncio.run(verify_roster())

    def test_online_buzz_window_events_are_visible_to_players(self):
        async def verify_broadcasts():
            manager = ui_service.RoomManager()
            socket = FakeSocket()
            manager.room_modes["online-room"] = "online"
            manager.active_connections["online-room"] = [ui_service.RoomConnection(socket, "player", "player", team="Team A", player_name="Ada")]
            await manager.broadcast_to_room("online-room", {"type": "OPEN_BUZZ", "payload": {"team": "Team A", "captain_only": False}})
            self.assertEqual(socket.messages[0]["type"], "OPEN_BUZZ")

        asyncio.run(verify_broadcasts())

    def test_ping_limit_does_not_spend_control_event_rate_budget(self):
        connection = ui_service.RoomConnection(FakeSocket(), "moderator", "owner")
        for _ in range(10):
            self.assertTrue(connection.allow_message("PING"))
        self.assertFalse(connection.allow_message("PING"))
        self.assertTrue(connection.allow_message("SAVE_ROSTER"))

    def test_room_expiry_closes_viewers_removes_the_room_and_frees_the_code(self):
        async def verify_expiry():
            manager = ui_service.RoomManager()
            socket = FakeSocket()
            connection = ui_service.RoomConnection(socket, "viewer", "viewer")
            manager.active_connections["round-one"] = [connection]
            manager.rooms["round-one"] = object()
            manager.room_tasks["round-one"] = []
            manager.last_activity["round-one"] = time.monotonic()
            with patch.object(ui_service, "delete_room") as delete:
                await manager._expire_room("round-one")
            self.assertNotIn("round-one", manager.rooms)
            self.assertNotIn("round-one", manager.last_activity)
            self.assertEqual(socket.closed, True)
            delete.assert_called_once_with("round-one")

        asyncio.run(verify_expiry())

    def test_idle_room_is_reaped_after_no_player_action(self):
        async def verify_reaping():
            manager = ui_service.RoomManager()
            socket = FakeSocket()
            connection = ui_service.RoomConnection(socket, "player", "player")
            manager.rooms["idle-room"] = FakeModerator()
            manager.room_modes["idle-room"] = "online"
            manager.active_connections["idle-room"] = [connection]
            manager.last_activity["idle-room"] = time.monotonic() - 30
            manager.rooms["busy-room"] = FakeModerator()
            manager.last_activity["busy-room"] = time.monotonic()
            with patch.object(ui_service, "ROOM_IDLE_TTL_SECONDS", 10), patch.object(ui_service, "delete_room") as delete:
                await manager.reap_idle_rooms()
            self.assertNotIn("idle-room", manager.rooms)
            self.assertIn("busy-room", manager.rooms)
            self.assertEqual(socket.closed, True)
            delete.assert_called_once_with("idle-room")

        asyncio.run(verify_reaping())

    def test_reaper_task_closes_idle_rooms(self):
        async def verify_loop():
            manager = ui_service.RoomManager()
            manager.rooms["idle-room"] = FakeModerator()
            manager.last_activity["idle-room"] = time.monotonic() - 30
            with patch.object(ui_service, "ROOM_IDLE_TTL_SECONDS", 10), patch.object(
                ui_service, "ROOM_REAP_INTERVAL_SECONDS", 0.01
            ), patch.object(ui_service, "delete_room"):
                manager._ensure_reaper()
                try:
                    for _ in range(50):
                        if "idle-room" not in manager.rooms:
                            break
                        await asyncio.sleep(0.01)
                    self.assertNotIn("idle-room", manager.rooms)
                finally:
                    manager.reaper_task.cancel()
                    await asyncio.gather(manager.reaper_task, return_exceptions=True)

        asyncio.run(verify_loop())

    def test_generated_room_codes_are_valid_and_readable(self):
        from core.validation import ROOM_ID_PATTERN
        codes = {auth_service.generate_room_code() for _ in range(50)}
        for code in codes:
            self.assertRegex(code, ROOM_ID_PATTERN)
            self.assertEqual(len(code), auth_service.ROOM_CODE_LENGTH)
            self.assertTrue(set(code) <= set(auth_service.ROOM_CODE_ALPHABET))
        self.assertGreater(len(codes), 1)

    def test_room_creation_retries_past_a_taken_code(self):
        codes = iter(["taken1", "fresh1"])
        attempts = []

        class AlreadyExists(Exception):
            pass

        class Document:
            def __init__(self, room_id):
                self.room_id = room_id

            def create(self, data):
                attempts.append(self.room_id)
                if self.room_id == "taken1":
                    raise AlreadyExists()

        class Collection:
            def __init__(self):
                self.documents = {}

            def document(self, room_id):
                return self.documents.setdefault(room_id, Document(room_id))

        class Client:
            def collection(self, name):
                return Collection()

        with patch.object(auth_service, "_firebase_app", return_value=object()), patch.object(
            auth_service.firestore, "client", return_value=Client()
        ), patch.object(auth_service, "generate_room_code", side_effect=lambda: next(codes)), patch.object(
            auth_service, "join_online_room", return_value={"name": "Mod", "team": "Team A", "captain": False}
        ):
            room = auth_service.create_online_room("owner", "Mod", "Team A", False)

        self.assertEqual(room["room_id"], "fresh1")
        self.assertEqual(attempts, ["taken1", "fresh1"])

    def test_room_creation_endpoint_returns_a_generated_code(self):
        profile = {"room_id": "kx7m2p", "name": "Mod", "team": "Team A", "captain": False}
        with patch.object(ui_service, "_authenticated_user", new=AsyncMock(return_value=FakeUser("owner"))), patch.object(
            ui_service, "create_online_room", return_value=profile
        ):
            response = TestClient(ui_service.app).post("/api/rooms", json={"name": "Mod", "team": "Team A", "captain": False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["room_id"], "kx7m2p")
        self.assertEqual(response.json()["mode"], "online")

    def test_room_creation_endpoint_reports_an_unavailable_room_service(self):
        with patch.object(ui_service, "_authenticated_user", new=AsyncMock(return_value=FakeUser("owner"))), patch.object(
            ui_service, "create_online_room", side_effect=RuntimeError("Room authorization service is unavailable")
        ):
            response = TestClient(ui_service.app).post("/api/rooms", json={"name": "Mod"})
        self.assertEqual(response.status_code, 503)

    def test_online_player_can_only_submit_answer_audio_in_own_answer_window(self):
        moderator = FakeModerator()
        moderator.answer_audio_open = True
        moderator.answering_user_id = "player-1"
        self.assertTrue(ui_service.online_answer_audio_allowed(moderator, "player-1", "player"))
        self.assertFalse(ui_service.online_answer_audio_allowed(moderator, "player-2", "player"))
        self.assertFalse(ui_service.online_answer_audio_allowed(moderator, "player-1", "viewer"))
        moderator.answer_audio_open = False
        self.assertFalse(ui_service.online_answer_audio_allowed(moderator, "player-1", "player"))

    def test_online_answer_audio_is_routed_only_to_target_user(self):
        async def verify_routing():
            manager = ui_service.RoomManager()
            target = FakeSocket()
            other = FakeSocket()
            manager.room_modes["online-room"] = "online"
            manager.active_connections["online-room"] = [
                ui_service.RoomConnection(target, "answerer", "player"),
                ui_service.RoomConnection(other, "teammate", "player"),
            ]
            await manager.broadcast_to_room("online-room", {
                "type": "START_LISTENING",
                "payload": {"timeout": 8, "target_user_id": "answerer"},
            })
            self.assertEqual(len(target.messages), 1)
            self.assertNotIn("target_user_id", target.messages[0]["payload"])
            self.assertEqual(other.messages, [])

        asyncio.run(verify_routing())

    def test_reading_completion_waits_for_the_connected_moderator_only(self):
        from main import ScienceBowlModerator
        moderator = ScienceBowlModerator(use_audio=False, initial_bank=[])
        moderator.online_mode = True
        owner = ui_service.RoomConnection(FakeSocket(), "owner", "owner", team="Team A", player_name="Host")
        player = ui_service.RoomConnection(FakeSocket(), "player", "player", team="Team A", player_name="Ada")
        moderator.online_participants = {"owner": owner, "player": player}
        reading_id = moderator._new_reading_id()
        self.assertEqual(moderator.reading_expected_users, {"owner"})
        self.assertTrue(reading_id)
        self.assertEqual(moderator.reading_completions, set())

    def test_a_failed_read_is_only_reported_by_the_reading_station(self):
        """A listener that cannot speak must not end the room's read.

        The moderator station is the reader; its failure is forwarded (with the read's
        id) so the moderator is told, while a listener's failure is answered privately.
        """
        from main import ScienceBowlModerator

        def drive(uid, event_type, expect_forwarded, expect_error):
            moderator = ScienceBowlModerator(use_audio=True, initial_bank=[])
            moderator.online_mode = True
            moderator.online_participants = {
                "owner": ui_service.RoomConnection(FakeSocket(), "owner", "owner", team="Team A", player_name="Host"),
                "player": ui_service.RoomConnection(FakeSocket(), "player", "player", team="Team A", player_name="Ada"),
            }
            reading_id = moderator._new_reading_id()
            profile = {"name": "Host", "team": "Team A", "captain": False}

            async def fake_connect(websocket, room_id, user_id, role, seat=None, **kwargs):
                await websocket.send_json({"type": "CONNECTED", "payload": {"role": role, "mode": "online", **profile}})
                return moderator, ui_service.RoomConnection(websocket, user_id, role, team="Team A", player_name="Host")

            with patch.object(ui_service, "verify_id_token", return_value=FakeUser(uid)), patch.object(
                ui_service, "get_room_info", return_value={"owner_uid": "owner", "mode": "online"}
            ), patch.object(ui_service, "get_room_participant", return_value=profile), patch.object(
                ui_service.manager, "connect", side_effect=fake_connect
            ), patch.object(ui_service.manager, "disconnect", new=AsyncMock()):
                client = TestClient(ui_service.app)
                with client.websocket_connect("/ws/read-room", headers={"origin": "http://testserver"}) as websocket:
                    websocket.send_json({"type": "AUTH_INIT", "payload": {"id_token": "signed-token"}})
                    self.assertEqual(websocket.receive_json()["type"], "CONNECTED")
                    websocket.send_json({"type": event_type, "payload": {"reading_id": reading_id}})
                    if expect_error:
                        self.assertEqual(websocket.receive_json()["type"], "ERROR")
            if expect_forwarded:
                event = moderator.inbound_queue.get_nowait()
                self.assertEqual(event.type, event_type)
                self.assertEqual(event.payload, {"reading_id": reading_id})
            else:
                self.assertTrue(moderator.inbound_queue.empty())

        drive("owner", "READING_FAILED", True, False)
        drive("owner", "READING_DONE", True, False)
        drive("player", "READING_FAILED", False, True)

    def test_authenticated_player_buzz_uses_joined_identity_and_captain_claim(self):
        async def verify_buzz(profile, should_accept):
            moderator = FakeModerator()
            moderator.online_buzz_open = True
            moderator.online_buzz_team = "Team A"
            moderator.online_buzz_captain_only = True
            uid = "captain-1" if profile["captain"] else "player-1"

            async def fake_connect(websocket, room_id, user_id, role, seat=None, **kwargs):
                connection = ui_service.RoomConnection(
                    websocket, user_id, role, team=kwargs.get("team"),
                    player_name=kwargs.get("player_name"), captain=kwargs.get("captain", False),
                )
                await websocket.send_json({"type": "CONNECTED", "payload": {"role": role, "mode": "online", **profile}})
                return moderator, connection

            with patch.object(ui_service, "verify_id_token", return_value=FakeUser(uid)), patch.object(
                ui_service, "get_room_info", return_value={"owner_uid": "host", "mode": "online"}
            ), patch.object(ui_service, "get_room_participant", return_value=profile), patch.object(
                ui_service.manager, "connect", side_effect=fake_connect
            ), patch.object(ui_service.manager, "disconnect", new=AsyncMock()), patch.object(
                ui_service.manager, "buzz_locks", {"buzz-room": asyncio.Lock()}
            ):
                client = TestClient(ui_service.app)
                with client.websocket_connect("/ws/buzz-room", headers={"origin": "http://testserver"}) as websocket:
                    websocket.send_json({"type": "AUTH_INIT", "payload": {"id_token": "signed-token"}})
                    self.assertEqual(websocket.receive_json()["type"], "CONNECTED")
                    websocket.send_json({"type": "BUZZ", "payload": {}})
                    if should_accept:
                        websocket.close()
                    else:
                        self.assertEqual(websocket.receive_json()["type"], "ERROR")
            if should_accept:
                event = moderator.inbound_queue.get_nowait()
                self.assertEqual(event.payload, {"team": "Team A", "player": profile["name"], "user_id": uid, "captain": True})
            else:
                self.assertTrue(moderator.inbound_queue.empty())

        asyncio.run(verify_buzz({"name": "Ada", "team": "Team A", "captain": True}, True))
        asyncio.run(verify_buzz({"name": "Bea", "team": "Team A", "captain": False}, False))

    def test_non_owner_is_rejected_for_moderator_station_connection(self):
        with patch.object(ui_service, "verify_id_token", return_value=FakeUser("spectator")), patch.object(
            ui_service, "get_room_info", return_value={"owner_uid": "owner", "mode": "in_person"}
        ):
            client = TestClient(ui_service.app)
            with client.websocket_connect("/ws/test-room", headers={"origin": "http://testserver"}) as websocket:
                websocket.send_json({"type": "AUTH_INIT", "payload": {"id_token": "signed-token"}})
                with self.assertRaises(WebSocketDisconnect) as exc:
                    websocket.receive_json()
                self.assertEqual(exc.exception.code, 4403)

    def test_owner_can_connect_and_send_manual_force_buzz(self):
        moderator = FakeModerator()
        connection = FakeConnection()
        async def fake_connect(websocket, room_id, user_id, role, seat=None, **kwargs):
            await websocket.send_json({"type": "CONNECTED", "payload": {"role": role, "seat": seat}})
            return moderator, connection

        with patch.object(ui_service, "verify_id_token", return_value=FakeUser("owner")), patch.object(
            ui_service, "get_room_info", return_value={"owner_uid": "owner", "mode": "in_person"}
        ), patch.object(ui_service.manager, "connect", side_effect=fake_connect), patch.object(
            ui_service.manager, "disconnect", new=AsyncMock()
        ):
            client = TestClient(ui_service.app)
            with client.websocket_connect("/ws/manual-room", headers={"origin": "http://testserver"}) as websocket:
                websocket.send_json({"type": "AUTH_INIT", "payload": {"id_token": "signed-token"}})
                self.assertEqual(websocket.receive_json()["type"], "CONNECTED")
                websocket.send_json({"type": "BUZZ", "payload": {"team": "Team A", "player": "Manual-A"}})
            event = moderator.inbound_queue.get_nowait()
            self.assertEqual(event.type, "BUZZ")
            self.assertEqual(event.payload, {"team": "Team A", "player": "Manual-A"})
            self.assertEqual(event.user_id, "owner")

    def test_owner_cannot_send_manual_buzz_for_wrong_team(self):
        moderator = FakeModerator()
        connection = FakeConnection()
        async def fake_connect(websocket, room_id, user_id, role, seat=None, **kwargs):
            await websocket.send_json({"type": "CONNECTED", "payload": {"role": role, "seat": seat}})
            return moderator, connection

        with patch.object(ui_service, "verify_id_token", return_value=FakeUser("owner")), patch.object(
            ui_service, "get_room_info", return_value={"owner_uid": "owner", "mode": "in_person"}
        ), patch.object(ui_service.manager, "connect", side_effect=fake_connect), patch.object(
            ui_service.manager, "disconnect", new=AsyncMock()
        ):
            client = TestClient(ui_service.app)
            with client.websocket_connect("/ws/manual-room", headers={"origin": "http://testserver"}) as websocket:
                websocket.send_json({"type": "AUTH_INIT", "payload": {"id_token": "signed-token"}})
                self.assertEqual(websocket.receive_json()["type"], "CONNECTED")
                websocket.send_json({"type": "BUZZ", "payload": {"team": "Team B", "player": "Manual-A"}})
                self.assertEqual(websocket.receive_json()["type"], "ERROR")
                self.assertTrue(moderator.inbound_queue.empty())

    def test_missing_authentication_is_rejected(self):
        client = TestClient(ui_service.app)
        with self.assertRaises(WebSocketDisconnect):
            with client.websocket_connect("/ws/unauth-room", headers={"origin": "http://testserver"}) as websocket:
                websocket.send_json({"type": "PING", "payload": {}})
                websocket.receive_json()

    def test_cross_origin_is_rejected_before_authentication(self):
        client = TestClient(ui_service.app)
        with self.assertRaises(WebSocketDisconnect):
            with client.websocket_connect("/ws/cross-origin", headers={"origin": "https://attacker.invalid"}):
                pass


if __name__ == "__main__":
    unittest.main()
