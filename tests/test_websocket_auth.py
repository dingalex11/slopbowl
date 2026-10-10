import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from services import ui_service


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
    def test_moderator_interface_and_static_client_load(self):
        client = TestClient(ui_service.app)
        page = client.get("/")
        client_script = client.get("/static/js/websocket.js")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Science Bowl", page.text)
        self.assertEqual(client_script.status_code, 200)
        self.assertIn("CONNECTING TO MATCH", client_script.text)
        self.assertIn("btn-online-buzz", page.text)
        self.assertIn("online-player-name", page.text)
        self.assertIn("online-captain-claim", page.text)
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
            cleanup = manager.cleanup_tasks.pop("roster-room", None)
            if cleanup:
                cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)

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

    def test_room_expires_and_closes_viewers_after_moderator_disconnect(self):
        async def verify_expiry():
            manager = ui_service.RoomManager()
            socket = FakeSocket()
            connection = ui_service.RoomConnection(socket, "viewer", "viewer")
            manager.active_connections["round-one"] = [connection]
            marker = object()
            manager.rooms["round-one"] = marker
            manager.room_tasks["round-one"] = []
            with patch.object(ui_service, "ROOM_IDLE_TTL_SECONDS", 0):
                await manager._expire_room("round-one")
            self.assertNotIn("round-one", manager.rooms)
            self.assertEqual(socket.closed, True)

        asyncio.run(verify_expiry())

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
