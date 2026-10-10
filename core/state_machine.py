from enum import Enum, auto
from typing import Dict, Optional

class GameState(Enum):
    IDLE = auto()
    READING_TOSSUP = auto()
    TOSSUP_BUZZED = auto()
    WAITING_RECOGNITION = auto()
    LISTENING_TOSSUP_ANSWER = auto()
    EVALUATING_TOSSUP = auto()
    READING_BONUS = auto()
    BONUS_CONFERRING = auto()
    EVALUATING_BONUS = auto()
    QUESTION_COMPLETE = auto()

class StateEngine:
    def __init__(self):
        self.current_state: GameState = GameState.IDLE
        self.active_team: Optional[str] = None
        self.active_player: Optional[str] = None
        self.question_number: int = 1
        self.team_a_score: int = 0
        self.team_b_score: int = 0
        self.interrupted_at_word: int = -1
        self.is_interrupted: bool = False

    def transition_to(self, new_state: GameState) -> None:
        self.current_state = new_state

    def handle_buzz(self, team: str, word_index: int, is_reading: bool) -> bool:
        if self.current_state != GameState.READING_TOSSUP:
            return False
        
        self.active_team = team
        self.interrupted_at_word = word_index
        self.is_interrupted = is_reading
        self.transition_to(GameState.TOSSUP_BUZZED)
        return True

    def apply_tossup_points(self, is_correct: bool) -> Dict[str, int]:
        tossup_points = 0
        penalty_points = 0

        is_team_a = self.active_team in ["Team A", "A"] or (
            self.active_team and "Team A" in self.active_team
        )
        is_team_b = self.active_team in ["Team B", "B"] or (
            self.active_team and "Team B" in self.active_team
        )

        if is_correct:
            tossup_points = 4
            if is_team_a:
                self.team_a_score += 4
            elif is_team_b:
                self.team_b_score += 4
        else:
            if self.is_interrupted:
                penalty_points = -4
                if is_team_a:
                    self.team_a_score -= 4
                elif is_team_b:
                    self.team_b_score -= 4

        return {
            "tossup_points": tossup_points,
            "penalty_points": penalty_points,
            "score_a": self.team_a_score,
            "score_b": self.team_b_score
        }

    def apply_bonus_points(self, is_correct: bool) -> Dict[str, int]:
        bonus_points = 10 if is_correct else 0
        if is_correct:
            if self.active_team == "Team A":
                self.team_a_score += 10
            else:
                self.team_b_score += 10

        return {
            "bonus_points": bonus_points,
            "score_a": self.team_a_score,
            "score_b": self.team_b_score
        }

    def next_question(self) -> None:
        self.question_number += 1
        self.active_team = None
        self.active_player = None
        self.interrupted_at_word = -1
        self.is_interrupted = False
        self.transition_to(GameState.IDLE)