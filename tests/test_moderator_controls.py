import asyncio
import unittest

from core.events import Event
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


if __name__ == "__main__":
    unittest.main()
