import asyncio
import unittest

from core.state_machine import GameState
from main import ScienceBowlModerator


class NoAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_silent_tossup_reaches_buzz_window(self):
        moderator = ScienceBowlModerator(
            use_audio=False,
            initial_bank=[{"tossup_text": "Question", "tossup_answer": "Answer"}],
        )
        moderator.state.transition_to(GameState.READING_TOSSUP)
        task = asyncio.create_task(moderator._handle_tossup_attempt(moderator.question_bank[0]))
        try:
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            messages = [await moderator.outbound_queue.get() for _ in range(3)]
            self.assertEqual(messages[0]["type"], "UPDATE_QUESTION")
            self.assertEqual(messages[1]["payload"]["text"], "🔊 Reading Tossup...")
            self.assertEqual(messages[2]["payload"]["text"], "⏳ 5 Seconds to Buzz...")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
