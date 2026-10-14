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
ANALYTICS_SOURCE = (ROOT / "static/js/buzzboard_analytics.js").read_text(encoding="utf-8")
ROUTER_SOURCE = (ROOT / "static/js/router.js").read_text(encoding="utf-8")
INDEX_SOURCE = (ROOT / "index.html").read_text(encoding="utf-8")
AUTH_SOURCE = (ROOT / "static/js/auth.js").read_text(encoding="utf-8")
RULES_SOURCE = (ROOT / "firestore.rules").read_text(encoding="utf-8")
UI_SERVICE_SOURCE = (ROOT / "services/ui_service.py").read_text(encoding="utf-8")
CHALLENGE_SERVICE_SOURCE = (ROOT / "services/challenge_service.py").read_text(encoding="utf-8")


def function_source(source, name):
    """The text of one top-level function, from its definition to its closing brace."""
    start = source.index(f"function {name}(")
    end = source.index("\n}\n", start) + 3
    return source[start:end]


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


class BuzzedReadFreezeTests(unittest.TestCase):
    """A buzz stops the read: the rest of the question stays off the screen while you answer.

    The browser reports a *cancelled* utterance through 'error', not 'end'. Settling on that
    error revealed the whole question the instant a member buzzed — measured in the browser:
    four of eight words on screen at the buzz, all eight a beat later. The freeze is enforced
    inside the reveal itself, so no later settle and no paced fallback can push words past the
    word the read had reached.
    """

    def test_a_cancelled_read_never_settles(self):
        """Both channels a browser uses to report a cancelled read must refuse it."""
        onend = BUZZPOINT_SOURCE[BUZZPOINT_SOURCE.index("utterance.onend")
                                 :BUZZPOINT_SOURCE.index("utterance.onerror")]
        onerror = BUZZPOINT_SOURCE[BUZZPOINT_SOURCE.index("utterance.onerror")
                                   :BUZZPOINT_SOURCE.index("const begin = performance.now();")]
        for name, body in (("onend", onend), ("onerror", onerror)):
            self.assertIn("if (bukRun.tracker && bukRun.tracker.isCancelled()) return;", body,
                          f"{name} must refuse a cancelled read")
        # Chromium's own wording for it, so the reason travels with the fix.
        self.assertIn("'interrupted'", BUZZPOINT_SOURCE)

    def test_the_read_is_frozen_before_it_is_stopped(self):
        body = function_source(BUZZPOINT_SOURCE, "bukBuzz")
        self.assertIn("bukRun.frozenAt = bukRun.revealIndex;", body)
        # Freezing after the cancel would leave a race the browser could win.
        self.assertLess(body.index("bukRun.frozenAt = bukRun.revealIndex;"),
                        body.index("bukStopDelivery();"))

    def test_the_freeze_is_enforced_in_the_shared_reveal(self):
        body = function_source(BUZZPOINT_SOURCE, "bukRevealUpTo")
        self.assertIn("const ceiling = bukRun.frozenAt === null ? last : Math.min(last, bukRun.frozenAt);", body)
        self.assertIn("Math.min(index, ceiling)", body)

    def test_the_rest_of_the_question_appears_once_the_answer_is_in(self):
        self.assertIn("function bukRevealFullQuestion()", BUZZPOINT_SOURCE)
        body = function_source(BUZZPOINT_SOURCE, "bukSubmitToServer")
        self.assertIn("bukRevealFullQuestion();", body)

    def test_each_question_starts_unfrozen(self):
        body = function_source(BUZZPOINT_SOURCE, "bukStartQuestion")
        self.assertIn("bukRun.frozenAt = null;", body)

    def test_the_paced_fallback_reveals_through_the_word_index_the_file_defines(self):
        """The fallback called a function that does not exist, so on an engine that fires no
        word boundaries the reveal threw every 120 ms and no word ever appeared."""
        self.assertIn("function buzzWordIndexAt(words, charIndex)", BUZZPOINT_SOURCE)
        self.assertNotIn("bukWordIndexAt(", BUZZPOINT_SOURCE)
        body = function_source(BUZZPOINT_SOURCE, "bukStartFallbackReveal")
        self.assertIn("bukRevealUpTo(buzzWordIndexAt(bukRun.words, elapsedSeconds * charsPerSecond));", body)
        # The paced reveal is also a buzz-point source, so the tracker must be told the word.
        self.assertIn("bukRun.tracker.noteCounted(bukRun.revealIndex);", body)


class ReadAloudUnavailableTests(unittest.TestCase):
    """A device that cannot read the question aloud must refuse the read, not fake it.

    Measured in the browser with a speech engine that accepts the utterance and never speaks
    it (no voices installed, autoplay blocked, or no TTS backend): the client declared the read
    complete 1.5s in \u2014 the whole question on screen, the buzz window open, and then a timeout
    recorded against a question nobody had heard. A buzz is supposed to log the word the member
    heard, so an unspoken question is refused and the attempt is left where it is.
    """

    def test_a_silent_engine_refuses_the_read_instead_of_settling_it(self):
        body = function_source(BUZZPOINT_SOURCE, "bukSpeak")
        # No engine at all, and an engine that never starts, both refuse.
        self.assertIn("if (!window.speechSynthesis) {\n    bukReadUnavailable();\n    return;\n  }", body)
        self.assertIn("if (performance.now() - begin > 1500) bukReadUnavailable();", body)
        # The old bail declared the read finished, which is what dumped the question.
        self.assertNotIn("if (spoke || performance.now() - begin > 1500) settle();", body)
        # A read that spoke and stopped is still recovered, so an end event is not required.
        self.assertIn("if (spoke) { settle(); return; }", body)

    def test_an_error_before_any_audio_is_a_refusal_not_a_finished_read(self):
        body = function_source(BUZZPOINT_SOURCE, "bukSpeak")
        self.assertIn("if (!spoke) { bukReadUnavailable(); return; }", body)

    def test_a_refused_read_reveals_nothing_and_records_nothing(self):
        body = function_source(BUZZPOINT_SOURCE, "bukReadUnavailable")
        self.assertIn("bukRun.windowOpen = false;", body)
        self.assertIn("buzzBtn.disabled = true;", body)
        self.assertIn("panel.classList.add('hidden');", body)
        # No answer is submitted, so the attempt stays unspent for a device that speaks.
        self.assertNotIn("bukSubmitToServer", body)
        self.assertIn("BUK_READ_ALOUD_ERROR", body)
        self.assertIn("bukStopDelivery();", body)

    def test_the_start_button_refuses_before_the_packet_begins(self):
        body = function_source(BUZZPOINT_SOURCE, "bukStartChallenge")
        self.assertIn("if (!bukCanReadAloud()) {", body)
        # Refusal has to come before the first question is fetched.
        self.assertLess(body.index("if (!bukCanReadAloud()) {"), body.index("await bukApi("))
        self.assertIn("BUK_READ_ALOUD_ERROR", body)

    def test_an_empty_voice_list_is_not_grounds_to_refuse(self):
        """Chrome and iOS report no voices until speech is first used, so the read is the test."""
        body = function_source(BUZZPOINT_SOURCE, "bukCanReadAloud")
        self.assertIn("Boolean(window.speechSynthesis)", body)
        self.assertIn("typeof window.SpeechSynthesisUtterance === 'function'", body)
        self.assertNotIn("getVoices", body)


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
        # The board is reached by its own address from the home page. The bottom dock that
        # used to carry the link is gone — screens are links now — so the shell test pins the
        # link and its URL rather than a dock button.
        self.assertIn('href="#/board"', INDEX_SOURCE)
        self.assertIn('data-route="board"', INDEX_SOURCE)
        self.assertIn('<script src="/static/js/buzzpoint.js"></script>', INDEX_SOURCE)

    def test_members_sign_in_anonymously_and_are_told_if_it_is_disabled(self):
        self.assertIn("signInAnonymously", AUTH_SOURCE)
        self.assertIn("window.signInAnonymouslyForBoard", AUTH_SOURCE)
        self.assertIn("Anonymous sign-in is not enabled", AUTH_SOURCE)


class BoardByCodeTests(unittest.TestCase):
    """The captain's board already exists on the server, so Analytics reads it by code.

    The guarantee under test: a board fetched by code and a board uploaded as CSV are fed by
    the same two pieces — the board's own exporter and the file parser — so the two routes
    cannot analyse the same runs differently. And because the board is captain-only, the
    failure has to say so rather than look like an empty board.
    """

    def test_the_station_offers_a_load_by_code_control(self):
        self.assertIn('id="bbx-challenge-code"', INDEX_SOURCE)
        self.assertIn('id="bbx-load-board"', INDEX_SOURCE)
        self.assertIn("async function bbxLoadChallengeBoard(code)", ANALYTICS_SOURCE)
        self.assertIn("bbxLoadBoardBtn.addEventListener('click'", ANALYTICS_SOURCE)
        self.assertIn("if (event.key === 'Enter')", ANALYTICS_SOURCE)

    def test_the_board_is_fetched_as_the_captain(self):
        self.assertIn("await bbxApi(`/api/challenges/${encodeURIComponent(clean)}/board`)", ANALYTICS_SOURCE)
        self.assertIn("if (token) headers.Authorization = `Bearer ${token}`;", ANALYTICS_SOURCE)
        self.assertIn('@app.get("/api/challenges/{code}/board")', UI_SERVICE_SOURCE)

    def test_a_fetched_board_is_parsed_by_the_upload_parser(self):
        # The exporter the download button uses, then the parser a dropped file uses.
        self.assertIn("function bbxBoardCsvText(board)", ANALYTICS_SOURCE)
        self.assertIn("return bukBoardCsvText(board.runs || [], board.questions || []);", ANALYTICS_SOURCE)
        self.assertIn("const csv = bbxBoardCsvText(board);", ANALYTICS_SOURCE)
        # A fetched board is tagged with the code that produced it on the way to the view.
        self.assertIn("const added = bbxIngestBoardCsv(clean, csv);", ANALYTICS_SOURCE)
        self.assertIn("const added = bbxIngestCsv(csvText);", ANALYTICS_SOURCE)
        # Both routes finish in one place, so the badge and the status cannot disagree.
        self.assertIn("bbxFinishIngest(accepted);", ANALYTICS_SOURCE)
        self.assertIn("bbxFinishIngest(added);", ANALYTICS_SOURCE)
        self.assertIn("function bbxFinishIngest(accepted)", ANALYTICS_SOURCE)

    def test_the_code_is_validated_the_way_the_server_validates_it(self):
        self.assertIn("/^[a-z0-9]{4,8}$/", ANALYTICS_SOURCE)
        self.assertIn('_CHALLENGE_CODE_RE = re.compile(r"^[a-z0-9]{4,8}$")', UI_SERVICE_SOURCE)

    def test_an_unauthorised_or_unknown_board_says_which_it_was(self):
        self.assertIn("if (error.status === 403 || error.status === 404)", ANALYTICS_SOURCE)
        self.assertIn("if (error.status === 401) return 'Sign in as the captain to load a board by code.';", ANALYTICS_SOURCE)
        self.assertIn("if (error.status === 503) return detail ||", ANALYTICS_SOURCE)
        # The server answers 403 for "not the captain", and the client shows its wording.
        self.assertIn('raise HTTPException(status_code=403, detail=str(exc))', UI_SERVICE_SOURCE)

    def test_a_board_with_no_runs_says_so_instead_of_claiming_success(self):
        self.assertIn("if (!added) {", ANALYTICS_SOURCE)
        self.assertIn("has no runs yet", ANALYTICS_SOURCE)

    def test_the_same_board_is_not_loaded_twice(self):
        self.assertIn("const key = `challenge:${clean}`;", ANALYTICS_SOURCE)
        self.assertIn("if (bbxState.files.has(key)) {", ANALYTICS_SOURCE)
        # The router re-dispatching the same address must not fetch it again.
        self.assertIn("if (bbxState.files.has(`challenge:${code}`)) return;", ANALYTICS_SOURCE)

    def test_the_address_remembers_which_board_is_open(self):
        self.assertIn("window.routerReplace(`#/analytics/${encodeURIComponent(clean)}`)", ANALYTICS_SOURCE)
        self.assertIn("if (name === 'analytics' && p.code) return `#/analytics/${encodeURIComponent(p.code)}`;", ROUTER_SOURCE)
        self.assertIn("params.code = String(parts[1] || '').toLowerCase();", ROUTER_SOURCE)
        self.assertIn("document.addEventListener('app:view'", ANALYTICS_SOURCE)
        # The URL must open the buzzboard panel, not just switch the page inside it.
        self.assertIn("showAnalyticsView('buzzboard');", ANALYTICS_SOURCE)
        self.assertIn("bbxShowPage('questions');", ANALYTICS_SOURCE)

    def test_the_board_still_downloads_for_a_spreadsheet(self):
        self.assertIn("Download it for a spreadsheet, or read it in Analytics", BUZZPOINT_SOURCE)
        self.assertIn("btn.textContent = 'Download CSV';", BUZZPOINT_SOURCE)


class LiveBoardTests(unittest.TestCase):
    """A loaded board follows the challenge: members' runs land without a manual reload.

    The guarantee under test: a refresh is a *replacement* of that one board's rows, not an
    append. Rows are tagged with the code that produced them, so re-reading a board that has
    grown cannot double-count a run, and a CSV dropped by hand is never touched by a refresh.
    And because it is a poll, it has to know when to be quiet: off the station, in a hidden
    tab, and whenever the captain pauses it.
    """

    def test_a_loaded_board_is_re_read_on_an_interval(self):
        self.assertIn("const BBX_LIVE_REFRESH_MS = 15000;", ANALYTICS_SOURCE)
        self.assertIn("bbxLive.timer = setInterval(bbxLiveTick, BBX_LIVE_REFRESH_MS);", ANALYTICS_SOURCE)
        self.assertIn("async function bbxLiveTick()", ANALYTICS_SOURCE)
        # Loading a board by code is what arms it.
        self.assertIn("bbxLiveStart();          // a no-op while the captain has the poll paused",
                      ANALYTICS_SOURCE)

    def test_only_this_board_s_own_rows_are_replaced(self):
        self.assertIn("function bbxIngestBoardCsv(code, csvText) {", ANALYTICS_SOURCE)
        self.assertIn("bbxState.rows[i].source = source;", ANALYTICS_SOURCE)
        self.assertIn("bbxState.rows = bbxState.rows.filter((row) => row.source !== source);",
                      ANALYTICS_SOURCE)

    def test_an_unchanged_board_is_not_redrawn(self):
        # A poll that redrew the same board would fight the captain's draft and scroll.
        self.assertIn("if (csv === entry.csv) {", ANALYTICS_SOURCE)
        self.assertIn("bbxLive.boards.set(clean, { csv: csv, playing: bbxPlayingRuns(board) });",
                      ANALYTICS_SOURCE)

    def test_a_refresh_is_never_served_from_the_browser_cache(self):
        self.assertIn("cache: 'no-store'", ANALYTICS_SOURCE)

    def test_the_captain_can_pause_and_resume_the_poll(self):
        self.assertIn('id="bbx-live-toggle"', INDEX_SOURCE)
        self.assertIn("function bbxLivePause() { bbxLive.paused = true; bbxLiveStop(); }", ANALYTICS_SOURCE)
        self.assertIn("function bbxLiveResume() {", ANALYTICS_SOURCE)
        self.assertIn("if (bbxLive.paused) bbxLiveResume(); else bbxLivePause();", ANALYTICS_SOURCE)
        # The pill reports the poll's own state, so it cannot claim to be live while stopped.
        self.assertIn("function bbxRenderLivePill()", ANALYTICS_SOURCE)
        self.assertIn("pill.textContent = `Live \\u00b7 ${bbxLivePlaying()} playing \\u00b7 ${when}`;",
                      ANALYTICS_SOURCE)

    def test_the_poll_is_quiet_off_the_station_and_in_a_hidden_tab(self):
        self.assertIn("document.visibilityState === 'hidden'", ANALYTICS_SOURCE)
        self.assertIn("document.body.getAttribute('data-view') !== 'analytics'", ANALYTICS_SOURCE)
        self.assertIn("if (panel && panel.classList.contains('hidden')) return;", ANALYTICS_SOURCE)
        # Leaving the station stops the interval (the boards stay loaded for the way back).
        self.assertIn("if (detail.name !== 'analytics') {", ANALYTICS_SOURCE)
        self.assertIn("bbxLiveStop();", ANALYTICS_SOURCE)
        self.assertIn("bbxLiveStart();", ANALYTICS_SOURCE)

    def test_a_board_that_can_no_longer_be_read_stops_being_followed(self):
        self.assertIn("bbxLive.paused = true;", ANALYTICS_SOURCE)
        # Clear Data forgets the boards too, so nothing re-appears behind the captain's back.
        self.assertIn("bbxLive.boards.clear();", ANALYTICS_SOURCE)

    def test_a_board_with_no_runs_is_followed_until_the_first_one_lands(self):
        self.assertIn("it will fill in as members play", ANALYTICS_SOURCE)


if __name__ == "__main__":
    unittest.main()
