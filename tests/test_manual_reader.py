"""In-person manual-reader mode.

A human reads the packet. The moderator clicks the named player who buzzed,
marks the word where the buzz happened, then judges the answer. The server no
longer waits on speech, a microphone or an AI judge in this mode, and camera
buzz stays off until the moderator explicitly enables it.

What these tests protect:

* manual mode creates no vision, speech or judge path until camera buzz is opted in
* a word inside the question is a NEG (−4); Full read / after end is 0
* Dead ends a no-buzz toss-up; End marks a live buzz as Full read
* the named seat, not just the legacy Manual-A/B buttons, identifies the team
* the rebound-only 5-second window and the 20-second bonus confer clock are emitted
* the active page and script order carry the manual HUD and the optional camera panel
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
CAMERA_SOURCE = (ROOT / "static/js/camera.js").read_text(encoding="utf-8")

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

    def test_dead_and_camera_controls_are_owner_only_client_events(self):
        from core.validation import EVENT_TYPES

        for event_type in ("DEAD_TOSSUP", "SET_CAMERA_BUZZ"):
            self.assertIn(event_type, EVENT_TYPES)
            self.assertIn(event_type, ui_service.OWNER_ONLY_EVENTS)
        self.assertIn("CAMERA_BUZZ_STATE", ui_service.OWNER_ONLY_OUTPUTS)
        self.assertEqual(validate_client_event("DEAD_TOSSUP", {}), {})
        with self.assertRaises(ValueError):
            validate_client_event("DEAD_TOSSUP", {"force": True})
        self.assertEqual(validate_client_event("SET_CAMERA_BUZZ", {"enabled": True}), {"enabled": True})
        for bad in ({"enabled": "yes"}, {}, {"enabled": True, "zones": 1}):
            with self.assertRaises(ValueError):
                validate_client_event("SET_CAMERA_BUZZ", bad)

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
    async def test_manual_mode_keeps_camera_speech_and_judge_off_until_opt_in(self):
        moderator = ScienceBowlModerator(use_audio=True, use_camera=True, manual_reader=True)
        self.assertTrue(moderator.manual_reader)
        self.assertIsNone(moderator.vision)
        self.assertFalse(moderator.camera_buzz_enabled)
        self.assertIsNone(moderator.judge)

        await moderator._set_camera_buzz(True)
        state = await next_message(moderator, "CAMERA_BUZZ_STATE")
        self.assertTrue(state["payload"]["enabled"])
        self.assertIsNotNone(moderator.vision)

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

    async def test_end_after_a_buzz_is_full_read_zero_not_a_neg(self):
        moderator = ScienceBowlModerator(use_audio=False, manual_reader=True)
        task = asyncio.create_task(moderator._handle_manual_tossup_attempt(TOSSUP))

        await next_message(moderator, "UPDATE_QUESTION")
        await next_message(moderator, "MANUAL_PHASE")

        moderator.inbound_queue.put_nowait(Event(type="BUZZ", payload={"team": "Team B", "player": "B-1"}, role="owner"))
        buzz = await next_message(moderator, "BUZZ")
        self.assertEqual(buzz["payload"]["source"], "manual")
        self.assertFalse(buzz["payload"]["after_read"])
        await next_message(moderator, "MANUAL_PHASE")

        moderator.inbound_queue.put_nowait(
            Event(type="LOG_BUZZPOINT", payload={"buzzpoint": "Full Read", "after_read": True}, role="owner")
        )
        marked = await next_message(moderator, "BUZZPOINT_SET")
        self.assertEqual(marked["payload"]["buzzpoint"], "Full Read")
        self.assertEqual(marked["payload"]["points_if_wrong"], 0)

        moderator.inbound_queue.put_nowait(Event(type="FORCE_JUDGMENT", payload={"correct": False}, role="owner"))
        _payload, correct, interrupted, _action, _buzz_time, buzz_text = await asyncio.wait_for(task, 2)

        self.assertFalse(correct)
        self.assertFalse(interrupted)
        self.assertEqual(buzz_text, "Full Read")
        self.assertFalse(moderator.state.is_interrupted)
        self.assertEqual(moderator.state.apply_tossup_points(False)["penalty_points"], 0)

    async def test_a_normal_tossup_never_enters_a_buzz_window(self):
        moderator = ScienceBowlModerator(use_audio=False, manual_reader=True)
        task = asyncio.create_task(moderator._handle_manual_tossup_attempt(TOSSUP))

        await next_message(moderator, "UPDATE_QUESTION")
        reading = await next_message(moderator, "MANUAL_PHASE")
        self.assertEqual(reading["payload"]["phase"], "READING")
        await asyncio.sleep(0.4)

        seen = []
        while not moderator.outbound_queue.empty():
            seen.append(moderator.outbound_queue.get_nowait())
        phases = [m["payload"]["phase"] for m in seen if m.get("type") == "MANUAL_PHASE"]
        self.assertNotIn("BUZZ_WINDOW", phases)

        moderator.inbound_queue.put_nowait(Event(type="DEAD_TOSSUP", payload={}, role="owner"))
        await asyncio.wait_for(task, 2)

    async def test_dead_ends_a_no_buzz_tossup(self):
        moderator = ScienceBowlModerator(use_audio=False, manual_reader=True)
        task = asyncio.create_task(moderator._handle_manual_tossup_attempt(TOSSUP))

        await next_message(moderator, "UPDATE_QUESTION")
        reading = await next_message(moderator, "MANUAL_PHASE")
        self.assertEqual(reading["payload"]["phase"], "READING")

        moderator.inbound_queue.put_nowait(Event(type="DEAD_TOSSUP", payload={}, role="owner"))
        result = await asyncio.wait_for(task, 2)
        self.assertEqual(result, (None, False, False, False, 0.0, ""))
        idle = await next_message(moderator, "MANUAL_PHASE")
        self.assertEqual(idle["payload"]["phase"], "IDLE")

    async def test_camera_detection_arms_only_in_live_tossup_windows(self):
        moderator = ScienceBowlModerator(use_audio=False, manual_reader=True)
        await moderator._set_camera_buzz(True)
        await next_message(moderator, "CAMERA_BUZZ_STATE")
        vision = moderator.vision
        self.assertIsNotNone(vision)

        await moderator._set_manual_phase("READING")
        self.assertTrue(vision.armed)
        await moderator._set_manual_phase("ANSWERING")
        self.assertFalse(vision.armed)
        await moderator._set_manual_phase("BONUS_READ")
        self.assertFalse(vision.armed)
        await moderator._set_manual_phase("BUZZ_WINDOW", 5.0)
        self.assertTrue(vision.armed)
        await moderator._set_manual_phase("IDLE")
        self.assertFalse(vision.armed)

    async def test_camera_frames_only_emit_while_armed(self):
        import numpy as np

        moderator = ScienceBowlModerator(use_audio=False, manual_reader=True)
        await moderator._set_camera_buzz(True)
        await next_message(moderator, "CAMERA_BUZZ_STATE")
        vision = moderator.vision
        vision.update_rois({"A-1": [0.0, 0.0, 1.0, 1.0]})

        dark = np.zeros((360, 640, 3), dtype=np.uint8)
        bright = np.full_like(dark, 255)
        vision.set_armed(False)
        await vision.process_frame(dark, 1.0)
        await vision.process_frame(bright, 1.1)
        self.assertTrue(moderator.inbound_queue.empty())

        vision.set_armed(True)
        await vision.process_frame(dark, 2.0)
        self.assertTrue(moderator.inbound_queue.empty())
        await vision.process_frame(bright, 2.1)
        event = await asyncio.wait_for(moderator.inbound_queue.get(), 1)
        self.assertEqual(event.type, "BUZZ")
        self.assertEqual(event.payload["source"], "camera")

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

    async def test_the_staged_question_is_preview_only_until_start_tossup(self):
        moderator = ScienceBowlModerator(use_audio=False, manual_reader=True, initial_bank=[TOSSUP])
        task = asyncio.create_task(moderator.run_game_loop())
        try:
            preload = None
            while preload is None:
                message = await asyncio.wait_for(moderator.outbound_queue.get(), 4)
                if message["type"] == "UPDATE_QUESTION":
                    preload = message
            self.assertTrue(preload["payload"]["preview"])
            self.assertEqual(preload["payload"]["raw_text"], TOSSUP["tossup_text"])
        finally:
            task.cancel()
            await task


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

    def test_the_camera_setup_is_opt_in_and_loads_after_the_socket(self):
        self.assertIn("static/js/camera.js", INDEX_SOURCE)
        self.assertLess(
            INDEX_SOURCE.index("static/js/websocket.js"),
            INDEX_SOURCE.index("static/js/camera.js"),
        )
        for element in ('id="video-stream"', 'id="calib-canvas"', 'id="camera-select"', 'id="camera-panel-body"'):
            self.assertIn(element, INDEX_SOURCE)
        self.assertIn("Enable camera buzz", INDEX_SOURCE)
        self.assertNotIn("match-video-stream", INDEX_SOURCE)
        self.assertNotIn("Vision Detection Engine", INDEX_SOURCE)
        # Opt-in only: no load-time camera probe, prompt or stream.
        self.assertNotIn("window.addEventListener('load'", CAMERA_SOURCE)
        self.assertNotIn("loadCameras", CAMERA_SOURCE)
        self.assertIn("window.CameraBuzz = {", CAMERA_SOURCE)
        self.assertIn("startForMatch: startForMatch", CAMERA_SOURCE)
        self.assertIn("window.CameraBuzz.startForMatch()", UI_SOURCE)

    def test_the_hud_carries_the_manual_controls(self):
        for element in (
            'id="manual-reader-panel"',
            'id="reader-question-text"',
            'id="reader-options"',
            'id="reader-answer-key"',
            'id="manual-team-a-buzzers"',
            'id="manual-team-b-buzzers"',
            'id="btn-end-buzzpoint"',
            'id="btn-dead-tossup"',
            'id="btn-hud-camera"',
            'id="buzz-source-badge"',
            'id="manual-clock-wrap"',
            'id="action-timer-label"',
            'id="action-timer-box"',
            'id="action-timer-display"',
            'id="action-timer-bar"',
            'id="action-timer-warning"',
            'id="btn-manual-correct"',
            'id="btn-manual-incorrect"',
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
        self.assertIn("ManualReader.deadTossup()", INDEX_SOURCE)
        self.assertIn("markEndBuzzpoint: markFullRead", MANUAL_SOURCE)
        self.assertIn("deadTossup: deadTossup", MANUAL_SOURCE)
        self.assertIn("sendEvent('DEAD_TOSSUP', {})", MANUAL_SOURCE)
        self.assertNotIn('id="btn-manual-question-read"', INDEX_SOURCE)
        self.assertNotIn('id="btn-pause-match"', INDEX_SOURCE)
        self.assertNotIn('id="btn-export-csv"', INDEX_SOURCE)

    def test_dead_and_end_live_under_the_question_and_clock_above_the_scoresheet(self):
        options_at = INDEX_SOURCE.index('id="reader-options"')
        end_at = INDEX_SOURCE.index('id="btn-end-buzzpoint"')
        answer_at = INDEX_SOURCE.index('class="match-answer-strip"')
        self.assertLess(options_at, end_at)
        self.assertLess(end_at, answer_at)
        dead_at = INDEX_SOURCE.index('id="btn-dead-tossup"')
        self.assertLess(options_at, dead_at)
        self.assertLess(dead_at, answer_at)

        banner = INDEX_SOURCE[
            INDEX_SOURCE.index('<div class="match-buzz-banner">')
            :INDEX_SOURCE.index('<div class="match-verdict-actions">')
        ]
        self.assertNotIn('btn-end-buzzpoint', banner)
        self.assertNotIn('btn-dead-tossup', banner)

        clock_at = INDEX_SOURCE.index('id="manual-clock-wrap"')
        self.assertLess(INDEX_SOURCE.index('id="match-layout"'), clock_at)
        self.assertLess(clock_at, INDEX_SOURCE.index('class="match-scoreboard"'))

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

    def test_the_staged_question_is_hidden_until_start_tossup(self):
        self.assertIn("if (data && data.preview) {", MANUAL_SOURCE)
        self.assertIn("renderQuestionPreview(data);", MANUAL_SOURCE)
        self.assertIn("Question loaded. Click Start Tossup to reveal it.", MANUAL_SOURCE)
        main_source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('"preview": True,', main_source)

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

    def test_the_socket_dispatches_manual_phase_buzzpoint_and_camera_state(self):
        self.assertIn("data.type === 'MANUAL_PHASE'", SOCKET_SOURCE)
        self.assertIn("data.type === 'BUZZPOINT_SET'", SOCKET_SOURCE)
        self.assertIn("data.type === 'CAMERA_BUZZ_STATE'", SOCKET_SOURCE)
        self.assertIn("window.CameraBuzz.applyState(payload)", SOCKET_SOURCE)
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
        self.assertIn("window.CameraBuzz.startForMatch()", UI_SOURCE)


class ManualReaderLogTests(unittest.TestCase):
    """The log's CSV shape is unchanged: the buzzpoint column holds the marker."""

    def test_the_log_stores_the_word_or_full_read(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("self.manual_buzzpoint = \"Full Read\"", source)
        self.assertIn("buzz_text", source)
        self.assertNotIn("Buzzpoint,", source)  # no CSV header rewrite in this change


if __name__ == "__main__":
    unittest.main()
