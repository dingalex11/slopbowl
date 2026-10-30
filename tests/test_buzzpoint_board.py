"""Buzzpoint Board: the word tracker's contract, the attempt lock, and the board's safety.

The tracker and run flow live in the browser, so — like the other frontend tests — those
behaviours are pinned by asserting on the source (there is no JS runtime in the suite),
while the pure logic (word offsets, grading, run progression, the attempt lock) is
unit-tested directly against ``core``.

The non-negotiable primitive under test: track every word the TTS reads, and when a buzz
fires, log the word being spoken at that instant as the buzz-point.
"""

import json
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from core import challenge, grading, words
from services import ui_service

ROOT = Path(__file__).resolve().parents[1]
BUZZPOINT_SOURCE = (ROOT / "static/js/buzzpoint.js").read_text(encoding="utf-8")
SOLO_SOURCE = (ROOT / "static/js/solo.js").read_text(encoding="utf-8")
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

    def test_a_long_answer_tolerates_a_bounded_typo(self):
        # Bounded on purpose: a slip in a long word is forgiven, but the allowance is far
        # too small to reach a different word of the same length.
        self.assertTrue(grading.judge_keyword("mitochondria", "mitocondria"))
        self.assertTrue(grading.judge_keyword("NUCLEUS", "nucles"))
        self.assertFalse(grading.judge_keyword("mitochondria", "chloroplast"))
        self.assertFalse(grading.judge_keyword("NUCLEUS", "nucleolus"))

    def test_a_short_answer_must_be_spelled_out(self):
        # A short answer has too few characters for a typo to stay unambiguous, so the
        # previous five-character-opening rule is gone: "tids" is not TIDES.
        self.assertTrue(grading.judge_keyword("TIDES", "tide"))
        self.assertFalse(grading.judge_keyword("TIDES", "tids"))
        self.assertFalse(grading.judge_keyword("IRON", "environment"))

    def test_a_regular_plural_is_the_same_answer(self):
        self.assertTrue(grading.judge_keyword("NUCLEUS", "nuclei"))
        self.assertTrue(grading.judge_keyword("PLASTID", "plastids"))
        self.assertTrue(grading.judge_keyword("RADII", "radius"))

    def test_multi_word_answer_needs_every_significant_word(self):
        self.assertTrue(grading.judge_keyword("double helix", "it is a double helix"))
        self.assertFalse(grading.judge_keyword("double helix", "just helix"))

    def test_an_empty_response_is_never_correct(self):
        self.assertFalse(grading.judge_keyword("nucleus", ""))
        self.assertFalse(grading.grade_tossup({"tossup_answer": "nucleus"}, ""))

    def test_bonus_grading_uses_the_bonus_answer(self):
        question = {"bonus_answer": "HEMOGLOBIN"}
        self.assertTrue(grading.grade_bonus(question, "hemoglobin"))
        self.assertFalse(grading.grade_bonus(question, "nucleus"))


class AcceptAlternativeTests(unittest.TestCase):
    """Packet alternatives: ``6,000 (ACCEPT: 6 x 10^3)`` lists two correct answers.

    The parenthetical is part of the packet line, not part of the answer, so it must be
    parsed out rather than folded into the text being matched. Measured before this existed:
    the real packet line below scored BOTH of its own answers as wrong.
    """

    def test_a_parenthetical_alternative_is_a_correct_answer(self):
        line = "6,000 (ACCEPT: 6 \u00d7 103 )"
        self.assertTrue(grading.judge_keyword(line, "6,000"))
        self.assertTrue(grading.judge_keyword(line, "6000"))
        self.assertTrue(grading.judge_keyword(line, "6 x 10^3"))
        self.assertTrue(grading.judge_keyword(line, "6 times 10 cubed"))
        self.assertFalse(grading.judge_keyword(line, "6.2 kilograms"))
        self.assertFalse(grading.judge_keyword(line, "200"))

    def test_a_whole_line_written_as_an_alternative_is_accepted(self):
        self.assertTrue(grading.judge_keyword("ACCEPT: 90 NORTH", "90 north"))

    def test_several_alternatives_in_one_group(self):
        self.assertTrue(grading.judge_keyword("RADIUS (ACCEPT: RADII, RADIUS)", "radii"))
        self.assertTrue(grading.judge_keyword("RADIUS (ACCEPT: RADII, RADIUS)", "radius"))

    def test_a_comma_inside_a_number_is_not_an_alternative_separator(self):
        self.assertTrue(grading.judge_keyword("THICKNESS (ACCEPT: 1,000)", "1000"))

    def test_a_semicolon_line_lists_equally_correct_answers(self):
        self.assertTrue(grading.judge_keyword("MACH; KNOTS", "knots"))
        self.assertTrue(grading.judge_keyword("MACH; KNOTS", "mach"))
        self.assertTrue(grading.judge_keyword("HECTARE; ACRE", "acre"))

    def test_an_annotation_is_not_part_of_the_answer(self):
        self.assertTrue(grading.judge_keyword("HEAT AND PRESSURE (in any order)", "pressure and heat"))
        line = "13 (Solution: 5 2 + 12 2 = c 2 ; c = 13)"
        self.assertTrue(grading.judge_keyword(line, "13"))
        self.assertFalse(grading.judge_keyword(line, "c"))

    def test_every_answer_line_in_the_shipped_packet_grades_itself_correct(self):
        # A packet's own answer must always be accepted for its own question.
        bank = json.loads((ROOT / "questions/m_round01_bank.json").read_text(encoding="utf-8"))
        for index, question in enumerate(bank):
            with self.subTest(question=index + 1, field="tossup_answer"):
                self.assertTrue(grading.grade_tossup(question, question["tossup_answer"]))
            with self.subTest(question=index + 1, field="bonus_answer"):
                self.assertTrue(grading.grade_bonus(question, question["bonus_answer"]))


class NumericGradingTests(unittest.TestCase):
    """A numeric answer is decided by value, never by the digits appearing in the reply."""

    def test_the_same_number_written_differently_is_correct(self):
        self.assertTrue(grading.judge_keyword("6000", "6,000"))
        self.assertTrue(grading.judge_keyword("540,000", "540000"))
        self.assertTrue(grading.judge_keyword("6000", "6 x 10^3"))
        self.assertTrue(grading.judge_keyword("30", "thirty"))

    def test_a_unit_may_be_stated_or_left_out(self):
        # NSB's appendix: units do not need to be stated, and if stated must be correct.
        self.assertTrue(grading.judge_keyword("6000", "6000 grams"))
        self.assertTrue(grading.judge_keyword("6000", "6 x 10^3 grams"))
        self.assertTrue(grading.judge_keyword("90", "90 degrees"))

    def test_a_different_number_is_never_correct(self):
        self.assertFalse(grading.judge_keyword("30", "300"))
        self.assertFalse(grading.judge_keyword("32", "3.2"))
        self.assertFalse(grading.judge_keyword("540,000", "5400"))

    def test_a_number_inside_a_longer_expression_is_not_the_answer(self):
        # Containment must not decide a numeric answer: 6 is not "6 x 10^3".
        self.assertFalse(grading.judge_keyword("6", "6 x 10^3"))
        self.assertFalse(grading.judge_keyword("13", "5 2 + 12 2"))

    def test_a_qualifier_that_means_something_is_never_dropped(self):
        # "90 south" must not satisfy a packet answer of 90 NORTH.
        line = "90 (ACCEPT: 90\u00ba NORTH)"
        self.assertTrue(grading.judge_keyword(line, "90 degrees north"))
        self.assertFalse(grading.judge_keyword(line, "90 south"))


class MultipleChoiceRuleTests(unittest.TestCase):
    """NSB Rule 3-3 (letter or verbal answer) and Rule 3-6 (both, if both are given)."""

    QUESTION = {"type": "MC", "tossup_answer": "Y) SEVEN-TENTHS"}

    def test_the_letter_or_the_choice_text_is_correct(self):
        self.assertTrue(grading.grade_tossup(self.QUESTION, "Y"))
        self.assertTrue(grading.grade_tossup(self.QUESTION, "y"))
        self.assertTrue(grading.grade_tossup(self.QUESTION, "seven-tenths"))

    def test_case_and_punctuation_slide_but_the_wording_must_be_exact(self):
        self.assertTrue(grading.grade_tossup(self.QUESTION, "Seven Tenths!"))
        self.assertFalse(grading.grade_tossup(self.QUESTION, "seven tenth"))
        self.assertFalse(grading.grade_tossup(self.QUESTION, "tenths"))

    def test_when_both_are_given_both_must_be_correct(self):
        self.assertTrue(grading.grade_tossup(self.QUESTION, "Y) seven tenths"))
        self.assertFalse(grading.grade_tossup(self.QUESTION, "Y) ten"))
        self.assertFalse(grading.grade_tossup(self.QUESTION, "X) seven tenths"))

    def test_only_the_official_w_x_y_z_labels_are_letters(self):
        self.assertFalse(grading.grade_tossup(self.QUESTION, "X"))
        self.assertFalse(grading.grade_tossup(self.QUESTION, "A"))
        self.assertFalse(grading.grade_tossup(self.QUESTION, "B"))

    def test_a_choice_written_as_a_digit_takes_its_spelled_form(self):
        question = {"type": "MC", "tossup_answer": "W) 0"}
        self.assertTrue(grading.grade_tossup(question, "W"))
        self.assertTrue(grading.grade_tossup(question, "0"))
        self.assertTrue(grading.grade_tossup(question, "zero"))
        self.assertFalse(grading.grade_tossup(question, "X"))

    def test_a_short_answer_beginning_with_a_choice_letter_is_not_a_letter_answer(self):
        # "X-RAY" is the answer, not option X, so it is graded as text.
        self.assertTrue(grading.grade_tossup({"tossup_answer": "X-RAY"}, "xray"))
        self.assertFalse(grading.grade_tossup({"tossup_answer": "X-RAY"}, "W"))


class SoloJudgeMirrorTests(unittest.TestCase):
    """``core/grading.py`` is the reference; ``soloJudgeAnswer`` must not drift from it.

    There is no JS runtime in the suite, so the mirror is pinned by asserting on its source
    the way the other frontend tests do. The behaviour of both sides was verified against
    the same case table (46 cases) before this was written.
    """

    def test_the_solo_judge_keeps_the_same_rule_functions(self):
        for name in (
            "soloCandidateAnswers", "soloJudgeKeyword", "soloJudgeMultipleChoice",
            "soloNumericReplyMatches", "soloReplyNumber", "soloMatchesCandidate",
            "soloTypoAllowance", "soloPhrasesEqual",
        ):
            with self.subTest(function=name):
                self.assertIn(f"function {name}(", SOLO_SOURCE)

    def test_the_solo_judge_reads_the_same_packet_markers(self):
        # The ACCEPT group, the annotation parenthetical, the W/X/Y/Z choice line, and the
        # bounded typo bands must all be present on both sides.
        self.assertIn("accept", SOLO_SOURCE.lower())
        self.assertIn("SOLO_MC_LINE", SOLO_SOURCE)
        self.assertIn("SOLO_UNIT_WORDS", SOLO_SOURCE)
        for band in ("if (word.length < 6) return 0;", "if (word.length < 9) return 1;"):
            with self.subTest(band=band):
                self.assertIn(band, SOLO_SOURCE)

    def test_the_solo_judge_is_wired_into_the_drill(self):
        self.assertIn("soloJudgeAnswer(entry.question, response)", SOLO_SOURCE)


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


class ChallengeReportTests(unittest.TestCase):
    """A member's flag names the attempt; only the captain changes its verdict."""

    def _run(self):
        run = challenge.create_run_doc("c", "u", "n", 1, "now")
        record = challenge.build_tossup_record(
            {"category": "Life Science", "tossup_answer": "NUCLEUS"},
            0, {"word": "the"}, "organelle", "auto-wrong", "answered", None,
        )
        challenge.apply_bonus_to_record(record, "hemoglobin", "auto-wrong", False)
        run["questions"].append(record)
        return run

    def test_a_report_is_idempotent_until_it_is_resolved(self):
        run = self._run()
        first = challenge.add_report(run, 0, "tossup", "the key says nucleus", "t1")
        again = challenge.add_report(run, 0, "tossup", "duplicate click", "t2")
        self.assertEqual(first, again)
        self.assertEqual(len(run["reports"]), 1)
        self.assertFalse(run["reports"][0]["resolved"])

    def test_a_bonus_report_needs_a_bonus_answer(self):
        run = self._run()
        with self.assertRaises(ValueError):
            challenge.add_report(run, 1, "tossup", "", "t")
        record = run["questions"][0]
        record["bonusVerdict"] = None
        with self.assertRaises(ValueError):
            challenge.add_report(run, 0, "bonus", "", "t")

    def test_the_captain_verdict_replaces_the_automatic_one(self):
        run = self._run()
        challenge.add_report(run, 0, "tossup", "", "t")
        challenge.add_report(run, 0, "bonus", "", "t")
        verdict = challenge.resolve_report(run, 0, "tossup", True, "t2", "captain")
        bonus = challenge.resolve_report(run, 0, "bonus", False, "t2", "captain")
        self.assertEqual(verdict, "captain-correct")
        self.assertEqual(bonus, "captain-wrong")
        self.assertEqual(run["questions"][0]["verdict"], "captain-correct")
        self.assertEqual(run["questions"][0]["bonusVerdict"], "captain-wrong")
        self.assertTrue(all(report["resolved"] for report in run["reports"]))
        self.assertEqual(len(run["judgements"]), 2)
        self.assertEqual(run["judgements"][0]["judgedBy"], "captain")


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


class BuzzpointLifetimeTests(unittest.TestCase):
    """The buzzpoint lives for one question: its read, then its five seconds.

    Measured before this: the button was enabled when the read *finished* and disabled forever
    after the first buzz \u2014 so from the second question on, the one control meant for buzzing
    could not be clicked mid-read at all, while an already-answered question could still be
    buzzed. It now follows the question's whole lifetime, and the post-read window has a
    visible countdown instead of being inferred from a button that stays enabled.

    A buzz in that window was heard against the whole question, so it is captured as an 'end'
    buzz with no word and no word index: the board must never mark the final word as if the
    member had buzzed before hearing it.
    """

    def test_the_buzz_button_opens_with_the_read(self):
        body = function_source(BUZZPOINT_SOURCE, "bukStartQuestion")
        self.assertIn("if (question.phase !== 'bonus') bukOpenBuzzpoint();", body)
        opener = function_source(BUZZPOINT_SOURCE, "bukOpenBuzzpoint")
        self.assertIn("buzzBtn.disabled = false;", opener)
        self.assertIn("bukRun.windowOpen = true;", opener)

    def test_the_read_ends_in_a_five_second_buzz_window(self):
        self.assertIn("const BUK_TOSSUP_WINDOW_S = 5.0;", BUZZPOINT_SOURCE)
        finish = function_source(BUZZPOINT_SOURCE, "bukFinishDelivery")
        self.assertIn("bukStartBuzzDeadline();", finish)
        # The read does not score the toss-up: the window's expiry does.
        self.assertNotIn("bukSubmitToServer('', null, true, 'expired');", finish)
        window = function_source(BUZZPOINT_SOURCE, "bukStartBuzzDeadline")
        self.assertIn("const deadline = performance.now() + BUK_TOSSUP_WINDOW_S * 1000;", window)
        self.assertIn("bukEndBuzzpoint();", window)
        self.assertIn("bukSubmitToServer('', null, true, 'expired');", window)
        # The countdown is drawn, so the open window is visible rather than assumed.
        self.assertIn("buk-buzz-timer", window)
        self.assertIn("bukRun.windowTimer = setInterval(tick, 100);", window)

    def test_a_finished_read_is_still_buzzable_until_the_window_closes(self):
        body = function_source(BUZZPOINT_SOURCE, "bukBuzz")
        # The window opens the button without a read running behind it.
        self.assertIn("if (!bukRun.windowOpen && !bukRun.delivering) return false;", body)
        self.assertIn("const atEnd = !bukRun.delivering && bukRun.wordStreamDone && bukRun.speechDone;", body)
        self.assertIn("snapshotAtEnd()", body)
        # A hidden answer input keeps focus after its panel closes; Space must still buzz.
        self.assertIn("&& active.offsetParent !== null;", BUZZPOINT_SOURCE)
        self.assertIn("if (!typing && bukRun.question && !bukRun.answered && (bukRun.windowOpen || bukRun.delivering))", BUZZPOINT_SOURCE)

    def test_an_end_buzz_is_marked_at_the_end_not_on_the_last_word(self):
        end = BUZZPOINT_SOURCE[BUZZPOINT_SOURCE.index("function snapshotAtEnd()")
                               :BUZZPOINT_SOURCE.index("// Flag BEFORE speechSynthesis.cancel()")]
        self.assertIn("word: null,", end)
        self.assertIn("wordIndex: null,", end)
        self.assertIn("charIndex: null,", end)
        self.assertIn("captureSource: 'end',", end)
        self.assertIn("wordsHeard: totalWords,", end)
        # The member sees [end] where a word would sit, and the export says the same.
        body = function_source(BUZZPOINT_SOURCE, "bukBuzz")
        self.assertIn("? '[end]'", body)
        self.assertIn("rec.buzz.captureSource === 'end'", BUZZPOINT_SOURCE)
        self.assertIn("r.captureSource === 'end' ? '[end]'", BUZZPOINT_SOURCE)

    def test_the_bonus_read_opens_the_twenty_second_answer_window(self):
        self.assertIn("const BUK_BONUS_WINDOW_S = 20.0;", BUZZPOINT_SOURCE)
        finish = function_source(BUZZPOINT_SOURCE, "bukFinishDelivery")
        self.assertIn("bukOpenAnswerPanel(BUK_BONUS_WINDOW_S, 'bonus');", finish)
        panel = function_source(BUZZPOINT_SOURCE, "bukOpenAnswerPanel")
        self.assertIn("const deadline = performance.now() + seconds * 1000;", panel)
        # The window's full length shows at once instead of the panel's 5.0s placeholder.
        self.assertIn("label.textContent = `${seconds.toFixed(1)}s`;", panel)
        # Time running out submits the box, and the server grades it like any answer.
        self.assertIn("const typed = input ? input.value : '';", panel)
        self.assertIn("const hasText = Boolean(typed.trim());", panel)
        self.assertIn("bukSubmitToServer(typed, bukRun.pendingBuzz, !hasText, hasText ? 'timeout' : 'expired');", panel)
        # Both windows have somewhere on the run screen to be seen.
        self.assertIn('id="buk-buzz-timer"', INDEX_SOURCE)
        self.assertIn('id="buk-answer-title"', INDEX_SOURCE)

    def test_an_answered_question_cannot_be_buzzed_again(self):
        submit = function_source(BUZZPOINT_SOURCE, "bukSubmitToServer")
        self.assertIn("bukRun.answered = true;", submit)
        self.assertIn("bukEndBuzzpoint();", submit)
        self.assertLess(submit.index("if (!bukRun.question || bukRun.submitting) return;"),
                        submit.index("bukRun.answered = true;"))

    def test_a_new_question_clears_the_previous_buzzpoint(self):
        body = function_source(BUZZPOINT_SOURCE, "bukStartQuestion")
        self.assertIn("bukResetBuzzpoint();", body)
        reset = function_source(BUZZPOINT_SOURCE, "bukResetBuzzpoint")
        self.assertIn("bukEndBuzzpoint();", reset)
        self.assertIn("bukRun.pendingBuzz = null;", reset)
        self.assertIn("wordEl.textContent = '';", reset)
        self.assertIn("srcEl.textContent = '';", reset)
        # The previous question's countdown must not keep ticking into this one.
        closer = function_source(BUZZPOINT_SOURCE, "bukEndBuzzpoint")
        self.assertIn("clearInterval(bukRun.windowTimer)", closer)
        self.assertIn("timerEl.textContent = '';", closer)

    def test_the_buzz_is_refused_outside_a_read(self):
        body = function_source(BUZZPOINT_SOURCE, "bukBuzz")
        self.assertIn("if (!bukRun.question || bukRun.answered) return false;", body)
        self.assertIn("if (!bukRun.windowOpen && !bukRun.delivering) return false;", body)
        # A bonus never takes a buzz.
        self.assertIn("if (bukRun.question.phase === 'bonus') return false;", body)


class EndBuzzBoardTests(unittest.TestCase):
    """An end buzz is a buzz with no word, so the board must not read it as a no-buzz."""

    def test_an_end_buzz_counts_as_a_buzz_and_not_as_a_no_buzz(self):
        self.assertIn("if (row.buzzIndex === null && row.captureSource !== 'end') {", ANALYTICS_SOURCE)
        self.assertIn("if (row.buzzIndex !== null || row.captureSource === 'end') {", ANALYTICS_SOURCE)
        # No index means no depth and no highlighted word: the final word is not marked.
        self.assertIn("buzz.buzzIndex !== null && buzz.buzzIndex - 1 === index", ANALYTICS_SOURCE)

    def test_the_end_line_is_a_count_and_opens_the_buzzer_list(self):
        card = function_source(ANALYTICS_SOURCE, "bbxQuestionCard")
        self.assertIn("const endBuzzes = question.buzzes.filter((buzz) => buzz.captureSource === 'end');", card)
        self.assertIn(r"`END \u00b7 ${endBuzzes.length}`", card)
        self.assertIn("bbxShowTooltip(endBadge, endBuzzes,", card)
        self.assertIn("endBadge.addEventListener('mouseleave', bbxHideTooltip);", card)
        # The members live in the hover card: no inline name chips in the footer.
        footer = card[card.index("// Footer:"):card.index("const answerBtn")]
        self.assertNotIn("hit.member", footer)
        self.assertNotIn("'inline-flex items-center gap-1 px-1.5", footer)
        # Runs that never buzzed are counted separately, not folded into END.
        self.assertIn(r"`NO BUZZ \u00b7 ${question.noBuzz}`", card)


class BuzzTooltipTests(unittest.TestCase):
    """The popup must name every member who buzzed on a word, each with their own verdict.

    It used to print the first member once and stack every answer under that single header,
    so the second and third buzzers saw a result that carried no name.
    """

    def test_every_buzz_gets_its_own_name_and_verdict(self):
        body = function_source(ANALYTICS_SOURCE, "bbxShowTooltip")
        self.assertIn("hits.forEach((hit, index) => {", body)
        self.assertIn("bbxAppend(row, 'span', 'font-bold text-white tracking-wide', hit.member);", body)
        self.assertIn("const hitCorrect = hit.verdict === 'correct';", body)
        self.assertIn("if (index > 0) bbxAppend(tip, 'div', 'border-t border-white/20 my-1.5');", body)
        # The single header was the bug: it named only hits[0].
        self.assertNotIn("const primary = hits[0];", body)
        self.assertNotIn("primary.member", body)

    def test_the_card_carries_only_the_name_and_verdict(self):
        body = function_source(ANALYTICS_SOURCE, "bbxShowTooltip")
        # The word already marks the position, and the verdict already implies the answer
        # and the points, so none of those rows belong on the card.
        for redundant in ("'Given Answer:'", "'Buzz Position:'", "'Point Impact:'"):
            self.assertNotIn(redundant, body)

    def test_a_wrong_end_buzz_is_incorrect_not_a_neg(self):
        body = function_source(ANALYTICS_SOURCE, "bbxShowTooltip")
        self.assertIn("hit.captureSource === 'end' ? 'Incorrect' : 'Neg'", body)

    def test_the_native_title_does_not_open_a_second_tooltip(self):
        self.assertNotIn("span.title =", ANALYTICS_SOURCE)
        self.assertIn("span.setAttribute('aria-label'", ANALYTICS_SOURCE)

    def test_the_tooltip_is_drawn_as_the_lighter_popup(self):
        self.assertIn('id="bbx-tooltip"', INDEX_SOURCE)
        self.assertIn("bg-slate-700/95", INDEX_SOURCE)
        self.assertIn("border-slate-500", INDEX_SOURCE)


class NumericalStatlineTests(unittest.TestCase):
    """The candidate statline carries the correct count and the run's overall points."""

    def test_points_use_the_same_weighting_as_the_ppth_columns(self):
        body = function_source(ANALYTICS_SOURCE, "bbxPlayerPoints")
        self.assertIn("player.correct * BBX_CORRECT_POINTS + player.negs * BBX_NEG_POINTS;", body)
        # PPTH is the same points divided by toss-ups heard, not a second formula.
        ppth = function_source(ANALYTICS_SOURCE, "bbxPlayerPpth")
        self.assertIn("return bbxPlayerPoints(player) / player.attempts;", ppth)

    def test_the_matrix_shows_correct_incorrect_and_points_columns(self):
        matrix = function_source(ANALYTICS_SOURCE, "bbxRenderMatrix")
        self.assertIn("th('Correct',", matrix)
        self.assertIn("th('Incorrect',", matrix)
        self.assertIn("th('Pts',", matrix)
        self.assertIn("bbxAppend(row, 'td', 'py-1.5 px-2 text-center font-mono text-slate-200', player.correct);", matrix)
        self.assertIn("bbxAppend(row, 'td', 'py-1.5 px-2 text-center font-mono text-slate-400', player.incorrect);", matrix)
        self.assertIn("const points = bbxPlayerPoints(player);", matrix)
        self.assertIn("pointsCell.style.color = points > 0 ? '#00ff66' : (points < 0 ? '#ff0055' : '#94a3b8');", matrix)

    def test_the_new_columns_can_sort(self):
        body = function_source(ANALYTICS_SOURCE, "bbxSortValue")
        self.assertIn("if (key === 'correct') return player.correct;", body)
        self.assertIn("if (key === 'incorrect') return player.incorrect;", body)
        self.assertIn("if (key === 'pts') return bbxPlayerPoints(player);", body)

    def test_a_wrong_end_buzz_never_counts_as_a_neg(self):
        """The points column follows the wording: incorrect after the read is not a neg."""
        build = function_source(ANALYTICS_SOURCE, "bbxBuildModels")
        self.assertIn("player.incorrect += 1;", build)
        self.assertIn("player.negs += 1;", build)
        stats = function_source(ANALYTICS_SOURCE, "bbxQuestionStats")
        self.assertIn("const negs = question.buzzes.filter((b) => b.verdict === 'wrong' && b.captureSource !== 'end').length;", stats)


class ChallengeTimeoutAndOverviewTests(unittest.TestCase):
    """Time running out still submits the box; the last answer stops on an overview button."""

    def test_the_typed_answer_is_submitted_when_time_runs_out(self):
        panel = function_source(BUZZPOINT_SOURCE, "bukOpenAnswerPanel")
        self.assertIn("const typed = input ? input.value : '';", panel)
        self.assertIn("const hasText = Boolean(typed.trim());", panel)
        self.assertIn("bukSubmitToServer(typed, bukRun.pendingBuzz, !hasText, hasText ? 'timeout' : 'expired');", panel)
        # A written timeout is graded; only an empty box is a plain expiry.
        self.assertIn("hasText ? 'timeout' : 'expired'", panel)

    def test_the_last_question_offers_the_overview_instead_of_skipping_to_it(self):
        advance = function_source(BUZZPOINT_SOURCE, "bukAdvance")
        self.assertIn("bukRun.atEnd = true;", advance)
        self.assertIn("nextBtn.textContent = 'Go to Overview';", advance)
        self.assertNotIn("bukFinishRun();", advance)
        nxt = function_source(BUZZPOINT_SOURCE, "bukNextQuestion")
        self.assertIn("if (bukRun.atEnd) {", nxt)
        self.assertIn("bukFinishRun();", nxt)
        self.assertIn("nextBtn.textContent = 'Next \\u2192';", advance)

    def test_the_member_can_flag_a_question_for_the_captain(self):
        self.assertIn('id="buk-report-btn"', INDEX_SOURCE)
        self.assertIn("function bukReportQuestion()", BUZZPOINT_SOURCE)
        self.assertIn("await bukApi(`/api/challenges/${encodeURIComponent(bukRun.code)}/report`", BUZZPOINT_SOURCE)
        self.assertIn("bukShowReport(bukRun.question.qIndex, bukRun.question.phase);", BUZZPOINT_SOURCE)
        self.assertIn(r"\u2691 Report question", BUZZPOINT_SOURCE)

    def test_the_board_export_carries_whose_attempt_and_which_phase_was_reported(self):
        export = function_source(BUZZPOINT_SOURCE, "bukBoardCsvRows")
        for column in ("'RunUid'", "'Reported'", "'ReportReason'"):
            self.assertIn(column, export)
        self.assertIn("run.reports", export)
        self.assertIn("!report.resolved", export)


class CaptainReviewQueueTests(unittest.TestCase):
    """The captain sees the flag and can set that attempt's verdict by hand."""

    def test_the_analytics_parser_keeps_the_reported_attempt(self):
        body = function_source(ANALYTICS_SOURCE, "bbxIngestCsv")
        self.assertIn("uid: String(row['RunUid'] || '')", body)
        self.assertIn("reported: String(row['Reported'] || '').toLowerCase()", body)
        self.assertIn("reportReason: String(row['ReportReason'] || '')", body)

    def test_a_reported_question_is_flagged_on_its_card(self):
        card = function_source(ANALYTICS_SOURCE, "bbxQuestionCard")
        self.assertIn("if (question.reportCount)", card)
        self.assertIn("reported", card)

    def test_the_review_strip_shows_what_was_answered_against_the_target(self):
        """A verdict alone cannot be reviewed: a near miss and a blank look the same."""
        body = function_source(ANALYTICS_SOURCE, "bbxRenderReports")
        self.assertIn("row.questionText", body)
        self.assertIn("answerCell('answered'", body)
        self.assertIn("answerCell('target'", body)
        self.assertIn("row.bonusTypedAnswer", body)
        self.assertIn("row.bonusAnswer", body)

    def test_the_export_carries_bonus_answers_for_bonus_reports(self):
        export = function_source(BUZZPOINT_SOURCE, "bukBoardCsvRows")
        self.assertIn("'BonusAnswer'", export)
        self.assertIn("'BonusTypedAnswer'", export)
        self.assertIn("question.bonus_answer || ''", export)
        self.assertIn("r.bonusTypedAnswer || ''", export)
        parser = function_source(ANALYTICS_SOURCE, "bbxIngestCsv")
        self.assertIn("bonusTypedAnswer: row['BonusTypedAnswer']", parser)
        self.assertIn("bonusAnswer: row['BonusAnswer']", parser)

    def test_the_captain_can_mark_the_reported_phase(self):
        body = function_source(ANALYTICS_SOURCE, "bbxRenderReports")
        self.assertIn("bbxJudgeReport(row, phase, correct)", body)
        self.assertIn("Mark correct", body)
        self.assertIn("Mark incorrect", body)
        # The row is built detached unless it is attached: the queue showed its heading and
        # nothing else, so the captain had no control to press.
        self.assertIn("list.appendChild(item);", body)
        judge = function_source(ANALYTICS_SOURCE, "bbxJudgeReport")
        self.assertIn("method: 'POST'", judge)
        self.assertIn("/judge`", judge)
        self.assertIn("qIndex: row.qNum - 1", judge)

    def test_the_queue_is_wired_into_the_station(self):
        self.assertIn('id="bbx-report-queue"', INDEX_SOURCE)
        self.assertIn('id="bbx-report-list"', INDEX_SOURCE)
        self.assertIn("bbxRenderReports();", ANALYTICS_SOURCE)

    def test_a_report_from_a_dropped_csv_still_reaches_the_queue(self):
        """The strip shows the report even when no run uid can act on it.

        Appending after the "load by code" branch left a hand-loaded CSV's queue visible but
        empty, so a captain saw the heading and none of the members' flags.
        """
        body = function_source(ANALYTICS_SOURCE, "bbxRenderReports")
        self.assertLess(body.index("list.appendChild(item);"), body.index("if (!fixable) {"))


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
        self.assertIn("bukEndBuzzpoint();", body)
        closer = function_source(BUZZPOINT_SOURCE, "bukEndBuzzpoint")
        self.assertIn("bukRun.windowOpen = false;", closer)
        self.assertIn("buzzBtn.disabled = true;", closer)
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

    def test_the_buzzboard_view_has_the_templates_compact_shell(self):
        self.assertIn('id="bbx-ingestion-drawer"', INDEX_SOURCE)
        self.assertIn('id="bbx-toggle-drawer"', INDEX_SOURCE)
        self.assertIn('id="bbx-active-code"', INDEX_SOURCE)
        self.assertIn('id="bbx-dataset-label"', INDEX_SOURCE)
        self.assertIn('id="bbx-question-subject-filters"', INDEX_SOURCE)
        self.assertIn('id="bbx-raw-columns"', INDEX_SOURCE)
        self.assertIn('Question buzzpoints', INDEX_SOURCE)
        self.assertIn('Team builder &amp; numerical data', INDEX_SOURCE)

    def test_question_cards_are_accordions_that_remember_being_opened(self):
        card = function_source(ANALYTICS_SOURCE, "bbxQuestionCard")
        self.assertIn("bbxState.openQuestions.has(question.key)", card)
        self.assertIn("bbxToggleQuestion(question.key, body, arrow)", card)
        toggle = function_source(ANALYTICS_SOURCE, "bbxToggleQuestion")
        self.assertIn("bbxState.openQuestions.add(key);", toggle)
        expand = function_source(ANALYTICS_SOURCE, "bbxExpandAllQuestions")
        self.assertIn("bbxState.openQuestions = new Set();", expand)

    def test_the_team_builder_is_a_drag_and_drop_draft(self):
        hud = function_source(ANALYTICS_SOURCE, "bbxRenderHud")
        self.assertIn("card.addEventListener('drop'", hud)
        self.assertIn("bbxAssignToTeam(key, teamId)", hud)
        matrix = function_source(ANALYTICS_SOURCE, "bbxRenderMatrix")
        self.assertIn("row.draggable = true;", matrix)
        self.assertIn("`+T${teamId}`", matrix)
        assign = function_source(ANALYTICS_SOURCE, "bbxAssignToTeam")
        self.assertIn("BBX_SQUAD_SIZE", assign)

    def test_raw_subject_columns_are_hidden_until_requested(self):
        matrix = function_source(ANALYTICS_SOURCE, "bbxRenderMatrix")
        self.assertIn("bbxState.rawColumns ? '' : 'hidden'", matrix)
        setter = function_source(ANALYTICS_SOURCE, "bbxSetRawColumns")
        self.assertIn("bbxState.rawColumns = Boolean(checked);", setter)

    def test_the_stats_use_the_whole_window(self):
        self.assertIn('id="analytics-shell"', INDEX_SOURCE)
        self.assertIn("main:has(#screen-analytics.bbx-wide)", INDEX_SOURCE)
        self.assertIn("#screen-analytics.bbx-wide", INDEX_SOURCE)
        self.assertIn("#screen-analytics.bbx-wide #analytics-shell", INDEX_SOURCE)
        self.assertIn("screen.classList.toggle('bbx-wide', !match);", ANALYTICS_SOURCE)

    def test_each_question_subject_has_its_own_colour(self):
        self.assertIn("const BBX_SUBJECT_COLORS = {", ANALYTICS_SOURCE)
        self.assertIn("Physics: '#00f3ff'", ANALYTICS_SOURCE)
        colour = function_source(ANALYTICS_SOURCE, "bbxSubjectColor")
        self.assertIn("BBX_SUBJECT_FALLBACK_COLORS[hash % BBX_SUBJECT_FALLBACK_COLORS.length]", colour)
        filters = function_source(ANALYTICS_SOURCE, "bbxRenderQuestionSubjectFilters")
        self.assertIn("const accent = bbxSubjectColor(subject);", filters)
        self.assertIn("btn.style.background = active ? accent : '#000000';", filters)
        card = function_source(ANALYTICS_SOURCE, "bbxQuestionCard")
        self.assertIn("const accent = bbxSubjectColor(subject);", card)
        self.assertIn("card.style.borderLeft = `3px solid ${accent}`;", card)
        self.assertIn("subjectChip.style.color = accent;", card)

    def test_the_team_panel_can_be_minimized(self):
        self.assertIn('id="bbx-matrix-panel"', INDEX_SOURCE)
        self.assertIn('id="bbx-team-panel"', INDEX_SOURCE)
        self.assertIn('id="bbx-team-toggle"', INDEX_SOURCE)
        toggle = function_source(ANALYTICS_SOURCE, "bbxToggleTeamPanel")
        self.assertIn("bbxState.teamsCollapsed = typeof force === 'boolean'", toggle)
        self.assertIn("panel.classList.toggle('hidden', collapsed)", toggle)
        self.assertIn("matrixPanel.style.gridColumn = collapsed ? '1 / -1' : '';", toggle)
        self.assertIn("toggle.setAttribute('aria-expanded', String(!collapsed))", toggle)


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


class MultipleChoiceReadTests(unittest.TestCase):
    """A multiple-choice stem is unreadable without its choices.

    A packet serves a choice question as a stem plus an ``options`` list, and the reader spoke
    the stem alone — so a multiple-choice toss-up stopped at "which of the following" and the
    choices were neither read nor shown. Measured on a real sample packet: every choice
    question reached the member with its choices missing.

    The choices are now part of the question text itself: the stem followed by one line per
    choice. That keeps the read as ONE word stream, so the speech, the word clock that logs the
    buzz-point word and the word-by-word reveal all keep running through the choices instead of
    stopping where a separate options panel would begin.
    """

    def test_the_question_text_carries_its_choices(self):
        body = function_source(BUZZPOINT_SOURCE, "bukReadText")
        self.assertIn("question.options", body)
        # One string: the stem, a blank line, then every choice on its own line.
        self.assertIn("`${stem}\\n\\n${options.join('\\n')}`", body)

    def test_the_read_is_composed_once_and_used_by_both_channels(self):
        body = function_source(BUZZPOINT_SOURCE, "bukStartQuestion")
        self.assertIn("const readText = bukReadText(question);", body)
        self.assertIn("bukSpeak(readText)", body)
        self.assertIn("createBuzzWordTracker(readText", body)
        self.assertIn("buzzTokenizeWords(readText)", body)
        # The stem alone must never be spoken again, and nothing may split the word stream.
        self.assertNotIn("bukSpeak(question.text", body)
        self.assertNotIn("createBuzzWordTracker(question.text", body)
        self.assertNotIn("readParts", body)

    def test_the_choices_are_drawn_in_the_question_block_on_their_own_lines(self):
        # One block keeps the line breaks, so a choice line is part of the question, not a panel.
        self.assertIn("whitespace-pre-line", INDEX_SOURCE)
        self.assertNotIn('id="buk-question-options"', INDEX_SOURCE)
        body = function_source(BUZZPOINT_SOURCE, "bukPrepareReveal")
        self.assertIn("const textEl = bukEl('buk-question-text');", body)
        # The gap (line break and choice label) is part of the word that starts it.
        self.assertIn("bukRun.readText.slice(previousEnd, word.start)", body)
        # Punctuation and a closing colon ride along with the word they follow.
        self.assertIn("const tail = bukRun.readText.slice(previousEnd);", body)

    def test_a_choice_appears_only_when_the_read_reaches_it(self):
        # The line break lives in the choice's first word, so an unread choice is fully invisible:
        # there is no separate host to switch on ahead of the read.
        body = function_source(BUZZPOINT_SOURCE, "bukPrepareReveal")
        self.assertIn("span.style.visibility = 'hidden';", body)
        reveal = function_source(BUZZPOINT_SOURCE, "bukRevealUpTo")
        self.assertIn("bukRun.wordSpans[i].style.visibility = 'visible';", reveal)
        self.assertNotIn("wordHosts", BUZZPOINT_SOURCE)

    def test_every_word_of_every_choice_is_in_the_one_word_list(self):
        """The buzz-point clock is the word list, so it must be the composed read."""
        body = function_source(BUZZPOINT_SOURCE, "bukStartQuestion")
        self.assertIn("bukRun.words = buzzTokenizeWords(readText);", body)
        self.assertIn("bukRun.readText = readText;", body)

    def test_the_export_carries_the_choices_so_a_choice_buzz_has_a_word_to_sit_on(self):
        # Analytics highlights the buzz at BuzzWordIndex in QuestionText, so the export must
        # carry the text that was actually read — otherwise an option buzz points past the end.
        body = function_source(BUZZPOINT_SOURCE, "bukBoardCsvRows")
        self.assertIn("bukReadText({", body)
        self.assertIn("question.tossup_options", body)
        # The row itself writes the composed text, never the bare stem field.
        self.assertIn("\n        questionText,\n", body)
        self.assertNotIn("\n        question.tossup_text,\n", body)

    def test_a_short_answer_question_is_read_exactly_as_before(self):
        body = function_source(BUZZPOINT_SOURCE, "bukReadText")
        # No choices means the stem, unchanged.
        self.assertIn("if (!options.length) return stem;", body)

    def test_the_choices_are_served_with_the_question_for_both_phases(self):
        bank = [{
            "tossup_text": "Which of the following is a gas: ",
            "tossup_options": ["W) oxygen", "X) hydrogen"],
            "bonus_text": "Which of these is a metal: ",
            "bonus_options": ["W) iron", "X) neon"],
        }]
        run = challenge.create_run_doc("c", "u", "n", 1, "now")
        tossup = challenge.next_question_payload(bank, run)
        self.assertEqual(tossup["options"], ["W) oxygen", "X) hydrogen"])
        run["phase"] = "bonus"
        bonus = challenge.next_question_payload(bank, run)
        self.assertEqual(bonus["options"], ["W) iron", "X) neon"])
        # The answer key still never travels with them.
        self.assertNotIn("tossup_answer", tossup)
        self.assertNotIn("bonus_answer", bonus)


if __name__ == "__main__":
    unittest.main()
