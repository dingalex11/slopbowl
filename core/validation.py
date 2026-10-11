import base64
import binascii
import json
import re
from typing import Any

from core.config import MAX_AUDIO_BYTES, MAX_FRAME_BYTES, MAX_QUESTIONS_PER_BANK

ROOM_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
EVENT_TYPES = {
    "PING", "BUZZ", "READING_DONE", "ANSWER_AUDIO", "PROCESS_FRAME",
    "SAVE_ROSTER", "LOAD_BANK", "SAVE_CALIBRATION", "RESET_CALIBRATION",
    "CALIBRATE_BASELINE", "ADJUST_SCORE", "EXPORT_CSV", "EDIT_LOG_ENTRY",
    "CHALLENGE", "FORCE_RESET_STATE", "FORCE_JUDGMENT", "FORCE_ACCEPT",
    "MATCH_BEGIN", "MANUAL_START_TOSSUP", "MANUAL_START_BONUS", "MANUAL_REREAD_TOSSUP",
    "PAUSE_MATCH", "LOG_BUZZPOINT", "AUTH_INIT",
}
SEATS = {"A-1", "A-C", "A-3", "A-4", "A-5", "B-1", "B-C", "B-3", "B-4", "B-5"}


def validate_room_id(room_id: str) -> bool:
    return isinstance(room_id, str) and ROOM_ID_PATTERN.fullmatch(room_id) is not None


def _bounded_string(value: Any, name: str, max_length: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > max_length or (not allow_empty and not value.strip()):
        raise ValueError(f"Invalid {name}")
    return value


def validate_question_bank(bank: Any) -> list[dict[str, Any]]:
    if not isinstance(bank, list) or not bank or len(bank) > MAX_QUESTIONS_PER_BANK:
        raise ValueError(f"Question bank must contain 1 to {MAX_QUESTIONS_PER_BANK} questions")
    allowed_fields = {"tossup_text", "tossup_answer", "bonus_text", "bonus_answer", "category", "type", "tossup_options", "bonus_options", "tossup_visual"}
    if len(json.dumps(bank, ensure_ascii=False).encode("utf-8")) > 768 * 1024:
        raise ValueError("Question bank exceeds the 768 KB limit")
    for index, question in enumerate(bank):
        if not isinstance(question, dict):
            raise ValueError(f"Question {index + 1} must be an object")
        if set(question) - allowed_fields:
            raise ValueError(f"Question {index + 1} contains unsupported fields")
        if "tossup_visual" in question and not isinstance(question["tossup_visual"], bool):
            raise ValueError(f"Question {index + 1} tossup_visual must be true or false")
        for field in ("tossup_text", "tossup_answer"):
            _bounded_string(question.get(field), f"question {index + 1} {field}", 12000)
        for field in ("bonus_text", "bonus_answer"):
            if field in question:
                _bounded_string(question[field], f"question {index + 1} {field}", 12000, allow_empty=True)
        for field in ("category", "type"):
            if field in question:
                _bounded_string(question[field], f"question {index + 1} {field}", 80, allow_empty=True)
        for field in ("tossup_options", "bonus_options"):
            options = question.get(field, [])
            if not isinstance(options, list) or len(options) > 8:
                raise ValueError(f"Question {index + 1} {field} must be a short list")
            for option in options:
                _bounded_string(option, f"question {index + 1} option", 2000)
    return bank


def validate_client_event(event_type: Any, payload: Any) -> dict[str, Any]:
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
        raise ValueError("Unsupported event type")
    if not isinstance(payload, dict):
        raise ValueError("Event payload must be an object")

    if event_type == "BUZZ":
        # Online buzzes derive team and player from the authenticated room profile.
        # Optional client fields remain validated for the legacy in-person controls.
        if "team" in payload and payload.get("team") not in ("Team A", "Team B"):
            raise ValueError("BUZZ team must be Team A or Team B")
        if "player" in payload:
            player = _bounded_string(payload["player"], "player", 64)
            if not re.fullmatch(r"(?:[AB]-(?:1|3|4|5|C)|Captain|Manual-[AB])", player):
                raise ValueError("Invalid player identifier")
        if set(payload) - {"team", "player"}:
            raise ValueError("Unsupported BUZZ fields")
    elif event_type == "READING_DONE":
        if set(payload) - {"reading_id"}:
            raise ValueError("Unsupported reading completion fields")
        if "reading_id" in payload:
            reading_id = payload["reading_id"]
            if not isinstance(reading_id, str) or not re.fullmatch(r"[a-f0-9]{32}", reading_id):
                raise ValueError("Invalid reading identifier")
    elif event_type == "LOAD_BANK":
        validate_question_bank(payload.get("bank"))
    elif event_type == "SAVE_ROSTER":
        for field, allowed in (("teams", {"Team A", "Team B"}), ("players", SEATS)):
            values = payload.get(field)
            if not isinstance(values, dict) or len(values) > len(allowed) or not set(values).issubset(allowed):
                raise ValueError(f"Invalid roster {field}")
            for value in values.values():
                _bounded_string(value, "roster name", 64)
                if any(ord(char) < 32 for char in value):
                    raise ValueError("Roster names cannot contain control characters")
    elif event_type == "ADJUST_SCORE":
        if payload.get("team") not in ("Team A", "Team B"):
            raise ValueError("Invalid score team")
        delta = payload.get("delta")
        if isinstance(delta, bool) or not isinstance(delta, int) or not -1000 <= delta <= 1000:
            raise ValueError("Score adjustment must be an integer from -1000 to 1000")
    elif event_type == "EDIT_LOG_ENTRY":
        q_num = payload.get("q_num")
        if isinstance(q_num, bool) or not isinstance(q_num, (str, int)) or not str(q_num).isdigit():
            raise ValueError("Invalid question number")
        if payload.get("team") not in ("Team A", "Team B") or payload.get("column") not in ("TOSSUP", "BONUS"):
            raise ValueError("Invalid score edit target")
        _bounded_string(payload.get("new_val"), "edited score", 100)
    elif event_type == "PROCESS_FRAME":
        frame = payload.get("frame_data")
        if not isinstance(frame, str) or not frame.startswith("data:image/jpeg;base64,"):
            raise ValueError("Frame must be a JPEG data URL")
        encoded = frame.partition(",")[2]
        if len(encoded) > MAX_FRAME_BYTES * 4 // 3 + 4:
            raise ValueError("Frame exceeds size limit")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Invalid base64 frame") from exc
        if not raw or len(raw) > MAX_FRAME_BYTES:
            raise ValueError("Frame exceeds size limit")
    elif event_type == "ANSWER_AUDIO":
        audio = _bounded_string(payload.get("audio_data", ""), "audio data", MAX_AUDIO_BYTES * 4 // 3 + 128, allow_empty=True)
        if audio and not audio.startswith("data:audio/"):
            raise ValueError("Audio must be an audio data URL")
    elif event_type == "SAVE_CALIBRATION":
        if len(payload) > 10:
            raise ValueError("Too many camera regions")
        for seat, coords in payload.items():
            if seat not in SEATS or not isinstance(coords, list) or len(coords) != 4:
                raise ValueError("Invalid camera region")
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1 for v in coords):
                raise ValueError("Camera coordinates must be between zero and one")
            if coords[0] >= coords[2] or coords[1] >= coords[3]:
                raise ValueError("Camera region bounds must have positive area")
    elif event_type == "FORCE_JUDGMENT" and not isinstance(payload.get("correct"), bool):
        raise ValueError("Judgment must be true or false")
    elif event_type == "LOG_BUZZPOINT":
        _bounded_string(payload.get("buzzpoint", ""), "buzzpoint", 80, allow_empty=True)
    elif event_type == "AUTH_INIT":
        _bounded_string(payload.get("api_key", ""), "API key", 256, allow_empty=True)
    elif event_type == "PAUSE_MATCH" and not isinstance(payload.get("paused"), bool):
        raise ValueError("Pause state must be true or false")

    return payload
