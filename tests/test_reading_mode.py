"""Moderator-reads-aloud mode: one voice, one read clock for an online room.

The mode exists because an online tossup is otherwise spoken by every player's own
browser. Different operating-system voices take different amounts of time for the same
text, while the server has always treated the moderator's completion as the read's end.
That made the interrupt boundary and the 5-second buzz window the moderator's, not the
room's.

What these tests protect:

* only the moderator can switch the mode, and only the two real values are accepted
* switching it is echoed to the room so every station draws the same state
* a tossup, the buzzer's name and every announcement are sent moderator-only, so player
  devices stay silent in that mode
* the broadcast layer actually withholds those reads from players
* player stations are told when the moderator is speaking, so a silent device reads as
  "listen to the moderator", not as a broken page
* the control exists on the moderator's pre-match screen
"""

import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

import main
import services.ui_service as ui_service
from core.events import Event
from core.validation import validate_client_event
from main import ScienceBowlModerator

ROOT = Path(__file__).resolve().parents[1]
INDEX_SOURCE = (ROOT / "index.html").read_text(encoding="utf-8")
UI_SOURCE = (ROOT / "static/js/ui.js").read_text(encoding="utf-8")
SOCKET_SOURCE = (ROOT / "static/js/websocket.js").read_text(encoding="utf-8")
MAIN_SOURCE = (ROOT / "main.py").read_text(encoding="utf-8")


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)


class ReadingModeValidationTests(unittest.TestCase):
    def test_only_the_two_modes_are_accepted(self):
        for mode in ("stations", "moderator"):
            self.assertEqual(validate_client_event("SET_READING_MODE", {"mode": mode})["mode"], mode)
        for bad in ("everyone", "", None, True, 1):
            with self.assertRaises(ValueError):
                validate_client_event("SET_READING_MODE", {"mode": bad})

    def test_only_the_moderator_may_switch_it(self):
        self.assertIn("SET_READING_MODE", ui_service.OWNER_ONLY_EVENTS)


class ReadingModeServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_default_is_each_device_reading(self):
        moderator = ScienceBowlModerator(use_audio=False)
        self.assertEqual(moderator.reading_mode, "stations")

    async def test_switching_the_mode_is_echoed_to_the_room(self):
        moderator = ScienceBowlModerator(use_audio=False)
        moderator.online_mode = True

        await moderator.process_admin_event(Event(type="SET_READING_MODE", payload={"mode": "moderator"}))

        self.assertEqual(moderator.reading_mode, "moderator")
        first = await asyncio.wait_for(moderator.outbound_queue.get(), 2)
        self.assertEqual(first["type"], "READING_MODE")
        self.assertEqual(first["payload"]["mode"], "moderator")
        status = await asyncio.wait_for(moderator.outbound_queue.get(), 2)
        self.assertEqual(status["type"], "UPDATE_STATUS")
        self.assertIn("player devices stay silent", status["payload"]["text"].lower())

    async def test_the_idle_loop_does_not_drop_the_mode_switch(self):
        # The switch lives on the pre-match screen, where the loop is waiting for a start.
        # If that wait swallowed the event, the moderator could flip a control that did nothing.
        moderator = ScienceBowlModerator(use_audio=False)
        moderator.online_mode = True
        moderator.inbound_queue.put_nowait(Event(type="SET_READING_MODE", payload={"mode": "moderator"}))
        moderator.inbound_queue.put_nowait(Event(type="MANUAL_START_TOSSUP", payload={}))

        action = await asyncio.wait_for(moderator.wait_for_moderator_action(["MANUAL_START_TOSSUP"]), 2)

        self.assertEqual(action.type, "MANUAL_START_TOSSUP")
        self.assertEqual(moderator.reading_mode, "moderator")
        echo = await asyncio.wait_for(moderator.outbound_queue.get(), 2)
        self.assertEqual(echo["type"], "READING_MODE")

    async def test_an_unknown_mode_does_not_change_anything(self):
        moderator = ScienceBowlModerator(use_audio=False)
        moderator.online_mode = True

        await moderator.process_admin_event(Event(type="SET_READING_MODE", payload={"mode": "everyone"}))

        self.assertEqual(moderator.reading_mode, "stations")
        self.assertTrue(moderator.outbound_queue.empty())

    async def test_announcements_and_bonus_reads_are_moderator_only(self):
        moderator = ScienceBowlModerator(use_audio=True)
        moderator.online_mode = True
        moderator.reading_mode = "moderator"

        task = asyncio.create_task(moderator._speak_and_wait("Bonus, name the unit."))
        try:
            message = await asyncio.wait_for(moderator.outbound_queue.get(), 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(message["type"], "START_READING")
        self.assertTrue(message["payload"]["moderator_only"])

    def test_question_reads_carry_the_moderator_only_flag(self):
        # The tossup read and the "who buzzed" read are the two direct START_READING
        # payloads; both must silence player devices in shared mode.
        self.assertEqual(MAIN_SOURCE.count('reading_payload["moderator_only"] = True'), 2)
        self.assertIn('if self.reading_mode == "moderator":', MAIN_SOURCE)

    async def test_stations_mode_never_announces_a_moderator_read(self):
        moderator = ScienceBowlModerator(use_audio=True)
        moderator.online_mode = True

        task = asyncio.create_task(moderator._speak_and_wait("Bonus, name the unit.", announce_kind="bonus"))
        try:
            first = await asyncio.wait_for(moderator.outbound_queue.get(), 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(first["type"], "START_READING")
        self.assertTrue(moderator.outbound_queue.empty())

    async def test_a_shared_bonus_read_is_bracketed_by_the_indicator(self):
        moderator = ScienceBowlModerator(use_audio=True)
        moderator.online_mode = True
        moderator.reading_mode = "moderator"

        with patch.object(main, "READ_EARLY_REPORT_FRACTION", 0.0), patch.object(main, "MIN_READ_SECONDS", 0.0):
            task = asyncio.create_task(moderator._speak_and_wait("Bonus, name the unit.", announce_kind="bonus"))
            try:
                started = await asyncio.wait_for(moderator.outbound_queue.get(), 2)
                self.assertEqual(started["type"], "MODERATOR_READING")
                self.assertTrue(started["payload"]["active"])
                self.assertEqual(started["payload"]["kind"], "bonus")

                read = await asyncio.wait_for(moderator.outbound_queue.get(), 2)
                self.assertEqual(read["type"], "START_READING")
                moderator.inbound_queue.put_nowait(
                    Event(type="READING_DONE", payload={"reading_id": read["payload"]["reading_id"]})
                )

                finished = await asyncio.wait_for(moderator.outbound_queue.get(), 2)
                self.assertEqual(finished["type"], "MODERATOR_READING")
                self.assertFalse(finished["payload"]["active"])
                await asyncio.wait_for(task, 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        self.assertFalse(moderator.moderator_reading["active"])

    async def test_a_shared_tossup_shows_reading_until_the_read_ends(self):
        moderator = ScienceBowlModerator(
            use_audio=True,
            initial_bank=[{"tossup_text": "Name the organelle that makes ATP.", "tossup_answer": "Mitochondria"}],
        )
        moderator.online_mode = True
        moderator.reading_mode = "moderator"

        with patch.object(main, "READ_EARLY_REPORT_FRACTION", 0.0), patch.object(main, "MIN_READ_SECONDS", 0.0):
            task = asyncio.create_task(moderator._handle_tossup_attempt(moderator.question_bank[0]))
            try:
                while True:
                    message = await asyncio.wait_for(moderator.outbound_queue.get(), 2)
                    if message["type"] == "START_READING":
                        break
                    if message["type"] == "MODERATOR_READING":
                        self.assertTrue(message["payload"]["active"])
                        self.assertEqual(message["payload"]["kind"], "tossup")
                self.assertTrue(moderator.moderator_reading["active"])

                moderator.inbound_queue.put_nowait(
                    Event(type="READING_DONE", payload={"reading_id": message["payload"]["reading_id"]})
                )
                finished = await asyncio.wait_for(moderator.outbound_queue.get(), 2)
                self.assertEqual(finished["type"], "MODERATOR_READING")
                self.assertFalse(finished["payload"]["active"])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_an_offline_room_has_no_stations_to_notify(self):
        moderator = ScienceBowlModerator(use_audio=True)
        moderator.reading_mode = "moderator"

        await moderator._set_moderator_reading(True, "tossup")

        self.assertTrue(moderator.outbound_queue.empty())
        self.assertFalse(moderator.moderator_reading["active"])


class ReadingModeBroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def test_players_do_not_receive_a_moderator_only_read(self):
        manager = ui_service.RoomManager()
        owner_socket = FakeSocket()
        player_socket = FakeSocket()
        manager.active_connections["room"] = [
            ui_service.RoomConnection(owner_socket, "owner", "owner"),
            ui_service.RoomConnection(player_socket, "player", "player"),
        ]
        manager.room_modes["room"] = "online"

        await manager.broadcast_to_room("room", {
            "type": "START_READING",
            "payload": {"text": "Tossup", "moderator_only": True},
        })

        self.assertEqual(len(owner_socket.sent), 1)
        self.assertNotIn("moderator_only", owner_socket.sent[0]["payload"])
        self.assertEqual(player_socket.sent, [])

    async def test_the_reading_indicator_reaches_players_and_moderator(self):
        manager = ui_service.RoomManager()
        owner_socket = FakeSocket()
        player_socket = FakeSocket()
        manager.active_connections["room"] = [
            ui_service.RoomConnection(owner_socket, "owner", "owner"),
            ui_service.RoomConnection(player_socket, "player", "player"),
        ]
        manager.room_modes["room"] = "online"

        await manager.broadcast_to_room("room", {"type": "MODERATOR_READING", "payload": {"active": True, "kind": "tossup"}})

        for socket in (owner_socket, player_socket):
            self.assertEqual(len(socket.sent), 1)
            self.assertEqual(socket.sent[0]["type"], "MODERATOR_READING")
            self.assertTrue(socket.sent[0]["payload"]["active"])


class ReadingModeScreenTests(unittest.TestCase):
    def test_the_moderator_screen_has_the_switch(self):
        self.assertIn('id="reading-mode-stations"', INDEX_SOURCE)
        self.assertIn('id="reading-mode-moderator"', INDEX_SOURCE)
        self.assertIn("setReadingMode('moderator')", INDEX_SOURCE)
        self.assertIn("function setReadingMode(mode)", UI_SOURCE)
        self.assertIn("sendEvent('SET_READING_MODE', { mode });", UI_SOURCE)

    def test_every_station_draws_the_server_choice(self):
        self.assertIn("else if (data.type === 'READING_MODE')", SOCKET_SOURCE)
        self.assertIn("renderReadingMode(currentReadingMode)", SOCKET_SOURCE)
        self.assertIn("currentReadingMode = payload.reading_mode || 'stations';", SOCKET_SOURCE)

    def test_the_server_tells_late_joiners_the_mode_and_the_live_read(self):
        service_source = (ROOT / "services/ui_service.py").read_text(encoding="utf-8")
        self.assertIn('"reading_mode": getattr(moderator, "reading_mode", "stations")', service_source)
        self.assertIn('"moderator_reading": dict(getattr(moderator, "moderator_reading"', service_source)
        self.assertIn('"MODERATOR_READING"', service_source)

    def test_the_player_station_has_a_reading_banner(self):
        self.assertIn('id="player-read-banner"', INDEX_SOURCE)
        self.assertIn('id="player-read-banner-text"', INDEX_SOURCE)
        self.assertIn("MODERATOR IS READING", SOCKET_SOURCE)
        self.assertIn("function renderModeratorReading(active, kind = null)", SOCKET_SOURCE)

    def test_the_banner_follows_the_server_and_clears_for_an_answer(self):
        self.assertIn("else if (data.type === 'MODERATOR_READING')", SOCKET_SOURCE)
        self.assertIn("renderModeratorReading(payload.active, payload.kind);", SOCKET_SOURCE)
        self.assertIn("renderModeratorReading(payload.moderator_reading?.active, payload.moderator_reading?.kind);", SOCKET_SOURCE)
        self.assertEqual(SOCKET_SOURCE.count("renderModeratorReading(false);"), 2)


if __name__ == "__main__":
    unittest.main()
