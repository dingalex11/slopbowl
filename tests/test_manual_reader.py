"""In-person manual-reader mode.

A human reads the packet. The moderator clicks the named player who buzzed,
marks the word where the buzz happened, then judges the answer. The server no
longer waits on speech, a camera, a microphone or an AI judge in this mode.

What these tests protect:

* manual mode creates no vision, speech or judge path
* a word inside the question is a NEG (−4); Full read / after end is 0
* a buzz after the read is automatically classified as Full read
* the named seat, not just the legacy Manual-A/B buttons, identifies the team
* the 5-second window and the 20-second bonus confer clock are emitted, and a
  missed buzz window ends the tossup
* the active page and script order carry the manual HUD, not the camera setup
"""

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import main
import services.ui_service as ui_service
from core.events import Event
from core.validation import validate_client_event
from main import ScienceBowlModerator

ROOT = Path(__file__).resolve().parents[1]
INDEX_SOURCE = (ROOT / "index.html").read_text(encoding="utf-8")
MANUAL_SOURCE = (ROOT / "static/js/manual_reader.js").read_text(encoding="utf-8")
STYLE_SOURCE = (ROOT / "static/css/style.css").read_text(encoding="utf-8")
SOCKET_SOURCE = (ROOT / "static/js/websocket.js").read_text(encoding="utf-8")
UI_SOURCE = (ROOT / "static/js/ui.js").read_text(encoding="utf-8")

TOSSUP = {
    "tossup_text": "What is the capital of France?",
    "tossup_answer": "Paris",
    "tossup_options": ["Paris", "Lyon"],
    "category": "GEOGRAPHY",
}
BONUS = {
    "bonus_text": "Name the river that flows through Paris.",
    "bonus_answer": "The Seine",
    "bonus_options": ["The Seine", "The Loire"],
    "category": "GEOGRAPHY",
}


async def next_message(moderator, wanted_type, timeout=2.0):
    """Read outbound messages until the requested type arrives."""
    while True:
        message = await asyncio.wait_for(moderator.outbound_queue.get(), timeout)
        if message.get("type") == wanted_type:
            return message


class ManualReaderValidationTests(unittest.TestCase):
    def test_manual_read_done_is_a_client_event(self):
        from core.validation import EVENT_TYPES

        self.assertIn("MANUAL_READ_DONE", EVENT_TYPES)
        self.assertIn("MANUAL_READ_DONE", ui_service.OWNER_ONLY_EVENTS)

    def test_override_and_close_are_owner_only(self):
        self.assertIn("OVERRIDE_LAST_QUESTION", ui_service.OWNER_ONLY_EVENTS)
        self.assertIn("CLOSE_MATCH", ui_service.OWNER_ONLY_EVENTS)

    def test_buzzpoint_accepts_the_after_read_flag(self):
        self.assertEqual(
            validate_client_event("LOG_BUZZPOINT", {"buzzpoint": "capital", "after_read": False}),
            {"buzzpoint": "capital", "after_read": False},
        )
        with self.assertRaises(ValueError):
            validate_client_event("LOG_BUZZPOINT", {"buzzpoint": "capital", "after_read": "yes"})


class ManualReaderServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_mode_has_no_vision_speech_or_judge(self):
        moderator = ScienceBowlModerator(use_audio=True, use_camera=True, manual_reader=True)
        self.assertTrue(moderator.manual_reader)
        self.assertIsNone(moderator.vision)
        self.assertIsNone(moderator.judge)

    async def test_word_inside_the_question_is_a_neg(self):
        moderator = ScienceBowlModerator(use_audio=False, manual_reader=True)
        task = asyncio.create_task(moderator._handle_manual_tossup_attempt(TOSSUP))

        question = await next_message(moderator, "UPDATE_QUESTION")
        self.assertTrue(question["payload"]["manual"])
        self.assertEqual(question["payload"]["raw_text"], TOSSUP["tossup_text"])
        reading = await next_message(moderator, "MANUAL_PHASE")
        self.assertEqual(reading["payload"]["phase"], "READING")

        moderator.inbound_queue.put_nowait(Event(type="BUZZ", payload={"team": "Team A", "player": "A-C"}, role="owner"))
        buzz = await next_message(moderator, "BUZZ")
        self.assertEqual(buzz["payload"]["player"], "A-C")
        answering = await next_message(moderator, "MANUAL_PHASE")
        self.assertEqual(answering["payload"]["phase"], "ANSWERING")
        self.assertEqual(answering["payload"]["seconds"], 4.0)

        moderator.inbound_queue.put_nowait(
            Event(type="LOG_BUZZPOINT", payload={"buzzpoint": "capital", "after_read": False}, role="owner")
        )
        marked = await next_message(moderator, "BUZZPOINT_SET")
        self.assertEqual(marked["payload"]["buzzpoint"], "capital")
        self.assertEqual(marked["payload"]["points_if_wrong"], -4)

        moderator.inbound_queue.put_nowait(Event(type="FORCE_JUDGMENT", payload={"correct": False}, role="owner"))
        payload, correct, interrupted, action, _buzz_time, buzz_text = await asyncio.wait_for(task, 2)

        self.assertEqual(payload["team"], "Team A")
        self.assertFalse(correct)
        self.assertTrue(interrupted)
        self.assertFalse(action)
        self.assertEqual(buzz_text, "capital")
        self.assertTrue(moderator.state.is_interrupted)
        self.assertEqual(moderator.state.apply_tossup_points(False)["penalty_points"], -4)

    async def test_full_read_after_the_question_is_zero_not_a_neg(self):
        moderator = ScienceBowlModerator(use_audio=False, manual_reader=True)
        task = asyncio.create_task(moderator._handle_manual_tossup_attempt(TOSSUP))

        await next_message(moderator, "UPDATE_QUESTION")
        await next_message(moderator, "MANUAL_PHASE")

        moderator.inbound_queue.put_nowait(Event(type="MANUAL_READ_DONE", payload={}, role="owner"))
        window = await next_message(moderator, "MANUAL_PHASE")
        self.assertEqual(window["payload"]["phase"], "BUZZ_WINDOW")
        self.assertEqual(window["payload"]["seconds"], 5.0)

        moderator.inbound_queue.put_nowait(Event(type="BUZZ", payload={"team": "Team B", "player": "B-1"}, role="owner"))
        buzz = await next_message(moderator, "BUZZ")
        self.assertTrue(buzz["payload"]["after_read"])
        answering = await next_message(moderator, "MANUAL_PHASE")
        self.assertEqual(answering["payload"]["buzzpoint"], "Full Read")
        self.assertTrue(answering["payload"]["after_read"])

        moderator.inbound_queue.put_nowait(Event(type="FORCE_JUDGMENT", payload={"correct": False}, role="owner"))
        _payload, correct, interrupted, _action, _buzz_time, buzz_text = await asyncio.wait_for(task, 2)

        self.assertFalse(correct)
        self.assertFalse(interrupted)
        self.assertEqual(buzz_text, "Full Read")
        self.assertFalse(moderator.state.is_interrupted)
        self.assertEqual(moderator.state.apply_tossup_points(False)["penalty_points"], 0)

    async def test_full_read_marker_flips_a_word_mark_to_zero(self):
        moderator = ScienceBowlModerator(use_audio=False, manual_reader=True)
        task = asyncio.create_task(moderator._handle_manual_tossup_attempt(TOSSUP))

        await next_message(moderator, "UPDATE_QUESTION")
        await next_message(moderator, "MANUAL_PHASE")
        moderator.inbound_queue.put_nowait(Event(type="BUZZ", payload={"team": "Team A", "player": "A-1"}, role="owner"))
        await next_message(moderator, "BUZZ")
        await next_message(moderator, "MANUAL_PHASE")

        moderator.inbound_queue.put_nowait(
            Event(type="LOG_BUZZPOINT", payload={"buzzpoint": "capital", "after_read": False}, role="owner")
        )
        first = await next_message(moderator, "BUZZPOINT_SET")
        self.assertEqual(first["payload"]["points_if_wrong"], -4)

        moderator.inbound_queue.put_nowait(
            Event(type="LOG_BUZZPOINT", payload={"buzzpoint": "Full Read", "after_read": True}, role="owner")
        )
        second = await next_message(moderator, "BUZZPOINT_SET")
        self.assertEqual(second["payload"]["buzzpoint"], "Full Read")
        self.assertEqual(second["payload"]["points_if_wrong"], 0)

        moderator.inbound_queue.put_nowait(Event(type="FORCE_JUDGMENT", payload={"correct": False}, role="owner"))
        _payload, _correct, interrupted, _action, _buzz_time, buzz_text = await asyncio.wait_for(task, 2)
        self.assertFalse(interrupted)
        self.assertEqual(buzz_text, "Full Read")

    async def test_five_second_buzz_window_times_out_to_a_dead_tossup(self):
        moderator = ScienceBowlModerator(use_audio=False, manual_reader=True)
        task = asyncio.create_task(moderator._handle_manual_tossup_attempt(TOSSUP, skip_read=True))

        await next_message(moderator, "UPDATE_QUESTION")
        window = await next_message(moderator, "MANUAL_PHASE")
        self.assertEqual(window["payload"]["phase"], "BUZZ_WINDOW")

        result = await asyncio.wait_for(task, 7)
        self.assertEqual(result, (None, False, False, False, 0.0, ""))
        idle = await next_message(moderator, "MANUAL_PHASE")
        self.assertEqual(idle["payload"]["phase"], "IDLE")

    async def test_bonus_read_starts_the_twenty_second_confer_clock(self):
        moderator = ScienceBowlModerator(use_audio=False, manual_reader=True)
        task = asyncio.create_task(moderator._handle_manual_bonus(BONUS, "Team A"))

        question = await next_message(moderator, "UPDATE_QUESTION")
        self.assertEqual(question["payload"]["phase"], "BONUS")
        read = await next_message(moderator, "MANUAL_PHASE")
        self.assertEqual(read["payload"]["phase"], "BONUS_READ")

        moderator.inbound_queue.put_nowait(Event(type="MANUAL_READ_DONE", payload={}, role="owner"))
        confer = await next_message(moderator, "MANUAL_PHASE")
        self.assertEqual(confer["payload"]["phase"], "BONUS_CONFER")
        self.assertEqual(confer["payload"]["seconds"], 20.0)

        moderator.inbound_queue.put_nowait(Event(type="FORCE_JUDGMENT", payload={"correct": True}, role="owner"))
        correct, action = await asyncio.wait_for(task, 2)
        self.assertTrue(correct)
        self.assertFalse(action)

    async def test_loading_a_packet_rearms_the_manual_start_control(self):
        moderator = ScienceBowlModerator(use_audio=False, manual_reader=True)
        moderator.state.transition_to(main.GameState.IDLE)

        await moderator.process_admin_event(Event(type="LOAD_BANK", payload={"bank": [TOSSUP]}))

        while True:
            message = await asyncio.wait_for(moderator.outbound_queue.get(), 2)
            if message["type"] == "UI_STATE":
                break
        self.assertEqual(message["payload"]["show"], "TOSSUP")


class ManualReaderSeatTests(unittest.TestCase):
    class FakeModerator:
        def __init__(self):
            self.inbound_queue = asyncio.Queue()
            self.manual_phase = {"phase": "ANSWERING", "seconds": 4.0, "started_at": 0}

    class FakeConnection:
        def __init__(self):
            self.role = "owner"
            self.user_id = "owner"
            self.team = None
            self.player_name = None
            self.captain = False

        def allow_message(self, event_type):
            return True

    def test_named_seat_buzz_is_accepted_for_the_matching_team(self):
        moderator = self.FakeModerator()
        connection = self.FakeConnection()

        async def fake_connect(websocket, room_id, user_id, role, seat=None, **kwargs):
            await websocket.send_json({
                "type": "CONNECTED",
                "payload": {"role": role, "mode": "in_person", "manual_reader": True, "manual_phase": moderator.manual_phase},
            })
            return moderator, connection

        with patch.object(ui_service, "verify_id_token", return_value=type("U", (), {"uid": "owner"})()), patch.object(
            ui_service, "get_room_info", return_value={"owner_uid": "owner", "mode": "in_person"}
        ), patch.object(ui_service.manager, "connect", side_effect=fake_connect), patch.object(
            ui_service.manager, "disconnect", new=AsyncMock()
        ):
            client = TestClient(ui_service.app)
            with client.websocket_connect("/ws/manual-seats", headers={"origin": "http://testserver"}) as websocket:
                websocket.send_json({"type": "AUTH_INIT", "payload": {"id_token": "signed-token"}})
                connected = websocket.receive_json()
                self.assertEqual(connected["type"], "CONNECTED")
                self.assertTrue(connected["payload"]["manual_reader"])
                websocket.send_json({"type": "BUZZ", "payload": {"team": "Team A", "player": "A-C"}})

        event = moderator.inbound_queue.get_nowait()
        self.assertEqual(event.type, "BUZZ")
        self.assertEqual(event.payload, {"team": "Team A", "player": "A-C"})


class ManualReaderPageTests(unittest.TestCase):
    def test_manual_reader_loads_after_ui_and_before_the_socket(self):
        manual_at = INDEX_SOURCE.index("static/js/manual_reader.js")
        self.assertLess(INDEX_SOURCE.index("static/js/ui.js"), manual_at)
        self.assertLess(manual_at, INDEX_SOURCE.index("static/js/websocket.js"))

    def test_the_camera_setup_and_camera_script_are_not_active(self):
        self.assertNotIn("static/js/camera.js", INDEX_SOURCE)
        self.assertNotIn("match-video-stream", INDEX_SOURCE)
        self.assertNotIn("camera-select", INDEX_SOURCE)
        self.assertNotIn("Vision Detection Engine", INDEX_SOURCE)

    def test_the_hud_carries_the_manual_controls(self):
        for element in (
            'id="manual-reader-panel"',
            'id="reader-question-text"',
            'id="reader-options"',
            'id="reader-answer-key"',
            'id="manual-team-a-buzzers"',
            'id="manual-team-b-buzzers"',
            'id="btn-end-buzzpoint"',
            'id="btn-manual-correct"',
            'id="btn-manual-incorrect"',
            'id="btn-manual-question-read"',
            'id="btn-manual-bonus-read"',
            'id="btn-start-tossup"',
            'id="btn-start-bonus"',
            'id="btn-override-last-question"',
            'id="btn-end-match"',
        ):
            self.assertIn(element, INDEX_SOURCE)
        self.assertNotIn('id="manual-buzzpoint-panel"', INDEX_SOURCE)
        self.assertNotIn('id="btn-full-read"', INDEX_SOURCE)
        self.assertIn("ManualReader.markEndBuzzpoint()", INDEX_SOURCE)
        self.assertIn("markEndBuzzpoint: markFullRead", MANUAL_SOURCE)
        self.assertNotIn('id="btn-pause-match"', INDEX_SOURCE)
        self.assertNotIn('id="btn-export-csv"', INDEX_SOURCE)

    def test_packet_words_are_text_not_markup(self):
        self.assertIn("document.createElement('button')", MANUAL_SOURCE)
        self.assertIn("token.textContent = part;", MANUAL_SOURCE)
        self.assertNotIn("innerHTML", MANUAL_SOURCE)
        self.assertIn("sendEvent('LOG_BUZZPOINT', { buzzpoint: value, after_read: false });", MANUAL_SOURCE)
        self.assertIn("sendEvent('LOG_BUZZPOINT', { buzzpoint: 'Full Read', after_read: true });", MANUAL_SOURCE)

    def test_answer_choices_are_word_level_clickable_text(self):
        """A buzz inside a choice marks the exact word, not the whole choice."""
        self.assertIn("appendWordTokens(line, option, 'reader-option-word');", MANUAL_SOURCE)
        self.assertIn("line.className = 'reader-option-line';", MANUAL_SOURCE)
        self.assertNotIn("reader-option-letter", MANUAL_SOURCE)
        self.assertIn("token.dataset.word = part;", MANUAL_SOURCE)
        self.assertIn("sendEvent('LOG_BUZZPOINT', { buzzpoint: value, after_read: false });", MANUAL_SOURCE)
        self.assertNotIn("reader-option bg-panel", MANUAL_SOURCE)
        self.assertNotIn("markOption", MANUAL_SOURCE)
        self.assertNotIn("selectedOption", MANUAL_SOURCE)
        self.assertIn(".reader-option-line", STYLE_SOURCE)
        self.assertIn(".reader-option-word", STYLE_SOURCE)
        self.assertNotIn(".reader-option.is-selected", STYLE_SOURCE)

    def test_scoresheet_rebuilds_from_the_authoritative_log(self):
        self.assertIn("function renderScoresheetFromLog(log)", SOCKET_SOURCE)
        self.assertIn("currentMatchLog = Array.isArray(payload.log) ? payload.log : [];", SOCKET_SOURCE)
        self.assertIn("renderScoresheetFromLog(currentMatchLog);", SOCKET_SOURCE)
        self.assertIn("resetMatchHud();", SOCKET_SOURCE)

    def test_end_match_and_override_use_the_owner_controls(self):
        self.assertIn("onclick=\"requestEndMatch()\"", INDEX_SOURCE)
        self.assertIn("onclick=\"requestOverrideLastQuestion()\"", INDEX_SOURCE)
        self.assertIn("function requestEndMatch()", UI_SOURCE)
        self.assertIn("function requestOverrideLastQuestion()", UI_SOURCE)
        self.assertIn("sendEvent('OVERRIDE_LAST_QUESTION')", UI_SOURCE)
        self.assertIn("sendEvent('CLOSE_MATCH');", UI_SOURCE)

    def test_the_final_screen_confirms_before_deleting_the_room(self):
        self.assertIn('id="match-close-confirm"', INDEX_SOURCE)
        self.assertIn('id="btn-confirm-close-match"', INDEX_SOURCE)
        self.assertIn("function requestMatchRoomClose()", UI_SOURCE)
        self.assertIn("function resetMatchAfterRoomClose()", UI_SOURCE)
        self.assertIn("Close this room and start over?", UI_SOURCE)
        self.assertIn("currentMatchTeardown", SOCKET_SOURCE)
        self.assertIn("4411: 'MATCH ENDED - ROOM CLOSED'", SOCKET_SOURCE)

    def test_repeated_word_marks_only_the_clicked_occurrence(self):
        """Selecting the second "the" must not light up every "the" in the question."""
        self.assertIn("let selectedToken = null;", MANUAL_SOURCE)
        self.assertIn("selectedToken = element;", MANUAL_SOURCE)
        self.assertIn("token.classList.toggle('is-selected', token === selectedToken);", MANUAL_SOURCE)
        self.assertNotIn("token === element || token.dataset.word === word", MANUAL_SOURCE)
        self.assertNotIn("token.dataset.word === selectedBuzzpoint", MANUAL_SOURCE)

    def test_the_socket_dispatches_manual_phase_and_buzzpoint(self):
        self.assertIn("data.type === 'MANUAL_PHASE'", SOCKET_SOURCE)
        self.assertIn("data.type === 'BUZZPOINT_SET'", SOCKET_SOURCE)
        self.assertIn("window.ManualReader.beginBuzz(payload);", SOCKET_SOURCE)
        self.assertIn("window.ManualReader.onConnected(payload);", SOCKET_SOURCE)
        self.assertIn("data.type === 'DOWNLOAD_CSV'", SOCKET_SOURCE)
        self.assertIn("link.download = 'slopbowl_match_log.csv';", SOCKET_SOURCE)

    def test_bonus_verdicts_do_not_need_a_buzzpoint(self):
        self.assertIn("function isBonusPhase()", MANUAL_SOURCE)
        self.assertIn("if (!isBonusPhase() && (!buzzActive || !selectedBuzzpoint)) return;", MANUAL_SOURCE)
        self.assertIn("setVerdictEnabled(!verdictSent);", MANUAL_SOURCE)
        self.assertIn("setVerdictLabels(true);", MANUAL_SOURCE)

    def test_start_match_creates_a_server_room_and_sends_the_staged_packet(self):
        self.assertIn("async function startInPersonMatch()", UI_SOURCE)
        self.assertIn("const room = await window.createInPersonRoomApi();", UI_SOURCE)
        self.assertNotIn("ensureRoomOwnership(roomId", UI_SOURCE)
        self.assertIn("connectWebSocket(roomId", UI_SOURCE)
        self.assertIn("sendEvent('LOAD_BANK', { bank: window.pendingBank });", UI_SOURCE)
        self.assertIn("sendEvent('SAVE_ROSTER', rosterData);", UI_SOURCE)


class ManualReaderLogTests(unittest.TestCase):
    """The log's CSV shape is unchanged: the buzzpoint column holds the marker."""

    def test_the_log_stores_the_word_or_full_read(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("self.manual_buzzpoint = \"Full Read\"", source)
        self.assertIn("buzz_text", source)
        self.assertNotIn("Buzzpoint,", source)  # no CSV header rewrite in this change


if __name__ == "__main__":
    unittest.main()
