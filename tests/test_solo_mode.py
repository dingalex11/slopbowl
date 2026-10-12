"""Solo practice drills: the setup screen, the dojo and the packet it drills from.

The drill runs entirely in the browser, so these tests pin the contract the page must
keep: the controls the setup and dojo screens expose, the promise that practice needs no
room or account, and the read-completion rule that a hybrid read is not over until both
its channels are.
"""

import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from services import ui_service

ROOT = Path(__file__).resolve().parents[1]
SOLO_SOURCE = (ROOT / "static/js/solo.js").read_text(encoding="utf-8")
INDEX_SOURCE = (ROOT / "index.html").read_text(encoding="utf-8")
SHELL_SOURCE = (ROOT / "static/js/slopbowl_ui.js").read_text(encoding="utf-8")


class SoloModeTests(unittest.TestCase):
    def test_practice_needs_no_room_account_or_server_call(self):
        """Solo training must work on a bare page load: no socket, no sign-in."""
        self.assertNotIn("sendEvent(", SOLO_SOURCE)
        self.assertNotIn("new WebSocket", SOLO_SOURCE)
        self.assertIn("function openSoloSetup()", SOLO_SOURCE)
        self.assertIn("function startSoloDrill()", SOLO_SOURCE)

    def test_setup_screen_exposes_delivery_and_capture_modes(self):
        for control in (
            'data-solo-output="spoken"',
            'data-solo-output="typed"',
            'data-solo-output="hybrid"',
            'data-solo-input="audio"',
            'data-solo-input="typed"',
            'id="solo-voice-select"',
            'id="solo-rate-slider"',
            'id="solo-test-voice"',
            'id="solo-mic-status"',
            'id="solo-test-mic"',
            'id="solo-wpm-slider"',
            'id="solo-subject-list"',
            'id="solo-packet-list"',
            'id="solo-start-drill"',
        ):
            self.assertIn(control, INDEX_SOURCE, f"the setup screen is missing {control}")

    def test_dojo_screen_exposes_the_drill_controls(self):
        for control in (
            'id="solo-score"',
            'id="solo-streak"',
            'id="solo-reaction"',
            'id="solo-scope-badge"',
            'id="solo-mode-line"',
            'id="solo-q-label"',
            'id="solo-word-count"',
            'id="solo-lockout"',
            'id="solo-question-text"',
            'id="solo-progress-bar"',
            'id="solo-buzz-btn"',
            'id="solo-answer-panel"',
            'id="solo-answer-input"',
            'id="solo-start-question"',
            'id="solo-reveal-answer"',
            'id="solo-next-tossup"',
        ):
            self.assertIn(control, INDEX_SOURCE, f"the dojo screen is missing {control}")

    def test_the_work_in_progress_placeholder_is_retired(self):
        self.assertNotIn("wipModal", INDEX_SOURCE)
        self.assertNotIn("openWipModal", INDEX_SOURCE)
        self.assertNotIn("openWipModal", SHELL_SOURCE)
        # Header button, home card and the dojo's Reconfig all enter the drill.
        self.assertEqual(INDEX_SOURCE.count("openSoloSetup(); playSound('click');"), 3,
                         "every entry point into solo practice must open the setup screen")
        self.assertIn('<script src="/static/js/solo.js"></script>', INDEX_SOURCE)

    def test_a_hybrid_read_is_over_only_when_both_channels_finish(self):
        """The teleprompter finishes long before the voice, so the spoken channel gates it.

        Opening the window when the stream ended started the lockout while the question was
        still being read aloud.
        """
        self.assertIn("streamDone: false,", SOLO_SOURCE)
        self.assertIn("speechDone: false,", SOLO_SOURCE)
        self.assertIn("if (!soloDrill.streamDone || !soloDrill.speechDone) return;", SOLO_SOURCE)

    def test_spoken_delivery_survives_a_missing_end_event(self):
        """A browser that never fires 'end' must not leave the drill waiting forever."""
        self.assertIn("window.speechSynthesis.speaking || window.speechSynthesis.pending", SOLO_SOURCE)
        self.assertIn("if (spoke || performance.now() - begin > 1500) settle();", SOLO_SOURCE)

    def test_packet_text_is_streamed_as_text_not_markup(self):
        """Packet text is untrusted: it must reach the page as text nodes only."""
        self.assertNotIn("innerHTML", SOLO_SOURCE)
        self.assertIn("shown.textContent", SOLO_SOURCE)
        self.assertIn("function soloNormalizeText(", SOLO_SOURCE)

    def test_leaving_the_drill_slides_releases_speech_and_timers(self):
        self.assertIn("if (typeof soloLeavePractice === 'function') soloLeavePractice(slideNum);", SHELL_SOURCE)
        self.assertIn("soloStopRecognition();", SOLO_SOURCE)

    def test_sample_packet_is_public_so_practice_can_start_immediately(self):
        response = TestClient(ui_service.app).get("/api/sample-bank")
        self.assertEqual(response.status_code, 200, "the sample packet must load without a token")
        bank = response.json()
        self.assertTrue(bank)
        self.assertIn("tossup_text", bank[0])
        self.assertIn("tossup_answer", bank[0])


if __name__ == "__main__":
    unittest.main()
