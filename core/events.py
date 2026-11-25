import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, Optional


# The event contract lives here so the room loop, websocket gate, and client
# validation cannot drift apart. These are names, not EventType members: the
# client sends string event types and the loop uses the same strings.
MANUAL_START_EVENTS = frozenset({"MANUAL_START_TOSSUP", "MANUAL_REREAD_TOSSUP", "MANUAL_START_BONUS"})
READING_REPORTS = frozenset({"READING_DONE", "READING_FAILED"})

ADMIN_EVENTS = frozenset({
    "EXPORT_CSV", "SAVE_CALIBRATION", "RESET_CALIBRATION", "CALIBRATE_BASELINE",
    "PROCESS_FRAME", "PING", "UPDATE_STATUS", "PAUSE_MATCH", "ADJUST_SCORE",
    "EDIT_LOG_ENTRY", "FORCE_ACCEPT", "LOAD_BANK", "SAVE_ROSTER", "AUTH_INIT",
    "SET_READING_MODE", "END_MATCH", "CLOSE_MATCH", "SET_CAMERA_BUZZ",
}) | MANUAL_START_EVENTS
# Heartbeats and pause reports are admin events by routing, but they do not mutate
# match state and must not invoke the admin handler while a read or answer is live.
PASSIVE_ADMIN_EVENTS = frozenset({"PING", "PAUSE_MATCH"})

EVENT_TYPES = frozenset({
    "PING", "BUZZ", "READING_DONE", "READING_FAILED", "ANSWER_AUDIO", "PROCESS_FRAME",
    "SAVE_ROSTER", "LOAD_BANK", "SAVE_CALIBRATION", "RESET_CALIBRATION",
    "CALIBRATE_BASELINE", "ADJUST_SCORE", "EXPORT_CSV", "EDIT_LOG_ENTRY",
    "CHALLENGE", "FORCE_RESET_STATE", "FORCE_JUDGMENT", "FORCE_ACCEPT", "END_MATCH",
    "OVERRIDE_LAST_QUESTION", "CLOSE_MATCH", "DEAD_TOSSUP", "SET_CAMERA_BUZZ",
    "MATCH_BEGIN", "MANUAL_START_TOSSUP", "MANUAL_START_BONUS", "MANUAL_REREAD_TOSSUP",
    "PAUSE_MATCH", "LOG_BUZZPOINT", "MANUAL_READ_DONE", "AUTH_INIT", "SET_READING_MODE",
})

OWNER_ONLY_EVENTS = frozenset({
    "SAVE_ROSTER", "LOAD_BANK", "SAVE_CALIBRATION", "RESET_CALIBRATION",
    "CALIBRATE_BASELINE", "PROCESS_FRAME", "ADJUST_SCORE", "EXPORT_CSV",
    "EDIT_LOG_ENTRY", "CHALLENGE", "FORCE_RESET_STATE", "FORCE_JUDGMENT",
    "FORCE_ACCEPT", "END_MATCH", "OVERRIDE_LAST_QUESTION", "CLOSE_MATCH",
    "MANUAL_START_TOSSUP", "MANUAL_START_BONUS", "MANUAL_REREAD_TOSSUP",
    "PAUSE_MATCH", "MATCH_BEGIN", "READING_DONE", "READING_FAILED",
    "ANSWER_AUDIO", "LOG_BUZZPOINT", "MANUAL_READ_DONE", "AUTH_INIT",
    "SET_READING_MODE", "DEAD_TOSSUP", "SET_CAMERA_BUZZ",
})
OWNER_ONLY_OUTPUTS = frozenset({
    "START_READING", "START_LISTENING", "UPDATE_QUESTION", "UI_STATE",
    "NEW_LOG_ENTRY", "SYNC_LOG", "DOWNLOAD_CSV", "MATCH_COMPLETE",
    "DEBUG_FRAME", "CAMERA_BUZZ_STATE",
})
ONLINE_PUBLIC_OUTPUTS = frozenset({
    "START_READING", "START_LISTENING", "UPDATE_QUESTION", "UI_STATE",
    "NEW_LOG_ENTRY", "SYNC_LOG", "UPDATE_STATUS", "UPDATE_SCORE", "BUZZ",
    "ROOM_ROSTER", "CONNECTED", "ERROR", "OPEN_BUZZ", "CLOSE_BUZZ",
    "MODERATOR_READING",
})
ONLINE_PLAYER_EVENTS = frozenset({"BUZZ", "ANSWER_AUDIO", "READING_DONE", "READING_FAILED", "PING"})


class EventType(Enum):
    BUZZ = auto()
    TTS_STOPPED = auto()
    SPEECH_RECOGNIZED = auto()
    KEYWORD_DETECTED = auto()
    EVALUATION_COMPLETE = auto()
    STATE_CHANGED = auto()
    SCORE_UPDATED = auto()
    TIMEOUT = auto()
    UPDATE_SCORE = auto()
    BUZZ_EVALUATED = auto()
    PENALTY_APPLIED = auto()
    BLURT_DETECTED = auto()
    QUESTION_STARTED = auto()
    BONUS_STARTED = auto()
    BONUS_ANSWER_LOCKED = auto()
    TOSSUP_RESULT = auto()
    BONUS_RESULT = auto()
    READING_DONE = auto()


@dataclass
class Event:
    type: EventType | str
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    user_id: Optional[str] = None
    role: Optional[str] = None
