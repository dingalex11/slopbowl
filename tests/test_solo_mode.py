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
AUTH_SOURCE = (ROOT / "static/js/auth.js").read_text(encoding="utf-8")
RULES_SOURCE = (ROOT / "firestore.rules").read_text(encoding="utf-8")


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
        self.assertIn("span.textContent", SOLO_SOURCE)
        self.assertIn("function soloNormalizeText(", SOLO_SOURCE)

    def test_leaving_the_drill_slides_releases_speech_and_timers(self):
        self.assertIn("if (typeof soloLeavePractice === 'function') soloLeavePractice(slideNum);", SHELL_SOURCE)
        self.assertIn("soloStopRecognition();", SOLO_SOURCE)

    def test_the_telemetry_card_exposes_session_statistics(self):
        for control in (
            'id="solo-stat-asked"',
            'id="solo-stat-accuracy"',
            'id="solo-stat-celerity"',
            'id="solo-stat-fastest"',
            'id="solo-subject-stats"',
            'id="solo-subject-stats-empty"',
        ):
            self.assertIn(control, INDEX_SOURCE, f"the telemetry card is missing {control}")
        for column in ("Subject", "Asked", "Correct", "Accuracy", "Celerity"):
            self.assertIn(f"{column}</th>", INDEX_SOURCE, f"the subject table is missing the {column} column")

    def test_accuracy_is_measured_over_resolved_questions_and_celerity_over_buzzes(self):
        """Accuracy must ignore toss-ups still in flight, and celerity is the mean buzz."""
        self.assertIn("const resolved = questions.filter((entry) => entry.outcome !== 'pending');", SOLO_SOURCE)
        self.assertIn("accuracy: resolved.length ? (correct.length / resolved.length) * 100 : null,", SOLO_SOURCE)
        self.assertIn("celerity: mean(reactions),", SOLO_SOURCE)
        self.assertIn("fastest: reactions.length ? Math.min(...reactions) : null,", SOLO_SOURCE)
        # Per-subject rows carry their own accuracy and celerity, keyed by the subject the
        # toss-up belongs to rather than its raw category string.
        self.assertIn("accuracy: row.resolved ? (row.correct / row.resolved) * 100 : null,", SOLO_SOURCE)
        self.assertIn("celerity: mean(row.reactions),", SOLO_SOURCE)
        self.assertIn("subject: soloSubjectFor(question.category),", SOLO_SOURCE)

    def test_a_re_read_question_stays_one_question(self):
        """Re-drilling a toss-up adds an attempt, not a second question to the accuracy."""
        self.assertIn("existing.reReads += 1;", SOLO_SOURCE)
        self.assertIn("if (!entry || entry.outcome !== 'pending') return;", SOLO_SOURCE)
        self.assertIn("soloDrill.entryIndex = soloSession.questions.length - 1;", SOLO_SOURCE)
        self.assertIn("soloDrill.entryIndex = null;", SOLO_SOURCE)

    def test_every_outcome_is_recorded_once(self):
        for outcome in ("soloResolveEntry('expired', false);", "soloResolveEntry('revealed', false);"):
            self.assertIn(outcome, SOLO_SOURCE)
        self.assertIn("soloResolveEntry(outcome, correct);", SOLO_SOURCE)
        self.assertIn("if (typeof entry.reaction !== 'number') entry.reaction = soloDrill.lastReaction;", SOLO_SOURCE)

    def test_a_session_is_kept_on_the_device_and_can_be_saved_to_the_account(self):
        self.assertIn('id="solo-save-session"', INDEX_SOURCE)
        self.assertIn('id="solo-save-status"', INDEX_SOURCE)
        self.assertIn('id="solo-load-sessions"', INDEX_SOURCE)
        self.assertIn('id="solo-saved-list"', INDEX_SOURCE)
        self.assertIn("soloKeepLocalSession(payload);", SOLO_SOURCE)
        self.assertIn("window.saveSoloSessionToCloud = async (session) => {", AUTH_SOURCE)
        self.assertIn("window.loadSoloSessionsFromCloud = async (limit = 10) => {", AUTH_SOURCE)
        # Sessions are the signed-in owner's documents, and the existing rules already
        # cover every collection under /users/{uid}.
        self.assertIn('doc(db, "users", user.uid, "solo_sessions", session.id)', AUTH_SOURCE)
        self.assertIn('collection(db, "users", user.uid, "solo_sessions")', AUTH_SOURCE)
        self.assertIn("match /{document=**}", RULES_SOURCE)

    def test_practice_still_works_signed_out_and_without_firebase(self):
        """A bare page with no Firebase configured must keep the session, not fail."""
        self.assertIn("typeof window.saveSoloSessionToCloud !== 'function'", SOLO_SOURCE)
        self.assertIn("Kept on this device.", SOLO_SOURCE)
        self.assertIn("soloSaveStatus(`Saved to ${user.email || 'your account'}.`, 'green');", SOLO_SOURCE)
        # Being signed out keeps the session local and says so; it is not reported as a failure.
        self.assertIn("if (/sign in/i.test(reason))", SOLO_SOURCE)

    def test_sample_packet_is_public_so_practice_can_start_immediately(self):
        response = TestClient(ui_service.app).get("/api/sample-bank")
        self.assertEqual(response.status_code, 200, "the sample packet must load without a token")
        bank = response.json()
        self.assertTrue(bank)
        self.assertIn("tossup_text", bank[0])
        self.assertIn("tossup_answer", bank[0])


class SoloChallengeParityTests(unittest.TestCase):
    """The drill plays the same game as a buzzboard challenge.

    Read, a five-second post-read buzz window, a five-second typed answer, a bonus after a
    correct toss-up, and a Next that starts the next read — the same loop, with the solo
    setup's delivery choices still applied.
    """

    def test_the_drill_has_the_challenge_s_two_answer_windows(self):
        self.assertIn("const SOLO_BONUS_WINDOW_S = 20.0;", SOLO_SOURCE)
        self.assertIn("function soloOpenAnswerPanel(seconds, phase)", SOLO_SOURCE)
        self.assertIn("soloOpenAnswerPanel(SOLO_BONUS_WINDOW_S, 'bonus');", SOLO_SOURCE)
        self.assertIn("soloOpenAnswerPanel(SOLO_LOCKOUT_SECONDS, 'tossup');", SOLO_SOURCE)

    def test_time_running_out_grades_the_box(self):
        self.assertIn("function soloAutoSubmitAnswer()", SOLO_SOURCE)
        self.assertIn("if (typed.trim()) {", SOLO_SOURCE)
        self.assertIn("soloSubmitAnswer(typed, 'timeout');", SOLO_SOURCE)
        self.assertIn("soloSubmitBonus(typed, 'timeout');", SOLO_SOURCE)
        self.assertIn("soloRecordResult(false, `Time expired", SOLO_SOURCE)

    def test_a_correct_tossup_moves_into_its_bonus(self):
        self.assertIn("function soloSubmitBonus(response, outcome = 'answered')", SOLO_SOURCE)
        self.assertIn("function soloJudgeBonus(question, response)", SOLO_SOURCE)
        self.assertIn("soloDrill.bonusPending = Boolean(correct", SOLO_SOURCE)
        nxt = SOLO_SOURCE[SOLO_SOURCE.index("function soloNextTossup()"):]
        nxt = nxt[:nxt.index("\n}\n")]
        self.assertIn("soloStartQuestion('bonus');", nxt)
        self.assertIn("soloStartQuestion('tossup');", nxt)

    def test_next_starts_the_next_read_without_a_second_click(self):
        self.assertIn("function soloStartQuestion(phase = 'tossup')", SOLO_SOURCE)
        self.assertIn("soloStartQuestion('tossup');\n  playSound('click');", SOLO_SOURCE)
        self.assertIn("// Auto-start the next read, exactly like the challenge's Next button.", SOLO_SOURCE)

    def test_a_solo_buzz_uses_the_shared_word_tracker(self):
        self.assertIn("window.BuzzWordTracker(readText", SOLO_SOURCE)
        self.assertIn("soloDrill.tracker.snapshotAtEnd()", SOLO_SOURCE)
        self.assertIn("soloDrill.frozenAt = soloDrill.revealIndex;", SOLO_SOURCE)
        self.assertIn("function soloRevealFullQuestion()", SOLO_SOURCE)
        # The answer closes the frozen read, exactly as on the challenge screen.
        self.assertIn("soloRevealFullQuestion();", SOLO_SOURCE)

    def test_the_answer_panel_names_the_phase_and_shows_the_buzz_word(self):
        self.assertIn('id="solo-answer-title"', INDEX_SOURCE)
        self.assertIn('id="solo-buzz-word"', INDEX_SOURCE)
        self.assertIn('id="solo-buzz-source"', INDEX_SOURCE)
        self.assertIn("Bonus answer —", SOLO_SOURCE)


if __name__ == "__main__":
    unittest.main()
