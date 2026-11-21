import asyncio
import unittest
from unittest.mock import patch

import main
from core.events import Event
from core.state_machine import GameState
from main import READ_FAILED_WARNING, READ_TIMEOUT_WARNING, ScienceBowlModerator

# The read guard is about how long a question takes to speak. Tests that are not about it
# switch it off so a report is believed the moment it arrives.
NO_READ_GUARD = dict(READ_EARLY_REPORT_FRACTION=0.0, MIN_READ_SECONDS=0.0)


class ModeratorControlTests(unittest.IsolatedAsyncioTestCase):
    async def _next_message(self, moderator, timeout=3.0):
        """Return the next server message produced while the loop is idle."""
        return await asyncio.wait_for(moderator.outbound_queue.get(), timeout)

    async def _next_status(self, moderator, timeout=3.0):
        """Return the first UPDATE_STATUS text produced while the loop is idle."""
        while True:
            message = await self._next_message(moderator, timeout)
            if message.get("type") == "UPDATE_STATUS":
                return message["payload"]["text"]

    async def _next_of_type(self, moderator, message_type, timeout=3.0):
        """Return the next server message of one type."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                self.fail(f"timed out waiting for {message_type}")
            message = await asyncio.wait_for(moderator.outbound_queue.get(), remaining)
            if message.get("type") == message_type:
                return message

    async def test_start_tossup_without_bank_reports_missing_packet(self):
        moderator = ScienceBowlModerator(use_audio=False)
        moderator.inbound_queue.put_nowait(Event(type="MANUAL_START_TOSSUP", payload={}))

        task = asyncio.create_task(moderator.run_game_loop())
        try:
            text = await self._next_status(moderator)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertIn("no question packet", text.lower())

    async def test_start_tossup_after_last_question_reports_match_complete(self):
        moderator = ScienceBowlModerator(
            use_audio=False,
            initial_bank=[{"tossup_text": "Question", "tossup_answer": "Answer"}],
        )
        moderator.current_q_idx = 1  # Bank exhausted (already used by the first tossup).
        moderator.inbound_queue.put_nowait(Event(type="MANUAL_START_TOSSUP", payload={}))

        task = asyncio.create_task(moderator.run_game_loop())
        try:
            text = await self._next_status(moderator)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertIn("complete", text.lower())

    async def test_packet_exhaustion_emits_one_match_complete_summary(self):
        moderator = ScienceBowlModerator(
            use_audio=False,
            initial_bank=[{"tossup_text": "Question", "tossup_answer": "Answer"}],
        )
        moderator.current_q_idx = 1
        moderator.state.team_a_score = 38
        moderator.state.team_b_score = 28
        moderator.match_log = [{
            "Q_Num": 1, "Event_Type": "TOSSUP", "Team": "Team A", "Player": "A-1",
            "Correct": True, "Points": 4, "Buzzpoint": "capital", "Buzz_Time": "3.200",
            "Score_A": 38, "Score_B": 28,
        }]

        task = asyncio.create_task(moderator.run_game_loop())
        try:
            message = await self._next_message(moderator)
            self.assertEqual(message["type"], "MATCH_COMPLETE")
            self.assertEqual(message["payload"]["score_a"], 38)
            self.assertEqual(message["payload"]["score_b"], 28)
            self.assertEqual(message["payload"]["question_count"], 1)
            self.assertEqual(message["payload"]["log"], moderator.match_log)
            self.assertFalse(message["payload"]["teardown"])

            # The loop keeps serving exports and packet loads, but must not announce the
            # same ending once per idle tick.
            await asyncio.sleep(1.2)
            self.assertTrue(moderator.outbound_queue.empty())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_end_match_mid_read_emits_summary_and_keeps_export_alive(self):
        """End Match must stop a live tossup and leave the room idle, not dead."""
        moderator = ScienceBowlModerator(
            use_audio=False,
            manual_reader=True,
            initial_bank=[{"tossup_text": "Question", "tossup_answer": "Answer"}],
        )
        moderator.inbound_queue.put_nowait(Event(type="MANUAL_START_TOSSUP", payload={}))
        task = asyncio.create_task(moderator.run_game_loop())

        async def next_of_type(message_type, timeout=3.0):
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    self.fail(f"timed out waiting for {message_type}")
                message = await asyncio.wait_for(moderator.outbound_queue.get(), remaining)
                if message.get("type") == message_type:
                    return message

        try:
            # Wait until the human reader is actually mid-read, then pull the plug.
            deadline = asyncio.get_running_loop().time() + 3.0
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    self.fail("timed out waiting for the reading phase")
                message = await asyncio.wait_for(moderator.outbound_queue.get(), remaining)
                if message.get("type") == "MANUAL_PHASE" and message["payload"].get("phase") == "READING":
                    break

            moderator.inbound_queue.put_nowait(Event(type="END_MATCH", payload={}))
            complete = await next_of_type("MATCH_COMPLETE")
            # The tossup was never adjudicated, so the summary must not claim it was played.
            self.assertEqual(complete["payload"]["question_count"], 0)
            self.assertEqual(complete["payload"]["score_a"], 0)
            self.assertEqual(complete["payload"]["score_b"], 0)
            self.assertEqual(complete["payload"]["log"], [])
            self.assertTrue(complete["payload"]["teardown"])
            self.assertEqual(moderator.current_q_idx, 1)
            self.assertEqual(moderator.state.current_state, GameState.QUESTION_COMPLETE)
            self.assertEqual(moderator.manual_phase["phase"], "IDLE")

            # The finished room still serves the final screen's Export CSV.
            moderator.inbound_queue.put_nowait(Event(type="EXPORT_CSV", payload={}))
            exported = await next_of_type("DOWNLOAD_CSV")
            self.assertIn("Q_Num", exported["payload"]["csv_data"])

            # A forced end is a one-time boundary, not an event replayed every idle tick.
            await asyncio.sleep(1.2)
            self.assertTrue(moderator.outbound_queue.empty())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_idle_loop_disarms_a_stale_start_control(self):
        """A moderator whose packet ran out must not keep an armed Start Tossup button."""
        moderator = ScienceBowlModerator(use_audio=False)
        moderator.ui_state = {"show": "TOSSUP", "overrides": False}

        task = asyncio.create_task(moderator.run_game_loop())
        try:
            message = await self._next_message(moderator)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(message["type"], "UI_STATE")
        self.assertEqual(message["payload"]["show"], "NONE")


    async def test_loading_a_packet_arms_the_local_ai_start_control(self):
        moderator = ScienceBowlModerator(use_audio=False)
        await moderator.process_admin_event(Event(
            type="LOAD_BANK",
            payload={"bank": [{"tossup_text": "Question", "tossup_answer": "Answer"}]},
        ))

        message = await self._next_of_type(moderator, "UI_STATE")
        self.assertEqual(message["payload"]["show"], "TOSSUP")

    async def test_camera_buzz_is_rejected_in_online_rooms(self):
        moderator = ScienceBowlModerator(use_audio=False)
        moderator.online_mode = True

        await moderator._set_camera_buzz(True)

        message = await moderator.outbound_queue.get()
        self.assertEqual(message["type"], "UPDATE_STATUS")
        self.assertIn("in-person", message["payload"]["text"].lower())
        self.assertIsNone(moderator.vision)
        self.assertFalse(moderator.camera_buzz_enabled)

    async def test_camera_buzz_can_be_enabled_for_a_local_ai_room(self):
        moderator = ScienceBowlModerator(use_audio=False)
        moderator.vision = None
        with patch.object(main, "VisionService") as vision_class:
            await moderator._set_camera_buzz(True)

        vision_class.assert_called_once()
        self.assertTrue(moderator.camera_buzz_enabled)
        self.assertTrue(moderator.use_camera)
        vision_class.return_value.set_armed.assert_called_with(False)

        moderator._set_camera_window(True)
        vision_class.return_value.set_armed.assert_called_with(True)
        moderator._set_camera_window(False)
        vision_class.return_value.set_armed.assert_called_with(False)

    async def test_manual_start_mid_question_reports_in_progress(self):
        """A start arriving while a question is live must answer, not vanish."""
        moderator = ScienceBowlModerator(use_audio=False)

        await moderator.process_admin_event(Event(type="MANUAL_START_TOSSUP", payload={}))

        message = await moderator.outbound_queue.get()
        self.assertEqual(message["type"], "UPDATE_STATUS")
        self.assertIn("already in progress", message["payload"]["text"].lower())

    async def test_stale_reading_completion_cannot_skip_the_next_read(self):
        """A completion left over from an earlier speech must not satisfy a fresh read.

        Otherwise hitting Start Tossup jumps straight to the buzz window (and its
        5-second timer) without ever reading the question.
        """
        moderator = ScienceBowlModerator(
            use_audio=True,
            initial_bank=[{"tossup_text": "Question", "tossup_answer": "Answer"}],
        )
        moderator.state.transition_to(GameState.READING_TOSSUP)
        moderator.inbound_queue.put_nowait(Event(type="READING_DONE", payload={}))

        patched = [patch.object(main, key, value) for key, value in NO_READ_GUARD.items()]
        for item in patched:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in patched])

        task = asyncio.create_task(moderator._handle_tossup_attempt(moderator.question_bank[0]))

        async def collect_until_status(timeout=1.0):
            seen = []
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                try:
                    message = await asyncio.wait_for(moderator.outbound_queue.get(), 0.05)
                except asyncio.TimeoutError:
                    continue
                seen.append(message)
                if message.get("type") == "UPDATE_STATUS" and message["payload"].get("text") == "⏳ 5 Seconds to Buzz...":
                    return seen
            return seen

        try:
            early = await collect_until_status(0.5)
            early_statuses = [m["payload"].get("text") for m in early if m.get("type") == "UPDATE_STATUS"]
            self.assertNotIn("⏳ 5 Seconds to Buzz...", early_statuses, "the stale completion skipped the read")
            self.assertIn("🔊 Reading Tossup...", early_statuses)

            # The real completion still advances the question normally.
            moderator.inbound_queue.put_nowait(Event(type="READING_DONE", payload={}))
            late = await collect_until_status(1.0)
            late_statuses = [m["payload"].get("text") for m in late if m.get("type") == "UPDATE_STATUS"]
            self.assertIn("⏳ 5 Seconds to Buzz...", late_statuses)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_failed_read_warns_instead_of_claiming_the_question_was_read(self):
        """A station that cannot speak must say so; the read is not a finished read.

        Otherwise the buzz window (and its 5-second timer) opens with the question
        never having been read, which is what the moderator actually saw.
        """
        moderator = ScienceBowlModerator(
            use_audio=True,
            initial_bank=[{"tossup_text": "Question", "tossup_answer": "Answer"}],
        )
        moderator.state.transition_to(GameState.READING_TOSSUP)
        task = asyncio.create_task(moderator._handle_tossup_attempt(moderator.question_bank[0]))

        async def drive_failed_read():
            statuses = []
            while True:
                message = await asyncio.wait_for(moderator.outbound_queue.get(), 3.0)
                if message.get("type") == "START_READING":
                    moderator.inbound_queue.put_nowait(Event(type="READING_FAILED", payload={}))
                elif message.get("type") == "UPDATE_STATUS":
                    statuses.append(message["payload"]["text"])
                    if len(statuses) >= 3:
                        return statuses

        try:
            statuses = await drive_failed_read()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertIn("⏳ 5 Seconds to Buzz...", statuses)
        self.assertIn(READ_FAILED_WARNING, statuses)

    async def test_a_report_too_soon_to_be_a_read_does_not_open_the_buzz_window(self):
        """A station on older code reports the read as finished the instant it starts.

        That report cannot describe a spoken question, so it must not open the buzz window
        while the question is still being read out — the moderator saw the timer run with
        the question and then had no window left to buzz in.
        """
        moderator = ScienceBowlModerator(
            use_audio=True,
            initial_bank=[{"tossup_text": "Question " * 40, "tossup_answer": "Answer"}],
        )
        moderator.state.transition_to(GameState.READING_TOSSUP)

        async def run(earliest):
            seen = []

            async def watch(seconds):
                deadline = asyncio.get_running_loop().time() + seconds
                while asyncio.get_running_loop().time() < deadline:
                    try:
                        message = await asyncio.wait_for(moderator.outbound_queue.get(), 0.05)
                    except asyncio.TimeoutError:
                        continue
                    seen.append(message)
                    if message.get("type") == "START_READING":
                        moderator.inbound_queue.put_nowait(Event(type="READING_DONE", payload={}))

            await watch(earliest)
            early_statuses = [m["payload"].get("text") for m in seen if m.get("type") == "UPDATE_STATUS"]
            self.assertNotIn("⏳ 5 Seconds to Buzz...", early_statuses, "an impossible report ended the read")

            # The real read reports when it is done, and only then does the window open.
            self.assertTrue(moderator._report_is_early() is False, "the phantom report was still inside the believable window")
            moderator.inbound_queue.put_nowait(Event(type="READING_DONE", payload={}))
            await watch(1.5)
            late_statuses = [m["payload"].get("text") for m in seen if m.get("type") == "UPDATE_STATUS"]
            self.assertIn("⏳ 5 Seconds to Buzz...", late_statuses)

        with patch.object(main, "MIN_READ_SECONDS", 1.6), patch.object(main, "READ_CHARS_PER_SECOND", 400.0):
            task = asyncio.create_task(moderator._handle_tossup_attempt(moderator.question_bank[0]))
            try:
                # 1.6s estimate -> 0.64s of the read must exist before any report counts.
                await run(0.9)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_an_impossible_report_opens_the_window_when_the_read_would_have_had_its_say(self):
        """Nothing else is coming from a station whose only report was impossible.

        The window must open when the question has had the time to be spoken, not at the
        far later watchdog, and the moderator must be told the read went unreported.
        """
        moderator = ScienceBowlModerator(
            use_audio=True,
            initial_bank=[{"tossup_text": "Question " * 20, "tossup_answer": "Answer"}],
        )
        moderator.state.transition_to(GameState.READING_TOSSUP)

        # Both paces are compressed so the read window is a fraction of a second: the
        # point here is which fallback ends the read, not how long a real question takes.
        with patch.object(main, "MIN_READ_SECONDS", 1.0), patch.object(main, "READ_CHARS_PER_SECOND", 400.0), patch.object(
            main, "READ_SLOW_CHARS_PER_SECOND", 400.0
        ):
            task = asyncio.create_task(moderator._handle_tossup_attempt(moderator.question_bank[0]))
            try:
                # 1.0s estimate -> believable at 0.4s, assumed end at 1.0s, watchdog at 7.8s.
                async def report_immediately():
                    while True:
                        message = await asyncio.wait_for(moderator.outbound_queue.get(), 3.0)
                        if message.get("type") == "START_READING":
                            moderator.inbound_queue.put_nowait(Event(type="READING_DONE", payload={}))
                            return

                async def collect(seconds):
                    statuses = []
                    deadline = asyncio.get_running_loop().time() + seconds
                    while asyncio.get_running_loop().time() < deadline:
                        try:
                            message = await asyncio.wait_for(moderator.outbound_queue.get(), 0.05)
                        except asyncio.TimeoutError:
                            continue
                        if message.get("type") == "UPDATE_STATUS":
                            statuses.append(message["payload"]["text"])
                        if READ_TIMEOUT_WARNING in statuses:
                            return statuses
                    return statuses

                await report_immediately()
                elapsed_statuses = await collect(2.0)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        self.assertIn("⏳ 5 Seconds to Buzz...", elapsed_statuses)
        self.assertIn(READ_TIMEOUT_WARNING, elapsed_statuses)

    def test_a_guessed_read_end_is_not_earlier_than_a_real_question_takes_to_speak(self):
        """With no believable report the read is only guessed at a deliberately slow pace.

        Speech on the moderator's machine measures about 12 characters per second, so a
        guess derived from the faster sanity-check pace opened the buzz window — and its
        timer — while the question was still being read out; the guess must stay past the
        slowest reading a station can honestly produce.
        """
        measured_speech_chars_per_second = 12.0
        moderator = ScienceBowlModerator(use_audio=True)
        text = "Tossup,  " + ("Question " * 20) + "  "

        moderator._set_read_window(text)

        now = main.read_clock()
        guessed_end = moderator.read_assumed_end - now
        watchdog = moderator.read_deadline - now
        self.assertGreaterEqual(guessed_end, len(text) / measured_speech_chars_per_second)
        self.assertLess(guessed_end, watchdog)
        self.assertLess(moderator.read_earliest_end - now, guessed_end)

    async def test_a_silent_station_times_out_with_a_warning(self):
        """When nothing reports at all, the watchdog opens the window and says so."""
        moderator = ScienceBowlModerator(
            use_audio=True,
            initial_bank=[{"tossup_text": "Question", "tossup_answer": "Answer"}],
        )
        moderator.state.transition_to(GameState.READING_TOSSUP)

        with patch.object(main, "MIN_READ_SECONDS", 0.2), patch.object(main, "READ_WATCHDOG_MIN_SECONDS", 0.3), patch.object(
            main, "READ_EARLY_REPORT_FRACTION", 0.4
        ):
            task = asyncio.create_task(moderator._handle_tossup_attempt(moderator.question_bank[0]))
            try:
                statuses = []
                deadline = asyncio.get_running_loop().time() + 3.0
                while asyncio.get_running_loop().time() < deadline:
                    try:
                        message = await asyncio.wait_for(moderator.outbound_queue.get(), 0.1)
                    except asyncio.TimeoutError:
                        continue
                    if message.get("type") == "UPDATE_STATUS":
                        statuses.append(message["payload"]["text"])
                    if READ_TIMEOUT_WARNING in statuses:
                        break
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        self.assertIn("⏳ 5 Seconds to Buzz...", statuses)
        self.assertIn(READ_TIMEOUT_WARNING, statuses)

    async def test_tossup_talk_window_is_four_seconds(self):
        """The mic window a player is given to speak must be the configured 4 seconds."""
        moderator = ScienceBowlModerator(
            use_audio=True,
            initial_bank=[{"tossup_text": "Question", "tossup_answer": "Answer"}],
        )
        moderator.state.transition_to(GameState.READING_TOSSUP)
        patched = [patch.object(main, key, value) for key, value in NO_READ_GUARD.items()]
        for item in patched:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in patched])
        task = asyncio.create_task(moderator._handle_tossup_attempt(moderator.question_bank[0]))

        async def drive_to_listening():
            """Follow the tossup handshake until the server opens the answer window."""
            while True:
                message = await asyncio.wait_for(moderator.outbound_queue.get(), 3.0)
                kind = message.get("type")
                if kind == "START_READING":
                    moderator.inbound_queue.put_nowait(Event(type="READING_DONE", payload={}))
                    moderator.inbound_queue.put_nowait(Event(type="BUZZ", payload={"team": "Team A"}))
                elif kind == "BUZZ":
                    # Confirm the reader is done so the loop reaches the answer window.
                    moderator.inbound_queue.put_nowait(Event(type="READING_DONE", payload={}))
                elif kind == "START_LISTENING":
                    return message

        try:
            listening = await drive_to_listening()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(listening["payload"]["timeout"], 4.0)

    async def test_rewind_last_question_removes_its_rows_and_points(self):
        """Override is a replay: the completed question and everything it scored disappear."""
        moderator = ScienceBowlModerator(
            use_audio=False,
            manual_reader=True,
            initial_bank=[
                {"tossup_text": "First", "tossup_answer": "One"},
                {"tossup_text": "Second", "tossup_answer": "Two"},
            ],
        )
        moderator.current_q_idx = 1
        moderator.state.question_number = 2
        moderator.state.team_a_score = 14
        moderator.match_log = [
            {"Q_Num": 1, "Event_Type": "TOSSUP", "Team": "Team A", "Player": "A-1", "Correct": True, "Points": 4, "Score_A": 4, "Score_B": 0},
            {"Q_Num": 1, "Event_Type": "BONUS", "Team": "Team A", "Player": "Captain", "Correct": True, "Points": 10, "Score_A": 14, "Score_B": 0},
        ]

        self.assertTrue(await moderator._rewind_last_question())

        self.assertEqual(moderator.match_log, [])
        self.assertEqual(moderator.state.team_a_score, 0)
        self.assertEqual(moderator.state.team_b_score, 0)
        self.assertEqual(moderator.current_q_idx, 0)
        self.assertEqual(moderator.state.question_number, 1)
        self.assertEqual(moderator.state.current_state, GameState.IDLE)
        messages = []
        while not moderator.outbound_queue.empty():
            messages.append(await moderator.outbound_queue.get())
        self.assertIn("SYNC_LOG", [message["type"] for message in messages])
        self.assertEqual(next(message for message in messages if message["type"] == "SYNC_LOG")["payload"]["log"], [])
        reopened = next(message for message in messages if message["type"] == "UPDATE_QUESTION")
        self.assertEqual(reopened["payload"]["text"], "First")
        self.assertEqual(reopened["payload"]["answer"], "One")
        self.assertTrue(reopened["payload"]["manual"])

    async def test_override_at_start_tossup_wait_reloads_the_same_question(self):
        moderator = ScienceBowlModerator(
            use_audio=False,
            manual_reader=True,
            initial_bank=[
                {"tossup_text": "First", "tossup_answer": "One"},
                {"tossup_text": "Second", "tossup_answer": "Two"},
            ],
        )
        moderator.current_q_idx = 1
        moderator.state.question_number = 2
        moderator.state.team_a_score = 4
        moderator.match_log = [
            {"Q_Num": 1, "Event_Type": "TOSSUP", "Team": "Team A", "Player": "A-1", "Correct": True, "Points": 4, "Score_A": 4, "Score_B": 0},
        ]
        task = asyncio.create_task(moderator.run_game_loop())
        seen = []
        try:
            state = await self._next_of_type(moderator, "UI_STATE")
            self.assertEqual(state["payload"]["show"], "TOSSUP")
            self.assertTrue(state["payload"]["can_override"])
            moderator.inbound_queue.put_nowait(Event(type="OVERRIDE_LAST_QUESTION", payload={}))
            while True:
                message = await self._next_message(moderator)
                seen.append(message)
                if message.get("type") == "UI_STATE" and message["payload"].get("show") == "TOSSUP":
                    state = message
                    break
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        questions = [m["payload"]["text"] for m in seen if m["type"] == "UPDATE_QUESTION"]
        self.assertIn("First", questions)
        self.assertFalse(state["payload"]["can_override"])
        self.assertEqual(moderator.match_log, [])
        self.assertEqual(moderator.state.team_a_score, 0)
        self.assertEqual(moderator.current_q_idx, 0)
        self.assertIn("SYNC_LOG", [message["type"] for message in seen])

    async def test_override_at_start_bonus_wait_replays_the_tossup(self):
        moderator = ScienceBowlModerator(
            use_audio=False,
            manual_reader=True,
            initial_bank=[{
                "tossup_text": "What is the capital of France?",
                "tossup_answer": "Paris",
                "bonus_text": "Name the river through Paris.",
                "bonus_answer": "The Seine",
            }],
        )
        task = asyncio.create_task(moderator.run_game_loop())
        stage = 0
        try:
            while True:
                message = await self._next_message(moderator)
                kind = message.get("type")
                payload = message.get("payload", {})
                if kind == "UI_STATE" and payload.get("show") == "TOSSUP" and stage == 0:
                    self.assertFalse(payload.get("can_override"))
                    stage = 1
                    moderator.inbound_queue.put_nowait(Event(type="MANUAL_START_TOSSUP", payload={}))
                elif kind == "MANUAL_PHASE" and payload.get("phase") == "READING" and stage == 1:
                    stage = 2
                    moderator.inbound_queue.put_nowait(Event(type="BUZZ", payload={"team": "Team A", "player": "A-1"}))
                elif kind == "MANUAL_PHASE" and payload.get("phase") == "ANSWERING" and stage == 2:
                    stage = 3
                    moderator.inbound_queue.put_nowait(
                        Event(type="LOG_BUZZPOINT", payload={"buzzpoint": "Full Read", "after_read": True})
                    )
                elif kind == "BUZZPOINT_SET" and stage == 3:
                    stage = 4
                    moderator.inbound_queue.put_nowait(Event(type="FORCE_JUDGMENT", payload={"correct": True}))
                elif kind == "UI_STATE" and payload.get("show") == "BONUS" and stage == 4:
                    self.assertTrue(payload.get("can_override"))
                    self.assertEqual(moderator.state.team_a_score, 4)
                    stage = 5
                    moderator.inbound_queue.put_nowait(Event(type="OVERRIDE_LAST_QUESTION", payload={}))
                elif kind == "UI_STATE" and payload.get("show") == "TOSSUP" and stage == 5:
                    self.assertFalse(payload.get("can_override"))
                    break
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(stage, 5)
        self.assertEqual(moderator.match_log, [])
        self.assertEqual(moderator.state.team_a_score, 0)
        self.assertEqual(moderator.current_q_idx, 0)
        self.assertEqual(moderator.state.question_number, 1)

    async def test_end_after_a_natural_finish_reopens_the_teardown_summary(self):
        """Closing a packet summary and then choosing End Match must still offer teardown."""
        moderator = ScienceBowlModerator(
            use_audio=False,
            initial_bank=[{"tossup_text": "Question", "tossup_answer": "Answer"}],
        )
        moderator.current_q_idx = 1
        moderator.match_complete_notified = True
        moderator.match_complete_question_count = 1
        moderator.state.transition_to(GameState.QUESTION_COMPLETE)
        moderator.match_log = [{
            "Q_Num": 1, "Event_Type": "TOSSUP", "Team": "Team A", "Player": "A-1",
            "Correct": True, "Points": 4, "Score_A": 4, "Score_B": 0,
        }]
        moderator.state.team_a_score = 4
        moderator.inbound_queue.put_nowait(Event(type="END_MATCH", payload={}))

        task = asyncio.create_task(moderator.run_game_loop())
        try:
            complete = await self._next_of_type(moderator, "MATCH_COMPLETE")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertTrue(complete["payload"]["teardown"])
        self.assertEqual(complete["payload"]["score_a"], 4)
        self.assertEqual(complete["payload"]["question_count"], 1)

    async def test_close_match_emits_the_room_teardown_request(self):
        moderator = ScienceBowlModerator(use_audio=False)

        await moderator.process_admin_event(Event(type="CLOSE_MATCH", payload={}))

        message = await moderator.outbound_queue.get()
        self.assertEqual(message["type"], "ROOM_CLOSE_REQUESTED")


if __name__ == "__main__":
    unittest.main()
