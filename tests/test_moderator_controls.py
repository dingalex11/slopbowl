import asyncio
import unittest

from core.events import Event
from core.state_machine import GameState
from main import ScienceBowlModerator


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

    async def test_tossup_talk_window_is_four_seconds(self):
        """The mic window a player is given to speak must be the configured 4 seconds."""
        moderator = ScienceBowlModerator(
            use_audio=True,
            initial_bank=[{"tossup_text": "Question", "tossup_answer": "Answer"}],
        )
        moderator.state.transition_to(GameState.READING_TOSSUP)
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
