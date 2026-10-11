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

        with patch.object(main, "MIN_READ_SECONDS", 1.0), patch.object(main, "READ_CHARS_PER_SECOND", 400.0):
            task = asyncio.create_task(moderator._handle_tossup_attempt(moderator.question_bank[0]))
            try:
                # 1.0s estimate -> believable at 0.4s, assumed end at 1.15s.
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


if __name__ == "__main__":
    unittest.main()
