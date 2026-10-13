"""Buzzpoint Board: the word tracker's contract, the attempt lock, and the board's safety.

The tracker and run flow live in the browser, so — like the other frontend tests — those
behaviours are pinned by asserting on the source (there is no JS runtime in the suite),
while the pure logic (word offsets, grading, run progression, the attempt lock) is
unit-tested directly against ``core``.

The non-negotiable primitive under test: track every word the TTS reads, and when a buzz
fires, log the word being spoken at that instant as the buzz-point.
"""

import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from core import challenge, grading, words
from services import ui_service

ROOT = Path(__file__).resolve().parents[1]
BUZZPOINT_SOURCE = (ROOT / "static/js/buzzpoint.js").read_text(encoding="utf-8")
INDEX_SOURCE = (ROOT / "index.html").read_text(encoding="utf-8")
AUTH_SOURCE = (ROOT / "static/js/auth.js").read_text(encoding="utf-8")
RULES_SOURCE = (ROOT / "firestore.rules").read_text(encoding="utf-8")
UI_SERVICE_SOURCE = (ROOT / "services/ui_service.py").read_text(encoding="utf-8")
CHALLENGE_SERVICE_SOURCE = (ROOT / "services/challenge_service.py").read_text(encoding="utf-8")


class WordOffsetTests(unittest.TestCase):
    def test_tokenize_keeps_char_offsets(self):
        tokens = words.tokenize_words("The nucleus holds DNA.")
        self.assertEqual([token["word"] for token in tokens], ["The", "nucleus", "holds", "DNA"])
        self.assertEqual(tokens[1]["start"], 4)
        self.assertEqual(tokens[1]["end"], 11)

    def test_a_char_index_maps_to_the_word_it_falls_on(self):
        tokens = words.tokenize_words("the nucleus holds")
        self.assertEqual(words.word_index_at(tokens, 5), 1)  # inside "nucleus"
        self.assertEqual(words.word_index_at(tokens, 0), 0)
        self.assertEqual(words.word_index_at(tokens, 200), 2)  # clamps to the last word

    def test_offsets_are_bounds_checked_not_trusted(self):
        # The speech engine's charIndex is not reliable across browsers.
        tokens = words.tokenize_words("alpha beta")
        self.assertEqual(words.word_index_at(tokens, -5), 0)
        self.assertEqual(words.word_index_at(tokens, "x"), 0)
        self.assertEqual(words.word_index_at([], 3), 0)
        self.assertEqual(words.tokenize_words(None), [])

    def test_the_js_tracker_uses_the_same_word_pattern(self):
        # core/words.py is the reference; buzzpoint.js must not drift from it.
        self.assertIn(words.WORD_PATTERN, BUZZPOINT_SOURCE)


class GradingTests(unittest.TestCase):
    def test_keyword_matcher_accepts_exact_and_contained(self):
        self.assertTrue(grading.judge_keyword("NUCLEUS", "nucleus"))
        self.assertTrue(grading.judge_keyword("MITOCHONDRIA", "the mitochondria"))

    def test_single_word_answer_accepts_a_five_character_opening(self):
        self.assertTrue(grading.judge_keyword("mitochondria", "mitocondria"))

    def test_multi_word_answer_needs_every_significant_word(self):
        self.assertTrue(grading.judge_keyword("double helix", "it is a double helix"))
        self.assertFalse(grading.judge_keyword("double helix", "just helix"))

    def test_an_empty_response_is_never_correct(self):
        self.assertFalse(grading.judge_keyword("nucleus", ""))
        self.assertFalse(grading.grade_tossup({"tossup_answer": "nucleus"}, ""))

    def test_multiple_choice_accepts_the_letter_or_the_phrase(self):
        question = {"type": "MC", "tossup_answer": "W) STATIONARY"}
        self.assertTrue(grading.grade_tossup(question, "W"))
        self.assertTrue(grading.grade_tossup(question, "stationary"))
        self.assertTrue(grading.grade_tossup(question, "W) STATIONARY"))
        self.assertFalse(grading.grade_tossup(question, "X"))

    def test_bonus_grading_uses_the_bonus_answer(self):
        question = {"bonus_answer": "HEMOGLOBIN"}
        self.assertTrue(grading.grade_bonus(question, "hemoglobin"))
        self.assertFalse(grading.grade_bonus(question, "nucleus"))


class ChallengeStateMachineTests(unittest.TestCase):
    def test_a_served_question_never_carries_its_answer(self):
        bank = [{
            "tossup_text": "q", "tossup_answer": "SECRET",
            "bonus_text": "b", "bonus_answer": "BSECRET",
        }]
        self.assertNotIn("SECRET", str(challenge.strip_question(bank[0], "tossup")))
        self.assertNotIn("BSECRET", str(challenge.strip_question(bank[0], "bonus")))
        self.assertIn("text", challenge.strip_question(bank[0], "tossup"))

    def test_next_question_reports_done_at_the_end(self):
        bank = [{"tossup_text": "q"}]
        run = challenge.create_run_doc("c", "u", "n", 1, "now")
        self.assertFalse(challenge.next_question_payload(bank, run)["done"])
        run["cursor"] = 1
        self.assertTrue(challenge.next_question_payload(bank, run)["done"])

    def test_a_finished_attempt_cannot_be_restarted(self):
        # The lock: one run per uid; a spent attempt refuses a re-join.
        self.assertEqual(challenge.join_outcome(None)[0], "create")
        self.assertEqual(challenge.join_outcome({"status": "in_progress"})[0], "resume")
        self.assertEqual(challenge.join_outcome({"status": "finished"})[0], "refuse")

    def test_a_correct_tossup_earns_the_bonus_on_the_same_question(self):
        run = challenge.create_run_doc("c", "u", "n", 2, "now")
        challenge.advance_after_tossup(run, True, 2, "now")
        self.assertEqual(run["phase"], "bonus")
        self.assertEqual(run["cursor"], 0)
        challenge.advance_after_bonus(run, 2, "now")
        self.assertEqual(run["phase"], "tossup")
        self.assertEqual(run["cursor"], 1)

    def test_a_wrong_tossup_moves_on_without_a_bonus(self):
        run = challenge.create_run_doc("c", "u", "n", 1, "now")
        challenge.advance_after_tossup(run, False, 1, "now")
        self.assertEqual(run["phase"], "tossup")
        self.assertEqual(run["status"], "finished")

    def test_the_record_carries_the_buzz_word_and_capture_source(self):
        record = challenge.build_tossup_record(
            {"category": "Life Science", "type": "SA"}, 0,
            {"word": "nucleus", "wordIndex": 4, "charIndex": 20, "wordsHeard": 5,
             "totalWords": 30, "captureSource": "boundary",
             "lastConfirmedWord": "the", "confidence": "word boundary event"},
            "nucleus", "auto-correct", "answered",
            {"firstKeystrokeMs": 300, "submitMs": 1200},
        )
        self.assertEqual(record["buzzWord"], "nucleus")
        self.assertEqual(record["captureSource"], "boundary")
        self.assertEqual(record["subject"], "Biology")
        self.assertEqual(record["typedAnswer"], "nucleus")
        self.assertIsNone(record["bonusVerdict"])

    def test_a_bonus_folds_into_the_same_record(self):
        record = challenge.build_tossup_record({}, 0, None, "", "auto-correct", "answered", None)
        challenge.apply_bonus_to_record(record, "hemoglobin", "auto-correct", False)
        self.assertEqual(record["bonusTypedAnswer"], "hemoglobin")
        self.assertEqual(record["bonusVerdict"], "auto-correct")
        self.assertFalse(record["bonusExpired"])


class BuzzWordTrackerContractTests(unittest.TestCase):
    def test_a_cancel_is_never_recorded_as_a_completed_word(self):
        # Snapshot the word BEFORE cancelling, and a cancel must not settle the read or
        # mark a word complete (browsers fire 'end' on a cancelled utterance).
        snapshot_call = BUZZPOINT_SOURCE.index("bukRun.tracker.snapshot()")
        cancel_call = BUZZPOINT_SOURCE.index("bukRun.tracker.markCancelled()")
        self.assertLess(snapshot_call, cancel_call,
                        "the buzz word must be snapshotted before the read is cancelled")
        self.assertIn("if (bukRun.tracker && bukRun.tracker.isCancelled()) return;", BUZZPOINT_SOURCE)
        self.assertIn("function markCancelled() { cancelRequested = true; }", BUZZPOINT_SOURCE)

    def test_the_tracker_labels_its_capture_source(self):
        for source in ("'boundary'", "'counted'", "'estimated'"):
            self.assertIn(source, BUZZPOINT_SOURCE, f"capture source {source} must be labelled")
        self.assertIn("confidenceNote", BUZZPOINT_SOURCE)

    def test_the_tracker_keeps_two_word_values(self):
        self.assertIn("wordInProgress", BUZZPOINT_SOURCE)
        self.assertIn("lastConfirmedWord", BUZZPOINT_SOURCE)
        self.assertIn("noteNaturalEnd", BUZZPOINT_SOURCE)

    def test_the_first_word_is_seeded_on_start_not_on_a_boundary(self):
        self.assertIn("function noteStart()", BUZZPOINT_SOURCE)
        self.assertIn("utterance.onstart", BUZZPOINT_SOURCE)

    def test_the_tracker_is_reusable_beyond_the_board(self):
        self.assertIn("window.BuzzWordTracker = createBuzzWordTracker;", BUZZPOINT_SOURCE)
        self.assertIn("window.buzzTokenizeWords = buzzTokenizeWords;", BUZZPOINT_SOURCE)


class BoardRenderingSafetyTests(unittest.TestCase):
    def test_the_board_renders_packet_text_as_text_not_markup(self):
        # Packet text is untrusted: it must never reach the page as markup.
        self.assertNotIn("innerHTML", BUZZPOINT_SOURCE)
        self.assertIn("textContent", BUZZPOINT_SOURCE)
        self.assertIn("createElement", BUZZPOINT_SOURCE)

    def test_the_buzzboard_analytics_view_renders_imported_values_as_text(self):
        # An uploaded board CSV is untrusted too: it is text, never markup.
        source = (ROOT / "static/js/buzzboard_analytics.js").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", source)
        self.assertIn("textContent", source)
        self.assertIn("createElement", source)

    def test_the_buzz_line_view_moved_from_the_board_to_analytics(self):
        """The board exports data; the analysis lives in the Analytics station.

        The board used to draw buzz depth, subject accuracy and early-and-wrong charts in
        place. It now only downloads its CSV, and the Analytics station reads that CSV back
        into the question-buzzpoint view and the candidate matrix \u2014 so those views are
        pinned in static/js/buzzboard_analytics.js instead.
        """
        self.assertIn("function bukBoardCsvRows(", BUZZPOINT_SOURCE)
        self.assertNotIn("bukBoardBuzzTable(", BUZZPOINT_SOURCE)
        self.assertNotIn("bukBoardEarlyWrong(", BUZZPOINT_SOURCE)
        source = (ROOT / "static/js/buzzboard_analytics.js").read_text(encoding="utf-8")
        self.assertIn("function bbxRenderQuestions(", source)
        self.assertIn("function bbxRenderMatrix(", source)
        self.assertIn("function bbxRenderHud(", source)
        self.assertIn("function parseCSVRows(csvText)",
                      (ROOT / "static/js/analytics.js").read_text(encoding="utf-8"))
        self.assertIn("parseCSVRows(csvText)", source)


class BoardExportTests(unittest.TestCase):
    def test_the_export_carries_the_packet_so_analytics_can_rebuild_the_cards(self):
        # One row per attempt, with the question text, its answer, the category and the
        # word position \u2014 enough to redraw the highlighted question cards and the matrix.
        for column in ("QuestionText", "Answer", "Category", "BuzzWordIndex", "BuzzDepthPct"):
            self.assertIn(f"'{column}'", BUZZPOINT_SOURCE)

    def test_the_download_refuses_to_export_spreadsheet_formulas(self):
        self.assertIn("/^[=+\\-@\\t\\r]/", BUZZPOINT_SOURCE)


class ChallengeSettingsTests(unittest.TestCase):
    def test_a_correct_tossup_skips_the_bonus_when_bonuses_are_off(self):
        run = challenge.create_run_doc("c", "u", "n", 2, "now", read_bonuses=False)
        challenge.advance_after_tossup(run, True, 2, "now")
        self.assertEqual(run["phase"], "tossup")
        self.assertEqual(run["cursor"], 1)

    def test_bonuses_still_follow_a_correct_tossup_when_they_are_on(self):
        run = challenge.create_run_doc("c", "u", "n", 2, "now")
        challenge.advance_after_tossup(run, True, 2, "now")
        self.assertEqual(run["phase"], "bonus")

    def test_the_display_mode_travels_with_every_served_question(self):
        bank = [{"tossup_text": "q"}]
        run = challenge.create_run_doc("c", "u", "n", 1, "now", display_mode="audio-only")
        payload = challenge.next_question_payload(bank, run)
        self.assertEqual(payload["displayMode"], "audio-only")
        self.assertTrue(payload["readBonuses"])

    def test_bonuses_off_travels_with_every_served_question(self):
        bank = [{"tossup_text": "q"}]
        run = challenge.create_run_doc("c", "u", "n", 1, "now", read_bonuses=False)
        self.assertFalse(challenge.next_question_payload(bank, run)["readBonuses"])

    def test_the_challenge_stores_the_settings_the_captain_chose(self):
        self.assertIn('"readBonuses": read_bonuses', CHALLENGE_SERVICE_SOURCE)
        self.assertIn('"displayMode": display_mode', CHALLENGE_SERVICE_SOURCE)
        self.assertIn('read_bonuses=data.get("readBonuses", True)', UI_SERVICE_SOURCE)
        self.assertIn('display_mode=data.get("displayMode", "hybrid")', UI_SERVICE_SOURCE)

    def test_audio_only_hides_the_words_but_keeps_the_progress_bar(self):
        self.assertIn("bukRun.displayMode === 'audio-only'", BUZZPOINT_SOURCE)
        self.assertIn("Reading question", BUZZPOINT_SOURCE)

    def test_the_captain_sets_both_settings_when_creating_the_challenge(self):
        self.assertIn('id="buk-captain-bonuses"', INDEX_SOURCE)
        self.assertIn('id="buk-captain-display"', INDEX_SOURCE)
        self.assertIn("readBonuses, displayMode", BUZZPOINT_SOURCE)


class BuzzboardAnalyticsShellTests(unittest.TestCase):
    def test_the_analytics_station_offers_both_data_sources(self):
        self.assertIn('id="analytics-tab-match"', INDEX_SOURCE)
        self.assertIn('id="analytics-tab-buzzboard"', INDEX_SOURCE)
        self.assertIn("<script src=\"/static/js/buzzboard_analytics.js\"></script>", INDEX_SOURCE)

    def test_the_buzzboard_view_has_the_two_pages(self):
        self.assertIn('id="bbx-page-questions"', INDEX_SOURCE)
        self.assertIn('id="bbx-page-numbers"', INDEX_SOURCE)
        self.assertIn('id="bbx-hud"', INDEX_SOURCE)
        self.assertIn('id="bbx-questions-container"', INDEX_SOURCE)
        self.assertIn('id="bbx-matrix"', INDEX_SOURCE)


class AttemptLockTests(unittest.TestCase):
    def test_the_run_document_is_created_once_so_an_attempt_cannot_be_doubled(self):
        self.assertIn("_run_ref(code, uid).create(run)", CHALLENGE_SERVICE_SOURCE)
        self.assertIn('raise PermissionError("You have already used your attempt on this challenge")',
                      CHALLENGE_SERVICE_SOURCE)

    def test_the_bank_with_answers_is_stored_where_clients_cannot_read_it(self):
        self.assertIn("client.collection(BANKS).document(code).set(", CHALLENGE_SERVICE_SOURCE)
        self.assertIn('match /challenge_banks/{code}', RULES_SOURCE)
        self.assertIn("allow read, write: if false;", RULES_SOURCE)

    def test_run_documents_are_server_authoritative_and_owner_readable(self):
        self.assertIn("request.auth.uid == uid", RULES_SOURCE)
        self.assertIn("allow write: if false;", RULES_SOURCE)

    def test_grading_happens_on_the_server_not_the_browser(self):
        self.assertIn("from core.grading import grade_bonus, grade_tossup", CHALLENGE_SERVICE_SOURCE)
        self.assertIn("grade_tossup(question, typed_answer)", CHALLENGE_SERVICE_SOURCE)


class ChallengeRouteContractTests(unittest.TestCase):
    def test_challenge_endpoints_require_a_signed_in_user(self):
        client = TestClient(ui_service.app)
        for path in ("/api/challenges/abc123", "/api/challenges/abc123/next", "/api/challenges/abc123/board"):
            response = client.get(path)
            self.assertIn(response.status_code, (401, 503),
                          f"{path} must require a signed-in user")

    def test_a_spent_attempt_is_a_conflict_not_a_bad_request(self):
        self.assertIn("status_code=409", UI_SERVICE_SOURCE)

    def test_seeding_is_refused_in_production(self):
        self.assertIn('if APP_ENV == "production":', UI_SERVICE_SOURCE)
        self.assertIn('raise HTTPException(status_code=404, detail="Not found")', UI_SERVICE_SOURCE)

    def test_the_fourth_station_is_wired_into_the_shell(self):
        self.assertIn('id="slide-13"', INDEX_SOURCE)
        self.assertIn('id="nav-btn-13"', INDEX_SOURCE)
        self.assertIn('data-roles="all"', INDEX_SOURCE)
        self.assertIn('<script src="/static/js/buzzpoint.js"></script>', INDEX_SOURCE)

    def test_members_sign_in_anonymously_and_are_told_if_it_is_disabled(self):
        self.assertIn("signInAnonymously", AUTH_SOURCE)
        self.assertIn("window.signInAnonymouslyForBoard", AUTH_SOURCE)
        self.assertIn("Anonymous sign-in is not enabled", AUTH_SOURCE)


if __name__ == "__main__":
    unittest.main()
