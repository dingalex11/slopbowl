import base64
import unittest

from core.validation import validate_client_event, validate_question_bank, validate_room_id


class ValidationTests(unittest.TestCase):
    def test_valid_room_id(self):
        self.assertTrue(validate_room_id("semifinals-a"))
        self.assertFalse(validate_room_id("../admin"))
        self.assertFalse(validate_room_id(""))

    def test_question_bank_schema_and_limits(self):
        valid = [{"tossup_text": "Question", "tossup_answer": "Answer"}]
        self.assertEqual(validate_question_bank(valid), valid)
        with self.assertRaises(ValueError):
            validate_question_bank([{"tossup_text": "Missing answer"}])
        with self.assertRaises(ValueError):
            validate_question_bank([])
        with self.assertRaises(ValueError):
            validate_question_bank([{"tossup_text": "x" * 12001, "tossup_answer": "Answer"}])

    def test_question_bank_accepts_both_visual_flags(self):
        bank = [{"tossup_text": "Q", "tossup_answer": "A", "tossup_visual": True, "bonus_visual": False}]
        self.assertEqual(validate_question_bank(bank), bank)
        with self.assertRaises(ValueError):
            validate_question_bank([{"tossup_text": "Q", "tossup_answer": "A", "bonus_visual": "yes"}])
        with self.assertRaises(ValueError):
            validate_question_bank([{"tossup_text": "Q", "tossup_answer": "A", "round": 1}])

    def test_events_reject_unknown_or_invalid_payloads(self):
        self.assertEqual(validate_client_event("BUZZ", {"team": "Team A", "player": "A-1"})["team"], "Team A")
        with self.assertRaises(ValueError):
            validate_client_event("BUZZ", {"team": "Team C", "player": "A-1"})
        with self.assertRaises(ValueError):
            validate_client_event("LOAD_BANK", {"bank": []})
        with self.assertRaises(ValueError):
            validate_client_event("UNKNOWN", {})
        self.assertEqual(validate_client_event("BUZZ", {"team": "Team A", "player": "Manual-A"})["player"], "Manual-A")
        with self.assertRaises(ValueError):
            validate_client_event("ADJUST_SCORE", {"team": "Team A", "delta": 1_000_000})

    def test_roster_values_reject_control_characters_and_extra_keys(self):
        self.assertEqual(
            validate_client_event("SAVE_ROSTER", {"teams": {"Team A": "Ravens"}, "players": {"A-1": "Ada"}})["teams"]["Team A"],
            "Ravens",
        )
        with self.assertRaises(ValueError):
            validate_client_event("SAVE_ROSTER", {"teams": {"Team A": "Bad\nName"}, "players": {}})
        with self.assertRaises(ValueError):
            validate_client_event("SAVE_ROSTER", {"teams": {"unlisted": "Team"}, "players": {}})

    def test_calibration_regions_must_be_bounded_and_positive(self):
        self.assertTrue(validate_client_event("SAVE_CALIBRATION", {"A-1": [0.1, 0.1, 0.4, 0.4]}))
        with self.assertRaises(ValueError):
            validate_client_event("SAVE_CALIBRATION", {"A-1": [0.5, 0.1, 0.4, 0.4]})
        with self.assertRaises(ValueError):
            validate_client_event("SAVE_CALIBRATION", {"A-1": [0, 0, 1.1, 1]})

    def test_frame_rejects_non_jpeg_and_oversized_payload(self):
        valid_jpeg_data_url = "data:image/jpeg;base64," + base64.b64encode(b"jpeg").decode()
        self.assertEqual(validate_client_event("PROCESS_FRAME", {"frame_data": valid_jpeg_data_url})["frame_data"], valid_jpeg_data_url)
        with self.assertRaises(ValueError):
            validate_client_event("PROCESS_FRAME", {"frame_data": "data:image/png;base64,AAAA"})
        with self.assertRaises(ValueError):
            validate_client_event("PROCESS_FRAME", {"frame_data": "data:image/jpeg;base64," + "A" * 400_000})


if __name__ == "__main__":
    unittest.main()
