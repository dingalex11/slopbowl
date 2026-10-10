import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, Optional


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
