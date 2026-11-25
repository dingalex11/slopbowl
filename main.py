import os
import sys
import json
import asyncio
import argparse
import time
import csv
import io
import base64
import numpy as np
import cv2
from pathlib import Path
import uvicorn
from core.config import INBOUND_QUEUE_SIZE, OUTBOUND_QUEUE_SIZE, PROJECT_ROOT
from core.validation import validate_question_bank

from core.events import MANUAL_START_EVENTS, Event, EventType
from core.reading import ReadTimingPolicy, TossupSession
from core.room_events import RoomEventPump
from core.state_machine import StateEngine, GameState
from services.judge_service import JudgeService, JudgeUnavailable
from services.ui_service import HTMLGUIService
from services.camera_service import VisionService 

# Reports a reading station sends about a read it was asked to speak. READING_FAILED is
# not a finished read: the browser could not speak the question at all.
READ_FAILED_WARNING = "⚠️ This station could not read the question aloud — check its speech settings."
READ_TIMEOUT_WARNING = "⚠️ The read never reported finishing — the buzz window is open."
JUDGE_NOT_CONFIGURED_WARNING = "⚠️ AI judge unavailable — add an API key in Settings before starting an AI match."
JUDGE_UNAVAILABLE_WARNING = (
    "⚠️ AI judge unavailable — this answer was not scored and no penalty was applied. "
    "Check the API key or connection; Override Last Question can reopen it."
)

# Conservative spoken pace used to sanity-check read reports: real speech is slower, so a
# report that arrives faster than this cannot describe a question that was spoken.
READ_CHARS_PER_SECOND = 18.0
MIN_READ_SECONDS = 1.0
# A "finished" report inside the first share of the estimated read time is not believed.
# A station on older code reports a read as finished the moment it starts, which used to
# open the buzz window while the question was still being read out.
READ_EARLY_REPORT_FRACTION = 0.4
READ_WATCHDOG_FACTOR = 1.8
READ_WATCHDOG_MIN_SECONDS = 6.0
# When a read's only report was impossible (an older station claiming it finished at
# once), the end of the read has to be guessed from the text. Speech is much slower than
# the sanity-check pace above — measured at about 12 characters per second at the
# client's speech rate — so the guess uses a deliberately slower pace: a guessed end
# must never land while the question is still being read out.
READ_SLOW_CHARS_PER_SECOND = 9.0


def read_clock() -> float:
    """Monotonic seconds for read timing, usable with or without a running event loop."""
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:
        return time.monotonic()

# Seconds a player gets to speak once the mic opens: the tossup answer window and
# the bonus captain's answer window both use this.
ANSWER_WINDOW_SECONDS = 4.0

class MatchEnded(Exception):
    """Abort the question in flight and return the room to its idle loop."""

# The two ways an online tossup can be delivered:
#   stations  - every player's browser speaks the text with its own voice (the default).
#   moderator - only the moderator's station speaks; players stay silent and hear the
#               moderator's audio through the call. That makes the owner's read the
#               room's single clock: the interrupt boundary and the 5-second buzz window
#               are the same instant for both teams, instead of tracking the moderator's
#               voice while a slower station is still mid-question.
READING_MODES = ("stations", "moderator")


class ScienceBowlModerator:
    def __init__(self, use_audio=True, use_camera=False, initial_bank=None, inbound_queue_size=INBOUND_QUEUE_SIZE, outbound_queue_size=OUTBOUND_QUEUE_SIZE, manual_reader=False):
        self.inbound_queue = asyncio.Queue(maxsize=inbound_queue_size)
        self.outbound_queue = asyncio.Queue(maxsize=outbound_queue_size)
        self.event_pump = RoomEventPump(self.inbound_queue, self.process_admin_event)
        self.tossup_session = TossupSession(self.inbound_queue, self.event_pump, lambda: read_clock())
        self.read_session = self.tossup_session.read
        
        self.use_audio = use_audio
        self.use_camera = use_camera
        self.manual_reader = bool(manual_reader)
        
        if self.manual_reader:
            print("[MANUAL READER]: Human reader mode; no TTS, microphone or AI judge. Camera buzz is optional.")
        elif not self.use_audio:
            print("[AUDIO DISABLED]: Falling back to silent mock audio mode.")
            
        self.judge = None

        self.state = StateEngine() 
        
        if self.use_camera and not self.manual_reader:
            self.vision = VisionService(self.inbound_queue, self.outbound_queue)
        else:
            # Manual rooms create the vision engine only when the moderator opts in.
            self.vision = None
        self.camera_buzz_enabled = bool(self.vision)
        # True while an AI-moderated tossup is reading or in its 5-second buzz window.
        # Manual rooms use manual_phase instead.
        self.camera_read_active = False
        if self.vision:
            print("[CAMERA ENABLED]: Vision engine is ready; buzz detection arms during a read.")
        elif self.manual_reader:
            print("[CAMERA OPTIONAL]: Off until the moderator enables camera buzz in setup.")
        else:
            print("[CAMERA DISABLED]: Vision engine is off; using manual/keyboard buzzers.")

        self.ui = HTMLGUIService(self.inbound_queue, self.outbound_queue, self.vision)

        self.match_log = []
        self.question_bank = validate_question_bank(initial_bank) if initial_bank else []
        self.current_q_idx = 0
        # Packet exhaustion is the match boundary: announce it once, then leave the room
        # idle so the owner can export the log or load the next packet.
        self.match_complete_notified = False
        self.match_complete_question_count = None
        # Explicit End Match opens the teardown path; packet exhaustion does not.
        self.match_ended_by_user = False

        self.custom_teams = {}
        self.custom_roster = {}
        self.online_mode = False
        self.online_participants = {}
        self.answering_user_id = None
        self.online_buzz_open = False
        self.online_buzz_team = None
        self.online_buzz_captain_only = False
        self.reading_mode = "stations"
        # Broadcast to player stations while the moderator's station is the one speaking.
        self.moderator_reading = {"active": False, "kind": None}
        self.answer_audio_open = False
        self.ui_state = {"show": "NONE", "overrides": False, "can_override": False}
        # The manual-reader HUD is driven by phase events and explicit buzzpoint marks.
        # Keeping the last phase lets a reconnecting owner station rebuild its clocks.
        self.manual_phase = {"phase": "IDLE", "seconds": None, "started_at": None}
        self.manual_read_complete = False
        self.manual_read_started = 0.0
        self.manual_buzzpoint = ""
        self.manual_after_read = False
        self.manual_question = {}

    @property
    def current_reading_id(self):
        return self.read_session.current_reading_id

    @current_reading_id.setter
    def current_reading_id(self, value):
        self.read_session.current_reading_id = value

    @property
    def read_earliest_end(self):
        return self.read_session.read_earliest_end

    @read_earliest_end.setter
    def read_earliest_end(self, value):
        self.read_session.read_earliest_end = value

    @property
    def read_deadline(self):
        return self.read_session.read_deadline

    @read_deadline.setter
    def read_deadline(self, value):
        self.read_session.read_deadline = value

    @property
    def read_assumed_end(self):
        return self.read_session.read_assumed_end

    @read_assumed_end.setter
    def read_assumed_end(self, value):
        self.read_session.read_assumed_end = value

    @property
    def reading_expected_users(self):
        return self.read_session.reading_expected_users

    @reading_expected_users.setter
    def reading_expected_users(self, value):
        self.read_session.reading_expected_users = set(value)

    @property
    def reading_completions(self):
        return self.read_session.reading_completions

    @reading_completions.setter
    def reading_completions(self, value):
        self.read_session.reading_completions = set(value)

    async def _set_moderator_reading(self, active: bool, kind: str | None = None):
        """Tell player stations that the moderator's voice is the one being heard.

        In shared-reading mode players get no START_READING of their own, so without this
        their station would sit silent and look broken. The state is kept so a player who
        reconnects mid-read is told the truth when they attach (see CONNECTED).
        """
        if not self.online_mode or self.reading_mode != "moderator":
            return
        self.moderator_reading = {"active": bool(active), "kind": kind if active else None}
        await self.outbound_queue.put({"type": "MODERATOR_READING", "payload": dict(self.moderator_reading)})

    async def _set_online_buzz_window(self, opened: bool, *, team=None, captain_only=False):
        if not self.online_mode:
            return
        self.online_buzz_open = opened
        self.online_buzz_team = team if opened else None
        self.online_buzz_captain_only = bool(captain_only and opened)
        kind = "OPEN_BUZZ" if opened else "CLOSE_BUZZ"
        await self.outbound_queue.put({"type": kind, "payload": {"team": self.online_buzz_team, "captain_only": self.online_buzz_captain_only}})

    async def _set_ui_state(self, show: str, overrides: bool = False):
        """Broadcast which moderator control is armed, and remember it.

        Keeping the last state lets a moderator who reconnects (team change, dropped
        socket) be re-armed instead of facing a HUD with no usable start control.
        """
        can_override = show in ("TOSSUP", "BONUS") and self._last_logged_question_number() is not None
        self.ui_state = {"show": show, "overrides": bool(overrides), "can_override": can_override}
        await self.outbound_queue.put({"type": "UI_STATE", "payload": dict(self.ui_state)})

    async def _announce_judge_unavailable(self, message: str, reason: str = ""):
        """Make a failed judgement visible instead of quietly treating it as wrong."""
        payload = {"message": message}
        if reason:
            payload["reason"] = reason
        await self.outbound_queue.put({"type": "JUDGE_UNAVAILABLE", "payload": payload})
        await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": message}})

    async def _advance_unjudged(self, team: str, player: str, buzzpoint: str, buzz_time: float, reason: str = ""):
        """Record a no-score question and move on; a judge outage must not become a neg."""
        await self.log_event("UNJUDGED", team or "None", player or "None", None, 0, buzzpoint, buzz_time)
        await self._announce_judge_unavailable(JUDGE_UNAVAILABLE_WARNING, reason)
        await self.outbound_queue.put({"type": "UPDATE_SCORE", "payload": {"score_a": self.state.team_a_score, "score_b": self.state.team_b_score}})
        self.state.next_question()
        self.current_q_idx += 1
        await self.outbound_queue.put({"type": "SYNC_LOG", "payload": {"log": self.match_log}})
        self.answering_user_id = None
        self.answer_audio_open = False
        await asyncio.sleep(2)

    async def _transcribe_with_judge(self, b64_audio: str) -> str:
        if not self.judge:
            raise JudgeUnavailable("No AI judge API key is configured.")
        try:
            transcript = await self.judge.transcribe_audio(b64_audio)
        except JudgeUnavailable:
            raise
        except Exception as exc:
            print(f"[STT ERROR]: {exc}")
            raise JudgeUnavailable("The speech-to-text request failed.") from exc
        if not isinstance(transcript, str):
            raise JudgeUnavailable("The speech-to-text service returned no transcript.")
        return transcript

    async def _evaluate_with_judge(self, spoken_answer: str, correct_answer: str, category: str, is_multiple_choice: bool = False) -> bool:
        if not self.judge:
            raise JudgeUnavailable("No AI judge API key is configured.")
        try:
            verdict = await self.judge.evaluate_answer(
                spoken_answer,
                correct_answer,
                category,
                is_multiple_choice=is_multiple_choice,
            )
        except JudgeUnavailable:
            raise
        except Exception as exc:
            print(f"[JUDGE AGENT ERROR]: {exc}")
            raise JudgeUnavailable("The AI judge request failed.") from exc
        if not isinstance(verdict, bool):
            raise JudgeUnavailable("The AI judge returned no verdict.")
        return verdict

    def _idle_start_message(self) -> str:
        """Explain what a Start Tossup click is missing while the loop has nothing to read."""
        if not self.question_bank:
            return "⚠️ No question packet loaded — upload one in Pre Match Settings."
        return "🏁 Every question in this packet is complete — load a new packet to play on."

    def _last_logged_question_number(self):
        """The highest question number that has an adjudicated log row, if any."""
        highest = None
        for row in self.match_log:
            try:
                q_num = int(row.get("Q_Num"))
            except (AttributeError, TypeError, ValueError):
                continue
            if q_num > 0 and (highest is None or q_num > highest):
                highest = q_num
        return highest

    def _recalculate_scores_from_log(self) -> tuple[int, int]:
        """Rebuild both scores from the surviving event rows."""
        score_a = 0
        score_b = 0
        for row in self.match_log:
            try:
                points = int(row.get("Points", 0) or 0)
            except (TypeError, ValueError):
                points = 0
            if row.get("Team") == "Team A":
                score_a += points
            elif row.get("Team") == "Team B":
                score_b += points
        self.state.team_a_score = score_a
        self.state.team_b_score = score_b
        return score_a, score_b

    def _match_complete_data(self) -> dict:
        """The final score, packet size and adjudicated log for the summary screen."""
        return {
            "score_a": self.state.team_a_score,
            "score_b": self.state.team_b_score,
            "question_count": (
                self.match_complete_question_count
                if self.match_complete_question_count is not None
                else len(self.question_bank)
            ),
            "log": [dict(row) for row in self.match_log],
            "teardown": bool(self.match_ended_by_user),
        }

    async def _notify_match_complete(self):
        """Tell the owner station once that the packet has ended.

        The game loop stays alive afterwards so Export CSV, roster changes and a new
        LOAD_BANK still work; this event is only the boundary marker for the UI.
        """
        if self.match_complete_notified:
            return
        self.match_complete_notified = True
        self.state.transition_to(GameState.QUESTION_COMPLETE)
        if self.ui_state.get("show") != "NONE":
            await self._set_ui_state("NONE")
        await self.outbound_queue.put({"type": "MATCH_COMPLETE", "payload": self._match_complete_data()})

    def _prepare_forced_end(self):
        """Freeze the current match without awarding or adjudicating the live question."""
        self.match_ended_by_user = True
        # A natural packet ending may already have shown its summary; ending it now
        # must publish the explicit teardown summary instead of being suppressed.
        self.match_complete_notified = False
        logged_count = 0
        for row in self.match_log:
            try:
                logged_count = max(logged_count, int(row.get("Q_Num", 0)))
            except (TypeError, ValueError):
                continue
        self.match_complete_question_count = min(
            len(self.question_bank),
            max(self.current_q_idx, logged_count),
        )
        self.current_q_idx = len(self.question_bank)
        self.state.transition_to(GameState.QUESTION_COMPLETE)
        self.manual_phase = {"phase": "IDLE", "seconds": None, "started_at": None}
        self.manual_read_complete = False
        self.manual_buzzpoint = ""
        self.manual_after_read = False
        self.read_session.reset()
        self.online_buzz_open = False
        self.online_buzz_team = None
        self.online_buzz_captain_only = False
        self.answer_audio_open = False
        self.answering_user_id = None
        self.moderator_reading = {"active": False, "kind": None}
        self._set_camera_window(False)

    async def _rewind_last_question(self) -> bool:
        """Remove the last completed question and reload it for a fresh attempt."""
        q_num = self._last_logged_question_number()
        if q_num is None:
            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "⚠️ There is no completed question to override."}})
            return False

        self.match_log = [
            row for row in self.match_log
            if str(row.get("Q_Num", "")) != str(q_num)
        ]
        score_a, score_b = self._recalculate_scores_from_log()

        self.current_q_idx = max(0, q_num - 1)
        self.state.question_number = q_num
        self.state.active_team = None
        self.state.active_player = None
        self.state.interrupted_at_word = -1
        self.state.is_interrupted = False
        self.state.transition_to(GameState.IDLE)
        self.match_complete_notified = False
        self.match_complete_question_count = None
        self.match_ended_by_user = False

        self.manual_phase = {"phase": "IDLE", "seconds": None, "started_at": None}
        self.manual_read_complete = False
        self.manual_read_started = 0.0
        self.manual_buzzpoint = ""
        self.manual_after_read = False
        self.manual_question = {}
        self.read_session.reset()
        self.online_buzz_open = False
        self.online_buzz_team = None
        self.online_buzz_captain_only = False
        self.answer_audio_open = False
        self.answering_user_id = None
        self.moderator_reading = {"active": False, "kind": None}
        self.ui_state = {"show": "NONE", "overrides": False, "can_override": False}

        await self.outbound_queue.put({"type": "UPDATE_SCORE", "payload": {"score_a": score_a, "score_b": score_b}})
        await self.outbound_queue.put({"type": "SYNC_LOG", "payload": {"log": [dict(row) for row in self.match_log]}})
        # Publish the reopened question now. The loop will preload it again after the
        # override, but this event is what guarantees the HUD stops showing the
        # question that was staged after the removed one.
        if self.question_bank and 0 <= self.current_q_idx < len(self.question_bank):
            replay_q = self.question_bank[self.current_q_idx]
            replay_text = str(replay_q.get("tossup_text", "")).strip()
            replay_payload = {
                "text": replay_text if replay_text else "[WARNING: PYTHON MEMORY HAS NO TEXT FOR THIS QUESTION]",
                "raw_text": replay_text,
                "answer": str(replay_q.get("tossup_answer", "")).strip(),
                "category": str(replay_q.get("category", "GENERAL")),
                "options": replay_q.get("tossup_options", []),
                "visual": replay_q.get("tossup_visual", False),
                "manual": self.manual_reader,
                "phase": "TOSSUP",
                "preview": True,
            }
            await self.outbound_queue.put({"type": "UPDATE_QUESTION", "payload": replay_payload})
        await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": f"↩️ Q{q_num} reopened — its score and log entries were removed."}})
        return True

    @staticmethod
    def _safe_csv_value(value):
        if isinstance(value, str) and value[:1] in "=+-@\t\r":
            return "'" + value
        return value

    def generate_csv(self):
        output = io.StringIO()
        fieldnames = ["Q_Num", "Event_Type", "Team", "Player", "Correct", "Points", "Buzzpoint", "Buzz_Time", "Score_A", "Score_B"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in self.match_log:
            writer.writerow({key: self._safe_csv_value(value) for key, value in row.items()})
        return output.getvalue()

    async def log_event(self, event_type: str, team: str, player: str, is_correct: bool | None, points: int, buzzpoint: str = "", buzz_time: float = 0.0):
        entry = {
            "Q_Num": self.state.question_number,
            "Event_Type": event_type,
            "Team": team,
            "Player": player,
            "Correct": is_correct,
            "Points": points,
            "Buzzpoint": buzzpoint,
            "Buzz_Time": f"{buzz_time:.3f}",
            "Score_A": self.state.team_a_score,
            "Score_B": self.state.team_b_score
        }
        self.match_log.append(entry)
        # Broadcast the new log entry directly to the UI for live updating
        await self.outbound_queue.put({"type": "NEW_LOG_ENTRY", "payload": entry})
        
    async def process_admin_event(self, event):
        # Network commands carry identity; only the authenticated room owner can mutate state.
        if getattr(event, "user_id", None) and getattr(event, "role", None) != "owner":
            return
        evt_type = event.type.name if hasattr(event.type, 'name') else str(event.type)
        if evt_type == "END_MATCH":
            self._prepare_forced_end()
            raise MatchEnded()
        if evt_type == "AUTH_INIT" and not self.manual_reader:
            api_key = event.payload.get("api_key")
            if not api_key:
                self.judge = None
                await self._announce_judge_unavailable(JUDGE_NOT_CONFIGURED_WARNING, "No API key was provided.")
            else:
                try:
                    self.judge = JudgeService(api_key=api_key)
                    await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "✅ Cloud API Key Linked & AI Judge Online!"}})
                except Exception as e:
                    self.judge = None
                    print(f"[WARNING]: Judge Service failed to initialize: {e}")
                    await self._announce_judge_unavailable(JUDGE_NOT_CONFIGURED_WARNING, "The AI judge could not be initialized.")

        elif evt_type in MANUAL_START_EVENTS:
            # A start that arrives while a question is live must not vanish: it is not
            # the loop's turn to accept one yet, so say so.
            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "⚠️ A question is already in progress — finish or challenge it before starting the next one."}})

        elif evt_type == "SET_READING_MODE":
            mode = event.payload.get("mode")
            if mode not in READING_MODES:
                return
            self.reading_mode = mode
            await self.outbound_queue.put({"type": "READING_MODE", "payload": {"mode": mode}})
            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": (
                "🔊 Moderator reads aloud — player devices stay silent. Share this station's audio with the room."
                if mode == "moderator" else
                "🔊 Each player's device reads the question in its own voice."
            )}})

        elif evt_type == "MATCH_BEGIN":
            if self.question_bank and self.current_q_idx < len(self.question_bank):
                await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "✅ Match started. Ready for the first tossup."}})

        elif evt_type == "SAVE_ROSTER":
            self.custom_teams = event.payload.get("teams", {})
            self.custom_roster = event.payload.get("players", {})
            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "✅ Match Roster Locked In!"}})

        elif evt_type == "LOAD_BANK":
            try:
                self.question_bank = validate_question_bank(event.payload.get("bank", []))
            except ValueError as exc:
                await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": f"❌ Invalid question bank: {exc}"}})
                return
            self.current_q_idx = 0
            self.state.question_number = 1
            self.state.team_a_score = 0
            self.state.team_b_score = 0
            self.state.transition_to(GameState.IDLE)
            self.match_log = []
            self.match_complete_notified = False
            self.match_complete_question_count = None
            self.match_ended_by_user = False
            
            await self.outbound_queue.put({"type": "UPDATE_SCORE", "payload": {"score_a": 0, "score_b": 0}})
            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": f"✅ Loaded {len(self.question_bank)} new questions."}})
            await self.outbound_queue.put({"type": "PREMATCH_READY", "payload": {"count": len(self.question_bank), "first_category": self.question_bank[0].get("category", "GENERAL")}})
            # A manual room can be waiting on "Start Tossup" when a packet lands (the
            # dev harness preloads one, and a moderator can replace a packet before the
            # first question). Keep that start control armed instead of hiding it.
            if (
                not self.online_mode
                and self.question_bank
                and self.current_q_idx < len(self.question_bank)
                and self.state.current_state == GameState.IDLE
            ):
                # Both local modes own their start control: the manual reader and the
                # in-person AI station. Online rooms arm through their lobby instead.
                await self._set_ui_state("TOSSUP")
            else:
                await self._set_ui_state("NONE")
            
            await self.inbound_queue.put(Event(type=EventType.STATE_CHANGED, payload={}))
            
        elif evt_type == "ADJUST_SCORE":
            team = event.payload.get("team")
            delta = event.payload.get("delta", 0)
            
            if team == "Team A":
                self.state.team_a_score += delta
            elif team == "Team B":
                self.state.team_b_score += delta
                
            # Log the manual override so it appears in analytics and the scoresheet
            await self.log_event("MANUAL_OVERRIDE", team, "Moderator", delta > 0, delta, "Override", 0.0)
            
            await self.outbound_queue.put({"type": "UPDATE_SCORE", "payload": {"score_a": self.state.team_a_score, "score_b": self.state.team_b_score}})
            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": f"🔧 Manual Override: {team} ({delta:+d})"}})
        
        elif evt_type == "EXPORT_CSV":
            csv_data = self.generate_csv()
            await self.outbound_queue.put({"type": "DOWNLOAD_CSV", "payload": {"csv_data": csv_data}})

        elif evt_type == "CLOSE_MATCH":
            # The room manager owns the socket registry and Firestore document, so it
            # performs the actual teardown when it sees this outbound request.
            await self.outbound_queue.put({"type": "ROOM_CLOSE_REQUESTED", "payload": {}})
            
        elif evt_type == "UPDATE_STATUS":
            if isinstance(event, dict):
                await self.outbound_queue.put(event)
            else:
                await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": event.payload})
            
        elif evt_type == "SET_CAMERA_BUZZ":
            await self._set_camera_buzz(bool(event.payload.get("enabled")))
        elif evt_type == "SAVE_CALIBRATION" and self.vision:
            self.vision.update_rois(event.payload)
            await self.outbound_queue.put({"type": "CAMERA_BUZZ_STATE", "payload": self._camera_state()})
        elif evt_type == "RESET_CALIBRATION" and self.vision:
            self.vision.clear_rois()
            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "✅ Camera ROIs Cleared!"}})
            await self.outbound_queue.put({"type": "CAMERA_BUZZ_STATE", "payload": self._camera_state()})
        elif evt_type == "CALIBRATE_BASELINE" and self.vision:
            self.vision.request_baseline_calibration()
        
        elif evt_type == "EDIT_LOG_ENTRY":
            q_num = event.payload.get("q_num")
            team = event.payload.get("team")
            column = event.payload.get("column") # Will be "TOSSUP" or "BONUS"
            
            # Clean the string just in case an asterisk sneaks through
            new_val = str(event.payload.get("new_val", "")).replace("*", "").strip()
            
            # 1. Find and edit the specific log entry
            for entry in self.match_log:
                if str(entry.get("Q_Num")) == str(q_num) and entry.get("Team") == team:
                    
                    # --- TOSSUP EDIT LOGIC ---
                    if column == "TOSSUP" and "TOSSUP" in entry.get("Event_Type", ""):
                        tokens = new_val.split()
                        if len(tokens) >= 2:
                            player_str = tokens[0]       # E.g., "P1"
                            score_str = tokens[1].upper() # E.g., "+4", "-4", or "X"
                            
                            entry["Player"] = player_str
                            
                            if score_str == 'X':
                                entry["Points"] = 0
                            else:
                                try:
                                    entry["Points"] = int(score_str.replace('+', ''))
                                except ValueError:
                                    entry["Points"] = 0
                                    
                        entry["Edited"] = True
                        if "Correct" in entry:
                            entry["Correct"] = entry["Points"] > 0
                            
                    # --- BONUS EDIT LOGIC ---
                    elif column == "BONUS" and entry.get("Event_Type") == "BONUS":
                        tokens = new_val.split()
                        if len(tokens) >= 1:
                            score_str = tokens[0].upper() # E.g., "10", "0", or "X"
                            
                            if score_str == 'X':
                                entry["Points"] = 0
                            else:
                                try:
                                    entry["Points"] = int(score_str.replace('+', ''))
                                except ValueError:
                                    entry["Points"] = 0
                                    
                        entry["Edited"] = True
                        if "Correct" in entry:
                            entry["Correct"] = entry["Points"] > 0

            # 2. Recalculate ALL cumulative scores from question 1
            temp_score_a, temp_score_b = 0, 0
            for entry in self.match_log:
                if entry.get("Team") == "Team A":
                    temp_score_a += entry.get("Points", 0)
                elif entry.get("Team") == "Team B":
                    temp_score_b += entry.get("Points", 0)
                    
                entry["Score_A"] = temp_score_a
                entry["Score_B"] = temp_score_b
                
            self.state.team_a_score = temp_score_a
            self.state.team_b_score = temp_score_b
            
            # 3. Push total math to the scoreboard headers
            await self.outbound_queue.put({"type": "UPDATE_SCORE", "payload": {"score_a": temp_score_a, "score_b": temp_score_b}})
            
            # 4. Re-broadcast the entire log to visually update the UI table
            for entry in self.match_log:
                await self.outbound_queue.put({"type": "NEW_LOG_ENTRY", "payload": entry})
            await self.outbound_queue.put({"type": "SYNC_LOG", "payload": {"log": self.match_log}})
            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "✏️ Scoresheet edited & recalculated."}})

        elif evt_type == "PROCESS_FRAME" and self.vision and self.camera_buzz_enabled:
            frame_data = event.payload.get("frame_data", "")
            if not frame_data: return 
            if frame_data.startswith("data:image/jpeg;base64,"):
                frame_data = frame_data.replace("data:image/jpeg;base64,", "")
            try:
                img_bytes = base64.b64decode(frame_data, validate=True)
                np_arr = np.frombuffer(img_bytes, np.uint8)
                if np_arr.size == 0: return 
                frame = cv2.imdecode(np_arr, cv2.IMREAD_REDUCED_COLOR_2)
                if frame is None or frame.shape[0] > 720 or frame.shape[1] > 1280:
                    return
                current_time = asyncio.get_running_loop().time()
                await self.vision.process_frame(frame, current_time)
            except Exception as e:
                print(f"❌ [FRAME DECODE ERROR]: {e}")

    async def wait_for_moderator_action(self, expected_event_types: list):
        return await self.event_pump.wait_for_action(expected_event_types)

    async def _speak_and_wait(self, text: str, *, moderator_only: bool = False, announce_kind: str | None = None):
        if not self.use_audio or not text:
            return
        # In shared-reading mode the moderator's station is the room's only voice:
        # announcements, bonus reads and reveals are not spoken on player devices.
        if self.online_mode and self.reading_mode == "moderator":
            moderator_only = True

        self.read_session.drop_stale_reports(self.inbound_queue)

        reading_id = self._new_reading_id()
        payload = {"text": text, "moderator_only": moderator_only}
        if reading_id:
            payload["reading_id"] = reading_id
        self._set_read_window(text)
        indicator = announce_kind if (self.online_mode and self.reading_mode == "moderator") else None
        if indicator:
            await self._set_moderator_reading(True, indicator)
        await self.outbound_queue.put({"type": "START_READING", "payload": payload})
        
        outcome, _ = await self.read_session.wait_for_report(
            self.inbound_queue,
            self.event_pump,
            online_mode=self.online_mode,
            requeue_other=True,
        )
        if outcome == "timeout":
            print("[DEBUG]: Read watchdog expired; continuing without a report.")
            self.read_session.current_reading_id = None

        if indicator:
            await self._set_moderator_reading(False)

    def _read_timing_policy(self) -> ReadTimingPolicy:
        return ReadTimingPolicy(
            min_read_seconds=MIN_READ_SECONDS,
            chars_per_second=READ_CHARS_PER_SECOND,
            early_report_fraction=READ_EARLY_REPORT_FRACTION,
            watchdog_factor=READ_WATCHDOG_FACTOR,
            watchdog_min_seconds=READ_WATCHDOG_MIN_SECONDS,
            slow_chars_per_second=READ_SLOW_CHARS_PER_SECOND,
        )

    def _set_read_window(self, text: str, *, immediate: bool = False) -> None:
        self.read_session.set_window(text, immediate=immediate, policy=self._read_timing_policy())

    def note_impossible_read_report(self) -> None:
        self.read_session.note_impossible_report()

    def read_report_is_believable(self) -> bool:
        return self.read_session.report_is_believable()

    def _new_reading_id(self):
        expected_users = {
            uid for uid, participant in self.online_participants.items()
            if participant.role == "owner"
        } if self.online_mode else set()
        return self.read_session.new_reading(
            online_mode=self.online_mode,
            expected_users=expected_users,
        )

    def _manual_phase_snapshot(self) -> dict:
        return dict(self.manual_phase)

    def _camera_state(self) -> dict:
        return {
            "enabled": bool(self.camera_buzz_enabled),
            "zones": len(self.vision.rois) if self.vision else 0,
        }

    def _camera_window_open(self) -> bool:
        if self.manual_reader:
            return self.manual_phase.get("phase") in ("READING", "BUZZ_WINDOW")
        return bool(self.camera_read_active)

    def _set_camera_window(self, active: bool):
        """Arm camera detection for an AI read/rebound window, or close it."""
        self.camera_read_active = bool(active)
        if self.vision is not None:
            self.vision.set_armed(bool(self.camera_read_active and self.camera_buzz_enabled))

    async def _set_camera_buzz(self, enabled: bool):
        if self.online_mode:
            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "Camera buzz is available in in-person rooms only."}})
            return
        if enabled:
            if self.vision is None:
                self.vision = VisionService(self.inbound_queue, self.outbound_queue, send_debug_frames=False)
                self.ui.vision_service = self.vision
            self.use_camera = True
            self.camera_buzz_enabled = True
            self.vision.set_armed(bool(self._camera_window_open()))
        else:
            self.camera_buzz_enabled = False
            if self.vision is not None:
                self.vision.set_armed(False)
        await self.outbound_queue.put({"type": "CAMERA_BUZZ_STATE", "payload": self._camera_state()})

    async def _set_manual_phase(self, phase: str, seconds: float | None = None, *, started_at: float | None = None, **extra):
        """Tell the manual-reader HUD which clock is running, and remember it.

        ``started_at`` is epoch milliseconds so a reconnecting owner station can rebuild
        the same countdown instead of restarting it from zero.
        """
        payload = {
            "phase": phase,
            "seconds": seconds,
            "started_at": started_at if started_at is not None else time.time() * 1000,
        }
        payload.update(extra)
        self.manual_phase = payload
        if self.vision is not None:
            self.vision.set_armed(bool(self.camera_buzz_enabled and phase in ("READING", "BUZZ_WINDOW")))
        await self.outbound_queue.put({"type": "MANUAL_PHASE", "payload": dict(payload)})

    def _drop_stale_manual_events(self) -> None:
        """Discard a buzz or verdict left over from the previous question.

        A click in the last instant of a question can still be in the room queue when
        the next question starts. Without this, it would answer the wrong question.
        """
        kept = []
        while not self.inbound_queue.empty():
            try:
                event = self.inbound_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            evt_type = event.type.name if hasattr(event.type, "name") else str(event.type)
            if evt_type not in ("BUZZ", "LOG_BUZZPOINT", "FORCE_JUDGMENT", "MANUAL_READ_DONE", "DEAD_TOSSUP"):
                kept.append(event)
        for event in kept:
            self.inbound_queue.put_nowait(event)

    @staticmethod
    def _manual_read_text(q_data: dict, *, bonus: bool = False) -> tuple[str, str, list, bool]:
        text_field = "bonus_text" if bonus else "tossup_text"
        answer_field = "bonus_answer" if bonus else "tossup_answer"
        options_field = "bonus_options" if bonus else "tossup_options"
        visual_field = "bonus_visual" if bonus else "tossup_visual"
        text = str(q_data.get(text_field, "") or "").strip()
        answer = str(q_data.get(answer_field, "") or "").strip()
        options = q_data.get(options_field, []) or []
        return text, answer, options, bool(q_data.get(visual_field, False))

    async def _handle_manual_tossup_attempt(self, q_data, allowed_team=None, skip_read=False):
        """One tossup with a human reader, a click-to-buzz player, and a marked buzzpoint."""
        self._drop_stale_manual_events()
        tossup_text, tossup_answer, options, visual = self._manual_read_text(q_data)
        category = str(q_data.get("category", "GENERAL") or "GENERAL").strip()
        self.manual_question = {
            "phase": "TOSSUP",
            "raw_text": tossup_text,
            "answer": tossup_answer,
            "category": category,
            "options": options,
            "visual": visual,
            "allowed_team": allowed_team,
        }
        self.state.transition_to(GameState.READING_TOSSUP)
        self.manual_read_started = asyncio.get_running_loop().time()
        self.manual_dead_action = False
        self.manual_read_complete = bool(skip_read)
        self.manual_buzzpoint = "Full Read" if skip_read else ""
        self.manual_after_read = bool(skip_read)

        await self.outbound_queue.put({"type": "UPDATE_QUESTION", "payload": {
            "text": tossup_text if tossup_text else "[WARNING: PYTHON MEMORY HAS NO TEXT FOR THIS QUESTION]",
            "raw_text": tossup_text,
            "answer": tossup_answer,
            "category": category,
            "options": options,
            "visual": visual,
            "manual": True,
            "phase": "TOSSUP",
            "allowed_team": allowed_team,
        }})

        if skip_read:
            await self._set_manual_phase("BUZZ_WINDOW", 5.0)
            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "⏳ 5 Seconds to Buzz..."}})
        else:
            await self._set_manual_phase("READING", None)
            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "🔊 Read the tossup aloud. Buzzes are live; press Dead when the question dies."}})

        deadline = asyncio.get_running_loop().time() + 5.0 if skip_read else None
        buzz_event = None
        while True:
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                await self._set_manual_phase("IDLE")
                return None, False, False, False, 0.0, ""
            event = await self.tossup_session.wait_for_event(0.25 if deadline is not None else 0.5)
            if event is None or event is self.tossup_session.HANDLED:
                continue
            evt_type = event.type.name if hasattr(event.type, "name") else str(event.type)
            if evt_type in ("FORCE_RESET_STATE", "CHALLENGE"):
                await self._set_manual_phase("IDLE")
                return None, False, False, evt_type, 0.0, ""
            if evt_type == "DEAD_TOSSUP":
                self.manual_dead_action = True
                await self._set_manual_phase("IDLE")
                await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "💀 Tossup dead — no buzz."}})
                return None, False, False, False, 0.0, ""
            if evt_type == "BUZZ":
                team = event.payload.get("team", "Unknown")
                if allowed_team and team != allowed_team:
                    continue
                if event.role not in (None, "owner"):
                    continue
                if self.state.handle_buzz(team, 0, not self.manual_read_complete):
                    buzz_event = event
                    break

        raw_team = buzz_event.payload.get("team", "Unknown Team")
        raw_player = buzz_event.payload.get("player", "Captain")
        team = raw_team if self.online_mode else self.custom_teams.get(raw_team, raw_team)
        player = raw_player if self.online_mode else self.custom_roster.get(raw_player, raw_player)
        self.answering_user_id = None
        buzz_time = max(0.0, asyncio.get_running_loop().time() - self.manual_read_started)
        self.manual_after_read = bool(self.manual_read_complete)
        self.manual_buzzpoint = "Full Read" if self.manual_after_read else ""

        self.state.transition_to(GameState.LISTENING_TOSSUP_ANSWER)
        await self.outbound_queue.put({"type": "BUZZ", "payload": {"team": team, "player": player, "raw_team": raw_team, "raw_player": raw_player, "after_read": self.manual_after_read, "source": str(buzz_event.payload.get("source", "manual"))}})
        await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": f"🎯 BUZZ: {player} ({team}) — mark the buzzpoint, then judge the answer."}})
        await self._set_manual_phase("ANSWERING", ANSWER_WINDOW_SECONDS, buzzpoint=self.manual_buzzpoint, after_read=self.manual_after_read)

        correct = None
        while correct is None:
            event = await self.tossup_session.wait_for_event(0.25)
            if event is None or event is self.tossup_session.HANDLED:
                continue
            evt_type = event.type.name if hasattr(event.type, "name") else str(event.type)
            if evt_type in ("FORCE_RESET_STATE", "CHALLENGE"):
                await self._set_manual_phase("IDLE")
                return None, False, False, evt_type, 0.0, ""
            if evt_type == "LOG_BUZZPOINT":
                after_read = bool(event.payload.get("after_read", False)) or self.manual_read_complete
                word = str(event.payload.get("buzzpoint", "") or "").strip()
                if after_read:
                    self.manual_after_read = True
                    self.manual_buzzpoint = "Full Read"
                elif word:
                    self.manual_after_read = False
                    self.manual_buzzpoint = word
                else:
                    continue
                self.state.is_interrupted = not self.manual_after_read
                await self.outbound_queue.put({"type": "BUZZPOINT_SET", "payload": {
                    "buzzpoint": self.manual_buzzpoint,
                    "after_read": self.manual_after_read,
                    "points_if_wrong": 0 if self.manual_after_read else -4,
                }})
                await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": (
                    "📍 Buzzpoint: Full read — a wrong answer is INCORRECT (0)."
                    if self.manual_after_read else
                    f"📍 Buzzpoint: '{self.manual_buzzpoint}' — a wrong answer is a NEG (-4)."
                )}})
                continue
            if evt_type == "FORCE_JUDGMENT":
                if not self.manual_buzzpoint:
                    await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "⚠️ Mark the buzzpoint (or Full read) before judging."}})
                    continue
                correct = bool(event.payload.get("correct"))

        self.state.is_interrupted = not self.manual_after_read
        self.state.transition_to(GameState.EVALUATING_TOSSUP)
        await self._set_manual_phase("IDLE")
        return buzz_event.payload, correct, not self.manual_after_read, False, buzz_time, self.manual_buzzpoint

    async def _handle_manual_bonus(self, q_data, team):
        """A bonus read by the human reader, with a manual +10/0 verdict."""
        self._drop_stale_manual_events()
        bonus_text, bonus_answer, options, visual = self._manual_read_text(q_data, bonus=True)
        category = str(q_data.get("category", "GENERAL") or "GENERAL").strip()
        self.manual_question = {
            "phase": "BONUS",
            "raw_text": bonus_text,
            "answer": bonus_answer,
            "category": category,
            "options": options,
            "visual": visual,
            "team": team,
        }
        await self.outbound_queue.put({"type": "UPDATE_QUESTION", "payload": {
            "text": bonus_text if bonus_text else "[WARNING: PYTHON MEMORY HAS NO BONUS TEXT]",
            "raw_text": bonus_text,
            "answer": bonus_answer,
            "category": category,
            "options": options,
            "visual": visual,
            "manual": True,
            "phase": "BONUS",
            "team": team,
        }})
        await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": f"🌟 BONUS for {team}. Read the bonus aloud, then start the 20s confer clock."}})

        confer_started = not bool(bonus_text)
        if confer_started:
            await self._set_manual_phase("BONUS_CONFER", 20.0, team=team)
            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "⏳ Conferring (20s). Mark the bonus correct or incorrect when ready."}})
        else:
            await self._set_manual_phase("BONUS_READ", None, team=team)

        while True:
            event = await self.tossup_session.wait_for_event(0.25)
            if event is None or event is self.tossup_session.HANDLED:
                continue
            evt_type = event.type.name if hasattr(event.type, "name") else str(event.type)
            if evt_type in ("FORCE_RESET_STATE", "CHALLENGE"):
                await self._set_manual_phase("IDLE")
                return None, evt_type
            if evt_type == "MANUAL_READ_DONE" and not confer_started:
                confer_started = True
                await self._set_manual_phase("BONUS_CONFER", 20.0, team=team)
                await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "⏳ Conferring (20s). Mark the bonus correct or incorrect when ready."}})
                continue
            if evt_type == "FORCE_JUDGMENT":
                await self._set_manual_phase("IDLE", team=team)
                return bool(event.payload.get("correct")), False

    async def _handle_tossup_attempt(self, q_data, allowed_team=None, skip_read=False):
        tossup_text = str(q_data.get('tossup_text', '')).strip()
        tossup_answer = str(q_data.get('tossup_answer', '')).strip()
        category = str(q_data.get('category', 'GENERAL')).strip()
        
        # Format multiple choice options for TTS
        tossup_options = q_data.get('tossup_options', [])
        options_spoken = " ".join(tossup_options) if tossup_options else ""
        
        prefix = f"Reading for {allowed_team} only. " if allowed_team else ""
        text_to_read = prefix + f"Tossup,  " + tossup_text + "  " + options_spoken
        
        safe_display_text = text_to_read if text_to_read else "[WARNING: PYTHON MEMORY HAS NO TEXT FOR THIS QUESTION]"

        self.state.transition_to(GameState.READING_TOSSUP)
        self._set_camera_window(True)

        await self.outbound_queue.put({"type": "UPDATE_QUESTION", "payload": {
            "text": safe_display_text,
            "category": category,
            "options": q_data.get('tossup_options', []),
            "visual": q_data.get('tossup_visual', False)
        }})
        
        await self._set_online_buzz_window(True, team=allowed_team)
        shared_read = False
        if not skip_read:
            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "🔊 Reading Tossup..."}})
            if self.use_audio and text_to_read:
                self.read_session.drop_stale_reports(self.inbound_queue)
                reading_id = self._new_reading_id()
                reading_payload = {"text": text_to_read}
                if reading_id:
                    reading_payload["reading_id"] = reading_id
                if self.reading_mode == "moderator":
                    # One reader for the room: players do not speak this tossup, so the
                    # owner's completion is everyone's read end (the interrupt boundary).
                    reading_payload["moderator_only"] = True
                    shared_read = True
                self._set_read_window(text_to_read)
                if shared_read:
                    await self._set_moderator_reading(True, "tossup")
                await self.outbound_queue.put({"type": "START_READING", "payload": reading_payload})
            else:
                print("[DEBUG]: Audio skipped (disabled or text was empty). Jumping to Buzz phase.")
                self._set_read_window("", immediate=True)
                self.inbound_queue.put_nowait(Event(type=EventType.READING_DONE, payload={}))
        else:
            # Instantly bypass the reading loop and jump straight to the 5-second buzz countdown
            self._set_read_window("", immediate=True)
            self.inbound_queue.put_nowait(Event(type=EventType.READING_DONE, payload={}))
        
        buzz_event = None
        is_interrupt = False
        buzz_time = 0.0
        buzzpoint_word = "Full Read"
        read_warning = None
        judge_failure = None
        
        async def handle_read_event(event):
            evt_type = event.type.name if hasattr(event.type, 'name') else str(event.type)
            if evt_type in ("FORCE_RESET_STATE", "CHALLENGE"):
                self._set_camera_window(False)
                await self._set_online_buzz_window(False)
                if shared_read:
                    await self._set_moderator_reading(False)
                return evt_type
            if evt_type != "BUZZ":
                return None
            team = event.payload.get('team', 'Unknown')
            if allowed_team and team != allowed_team:
                return None
            if self.online_mode and event.role not in ("owner", "player"):
                return None
            if not self.state.handle_buzz(team, 0, True):
                return None
            await self._set_online_buzz_window(False)
            if shared_read:
                await self._set_moderator_reading(False)
            return "buzz"

        start_time = asyncio.get_running_loop().time()
        outcome, event = await self.read_session.wait_for_report(
            self.inbound_queue,
            self.event_pump,
            online_mode=self.online_mode,
            on_other=handle_read_event,
            on_early=self.note_impossible_read_report,
        )
        if outcome == "timeout":
            print("[DEBUG]: Read watchdog expired; opening the buzz window.")
            read_warning = READ_TIMEOUT_WARNING
            if shared_read:
                await self._set_moderator_reading(False)
        elif outcome in ("FORCE_RESET_STATE", "CHALLENGE"):
            return None, False, False, outcome, 0.0, ""
        elif outcome == "buzz":
            buzz_event = event
            is_interrupt = True
        elif outcome == "failed":
            read_warning = READ_FAILED_WARNING
            if shared_read:
                await self._set_moderator_reading(False)
        elif outcome == "done" and shared_read:
            await self._set_moderator_reading(False)

        if not buzz_event:
            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "⏳ 5 Seconds to Buzz..."}})
            if read_warning:
                # The buzz window still opens so the match continues, but the moderator
                # must know why no question was heard.
                await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": read_warning}})
            start_time = asyncio.get_running_loop().time() 
            try:
                while True:
                    elapsed = asyncio.get_running_loop().time() - start_time
                    remaining = 5.0 - elapsed
                    if remaining <= 0:
                        raise asyncio.TimeoutError()

                    event = await self.tossup_session.wait_for_event(remaining)
                    if event is None:
                        raise asyncio.TimeoutError()
                    if event is self.tossup_session.HANDLED:
                        continue
                    evt_type = event.type.name if hasattr(event.type, 'name') else str(event.type)

                    if evt_type == "FORCE_RESET_STATE":
                        self._set_camera_window(False)
                        await self._set_online_buzz_window(False)
                        return None, False, False, True, 0.0, ""
                    elif evt_type == "BUZZ":
                        team = event.payload.get('team', 'Unknown')
                        if allowed_team and team != allowed_team:
                            continue
                        if self.online_mode and event.role not in ("owner", "player"):
                            continue
                        if self.state.handle_buzz(team, 0, False): 
                            buzz_event = event
                            buzz_time = asyncio.get_running_loop().time() - start_time 
                            await self._set_online_buzz_window(False)
                            break
            except asyncio.TimeoutError:
                self._set_camera_window(False)
                await self._set_online_buzz_window(False)
                return None, False, False, False, 0.0, ""

        self._set_camera_window(False)
        raw_team = buzz_event.payload.get('team', 'Unknown Team')
        raw_player = buzz_event.payload.get('player', 'Captain')
        
        team = raw_team if self.online_mode else self.custom_teams.get(raw_team, raw_team)
        player = raw_player if self.online_mode else self.custom_roster.get(raw_player, raw_player)
        self.answering_user_id = buzz_event.payload.get("user_id") if self.online_mode else None
        
        self.state.transition_to(GameState.LISTENING_TOSSUP_ANSWER) 
        await self.outbound_queue.put({"type": "BUZZ", "payload": {"team": team, "player": player, "source": str(buzz_event.payload.get("source", "manual"))}})
        
        if is_interrupt and self.use_audio:
            wait_start = asyncio.get_running_loop().time()
            try:
                while asyncio.get_running_loop().time() - wait_start < 1.5:
                    time_left = 1.5 - (asyncio.get_running_loop().time() - wait_start)
                    if time_left <= 0: break
                    
                    reply = await self.tossup_session.wait_for_event(time_left)
                    if reply is None:
                        break
                    if reply is self.tossup_session.HANDLED:
                        continue
                    evt_type = reply.type.name if hasattr(reply.type, 'name') else str(reply.type)
                    
                    if evt_type == "LOG_BUZZPOINT":
                        buzzpoint_word = reply.payload.get("buzzpoint", "")
                        break
                    self.inbound_queue.put_nowait(reply)
                    await asyncio.sleep(0.05)
            except asyncio.TimeoutError:
                pass

        player_str = f"{player}" if player else ""
        phrase = f"{player_str}"
        await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": f"🔊 {phrase}"}})
        
        if self.use_audio:
            self.read_session.drop_stale_reports(self.inbound_queue)

            reading_id = self._new_reading_id()
            reading_payload = {"text": phrase}
            if reading_id:
                reading_payload["reading_id"] = reading_id
            if self.reading_mode == "moderator":
                reading_payload["moderator_only"] = True
            self._set_read_window(phrase)
            await self.outbound_queue.put({"type": "START_READING", "payload": reading_payload})

            
            outcome, _ = await self.read_session.wait_for_report(
                self.inbound_queue,
                self.event_pump,
                online_mode=self.online_mode,
                requeue_other=True,
            )
            if outcome == "timeout":
                print("[DEBUG]: Read watchdog expired; continuing without a report.")
                self.read_session.current_reading_id = None
        
        spoken_answer = ""
        if self.use_audio:
            current_sys_time = time.time()
            self.answer_audio_open = True
            await self.outbound_queue.put({
                "type": "START_LISTENING", 
                "payload": {"timeout": ANSWER_WINDOW_SECONDS, "expires_at": current_sys_time + ANSWER_WINDOW_SECONDS, **({"target_user_id": self.answering_user_id} if self.online_mode else {})}
            })
            try:
                while True:
                    reply = await self.tossup_session.wait_for_event(12.0)
                    if reply is None:
                        raise asyncio.TimeoutError()
                    if reply is self.tossup_session.HANDLED:
                        continue
                    evt_type = reply.type.name if hasattr(reply.type, 'name') else str(reply.type)
                    
                    if evt_type == "ANSWER_AUDIO" and (not self.online_mode or reply.user_id == self.answering_user_id):
                        self.answer_audio_open = False
                        await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "⏳ Transcribing Tossup..."}})
                        b64_audio = reply.payload.get("audio_data", "")
                        try:
                            spoken_answer = await self._transcribe_with_judge(b64_audio)
                        except JudgeUnavailable as exc:
                            judge_failure = str(exc)
                        else:
                            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": f"🗣️ Heard: '{spoken_answer}'"}})
                        break
            except asyncio.TimeoutError:
                spoken_answer = ""
            finally:
                self.answer_audio_open = False
        else:
            await asyncio.sleep(1)
            spoken_answer = "Mitochondria"
            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": f"🎤 Capturing Mock Mic: '{spoken_answer}'"}})
        
        self.state.transition_to(GameState.EVALUATING_TOSSUP) 

        correct = None
        if judge_failure is None:
            try:
                correct = await self._evaluate_with_judge(
                    spoken_answer,
                    tossup_answer,
                    category,
                    is_multiple_choice=q_data.get('type', '') == 'MC'
                )
            except JudgeUnavailable as exc:
                judge_failure = str(exc)
            
        self.current_reading_id = None
        payload = dict(buzz_event.payload)
        if judge_failure is not None:
            # A lost judge is not a lost answer: the caller must leave it unjudged.
            payload["judge_unavailable"] = judge_failure
            return payload, None, is_interrupt, False, buzz_time, buzzpoint_word
        return payload, correct, is_interrupt, False, buzz_time, buzzpoint_word

    async def run_game_loop(self):
        await asyncio.sleep(1)

        try:

            while True:
                try:
                    if not self.question_bank or self.current_q_idx >= len(self.question_bank):
                        if self.question_bank and self.current_q_idx >= len(self.question_bank):
                            await self._notify_match_complete()
                        if self.ui_state.get("show") != "NONE":
                            # Nothing can be started with an empty packet: hide the start controls.
                            await self._set_ui_state("NONE")
                        # NEW: Process the queue so LOAD_BANK and SAVE_ROSTER don't get stuck forever
                        while not self.inbound_queue.empty():
                            evt = self.inbound_queue.get_nowait()
                            evt_type = evt.type.name if hasattr(evt.type, 'name') else str(evt.type)
                            # A manual start that arrives with nothing to read would otherwise
                            # vanish without a trace: tell the moderator what is missing.
                            if evt_type in MANUAL_START_EVENTS:
                                await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": self._idle_start_message()}})
                                continue
                            await self.process_admin_event(evt)
                    
                        await asyncio.sleep(1)
                        continue
                    q_data = self.question_bank[self.current_q_idx]
                
                    await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": f"✅ Ready for Q{self.current_q_idx+1}. Click 'Start Tossup'"}})
                
                    safe_preload_text = str(q_data.get('tossup_text', '')).strip()
                    await self.outbound_queue.put({"type": "UPDATE_QUESTION", "payload": {
                        "text": safe_preload_text if safe_preload_text else "[WARNING: PYTHON MEMORY HAS NO TEXT FOR THIS QUESTION]", 
                        "raw_text": safe_preload_text,
                        "answer": str(q_data.get('tossup_answer', '')).strip(),
                        "category": str(q_data.get('category', 'GENERAL')),
                        "options": q_data.get('tossup_options', []),
                        "visual": q_data.get('tossup_visual', False),
                        "manual": self.manual_reader,
                        "phase": "TOSSUP",
                        "preview": True,
                    }})
                
                    # Arm the moderator's start control only once a question is actually loaded.
                    await self._set_ui_state("TOSSUP")
                
                    action = await self.wait_for_moderator_action(["MANUAL_START_TOSSUP", "OVERRIDE_LAST_QUESTION"])
                

                    action_type = action.type.name if hasattr(action.type, 'name') else str(action.type)
                    if action_type == "OVERRIDE_LAST_QUESTION":
                        await self._rewind_last_question()
                        continue
                    if action_type == "FORCE_RESET_STATE": continue 
                    elif action_type == "CHALLENGE":
                        await self.log_event("CHALLENGE", "None", "Moderator", False, 0, "Question Dead", 0.0)
                        self.state.next_question()
                        self.current_q_idx += 1
                        continue

                    # Without a judge the AI cannot adjudicate a tossup. Say so instead
                    # of reading it and later turning the failure into a penalty.
                    if not self.manual_reader and not self.judge:
                        await self._announce_judge_unavailable(
                            JUDGE_NOT_CONFIGURED_WARNING,
                            "No AI judge API key is configured.",
                        )
                        continue

                    # HIDE BUTTONS WHILE READING/EVALUATING
                    await self._set_ui_state("NONE")

                    if self.manual_reader:
                        payload, correct, is_interrupt, interrupt_action, buzz_time, buzz_text = await self._handle_manual_tossup_attempt(q_data)
                    else:
                        payload, correct, is_interrupt, interrupt_action, buzz_time, buzz_text = await self._handle_tossup_attempt(q_data)
                
                    if interrupt_action == "FORCE_RESET_STATE": 
                        continue
                    elif interrupt_action == "CHALLENGE":
                        await self.log_event("CHALLENGE", "None", "Moderator", False, 0, "Question Dead", 0.0)
                        await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "⚠️ CHALLENGE! Clock paused. Edit scoresheet, then Start Next Tossup."}})
                        self.state.next_question()
                        self.current_q_idx += 1
                        continue

                    if payload and correct is None:
                        await self._advance_unjudged(
                            payload.get('team', 'Unknown Team'),
                            payload.get('player', 'Captain'),
                            buzz_text,
                            buzz_time,
                            payload.get('judge_unavailable', ''),
                        )
                        continue
                
                    bonus_earned_by = None
                    tossup_ans_text = str(q_data.get('tossup_answer', ''))

                    if payload and correct:
                        bonus_earned_by = payload['team']
                        self.state.apply_tossup_points(True) 
                        await self.log_event("TOSSUP", payload['team'], payload['player'], True, 4, buzz_text, buzz_time)
                        await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": f"✅ Correct! 4 points to {bonus_earned_by}."}})
                    
                        await self._speak_and_wait("That is correct.")

                    elif payload and not correct:
                        team = payload['team']
                        self.state.apply_tossup_points(False) 
                        other_team = "Team B" if team == "Team A" else "Team A"
                        skip_rebound = False
                    
                        if is_interrupt:
                            # Interrupt Penalty: -4 points and a pause for Moderator Override / Re-read
                            await self.log_event("TOSSUP", team, payload['player'], False, -4, buzz_text, buzz_time)
                            await self.outbound_queue.put({"type": "UPDATE_SCORE", "payload": {"score_a": self.state.team_a_score, "score_b": self.state.team_b_score}})
                            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": f"❌ NEG! Penalty applied. Waiting for Re-read..."}})
                        
                            await self._speak_and_wait("That is incorrect.")
                            await asyncio.sleep(1)
                        
                            # --- PAUSE FOR RE-READ OR OVERRIDE ---
                            await self._set_ui_state("REREAD", overrides=True)
                            action = await self.wait_for_moderator_action(["MANUAL_REREAD_TOSSUP", "FORCE_JUDGMENT"])
                            action_type = action.type.name if hasattr(action.type, 'name') else str(action.type)
                        
                            if action_type == "FORCE_JUDGMENT" and action.payload.get("correct"):
                                # REWIND STATE: Refund the -4, award +4, change to correct, and jump to bonus
                                self.state.apply_tossup_points(True)
                                if team == "Team A": self.state.team_a_score += 4
                                else: self.state.team_b_score += 4
                            
                                self.match_log[-1].update({"Correct": True, "Points": 4, "Score_A": self.state.team_a_score, "Score_B": self.state.team_b_score, "Edited": True})
                                await self.outbound_queue.put({"type": "NEW_LOG_ENTRY", "payload": self.match_log[-1]})
                            
                                bonus_earned_by = team
                                skip_rebound = True
                                await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "🔧 Override: Marked Correct! Jumping to Bonus."}})
                            elif action_type == "CHALLENGE":
                                await self.log_event("CHALLENGE", "None", "Moderator", False, 0, "Question Dead", 0.0)
                                await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "⚠️ CHALLENGE! Clock paused. Edit scoresheet, then Start Next Tossup."}})
                                self.state.next_question()
                                self.current_q_idx += 1
                                continue
                        
                            if not skip_rebound:
                                await self._set_ui_state("NONE")
                                if self.manual_reader:
                                    payload2, correct2, is_int2, interrupt_action2, buzz_time2, buzz_text2 = await self._handle_manual_tossup_attempt(q_data, allowed_team=other_team)
                                else:
                                    payload2, correct2, is_int2, interrupt_action2, buzz_time2, buzz_text2 = await self._handle_tossup_attempt(q_data, allowed_team=other_team)
                        
                        else:
                            # Full Read (No Penalty): 0 points, opposing team immediately gets 5 seconds
                            await self.log_event("TOSSUP", team, payload['player'], False, 0, buzz_text, buzz_time)
                            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": f"❌ Incorrect. 5 seconds for {other_team}."}})
                        
                            await self._speak_and_wait("That is incorrect.")
                        
                            if self.manual_reader:
                                payload2, correct2, is_int2, interrupt_action2, buzz_time2, buzz_text2 = await self._handle_manual_tossup_attempt(q_data, allowed_team=other_team, skip_read=True)
                            else:
                                payload2, correct2, is_int2, interrupt_action2, buzz_time2, buzz_text2 = await self._handle_tossup_attempt(q_data, allowed_team=other_team, skip_read=True)

                        # --- EVALUATE THE REBOUND ATTEMPT ---
                        if not skip_rebound:
                            if interrupt_action2 == "FORCE_RESET_STATE": 
                                continue
                            elif interrupt_action2 == "CHALLENGE":
                                await self.log_event("CHALLENGE", "None", "Moderator", False, 0, "Question Dead", 0.0)
                                await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "⚠️ CHALLENGE! Clock paused. Edit scoresheet, then Start Next Tossup."}})
                                self.state.next_question()
                                self.current_q_idx += 1
                                continue

                            if payload2 and correct2 is None:
                                await self._advance_unjudged(
                                    payload2.get('team', other_team),
                                    payload2.get('player', 'Captain'),
                                    buzz_text2,
                                    buzz_time2,
                                    payload2.get('judge_unavailable', ''),
                                )
                                continue
                            
                            if payload2 and correct2:
                                bonus_earned_by = other_team
                                self.state.apply_tossup_points(True) 
                                await self.log_event("TOSSUP_REBOUND", other_team, payload2['player'], True, 4, buzz_text2, buzz_time2)
                                await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": f"✅ Correct! 4 points to {other_team}."}})
                                await self._speak_and_wait("That is correct.")
                            
                            elif payload2 and not correct2:
                                self.state.apply_tossup_points(False) 
                                if is_int2:
                                    await self.log_event("TOSSUP_REBOUND", other_team, payload2['player'], False, -4, buzz_text2, buzz_time2)
                                    await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "❌ DOUBLE NEG! Penalty applied."}})
                                else:
                                    await self.log_event("TOSSUP_REBOUND", other_team, payload2['player'], False, 0, buzz_text2, buzz_time2)
                                    await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "❌ Incorrect. No penalty."}})
                                await self._speak_and_wait(f"That is incorrect. The correct answer is {tossup_ans_text}.", moderator_only=True)
                            
                            else:
                                await self.log_event("DEAD_TOSSUP", "None", "None", False, 0, "Time", 0.0)
                                await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "⏰ Time expired on rebound."}})
                                await self._speak_and_wait(f"Time. The correct answer is {tossup_ans_text}.", moderator_only=True)

                    else:
                        dead_by_moderator = bool(getattr(self, "manual_dead_action", False))
                        await self.log_event("DEAD_TOSSUP", "None", "None", False, 0, "Dead" if dead_by_moderator else "Time", 0.0)
                        await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "💀 Tossup dead — no buzz." if dead_by_moderator else "⏰ Time expired. No buzz."}})
                        await self._speak_and_wait((f"Dead tossup. The correct answer is {tossup_ans_text}." if dead_by_moderator else f"Time. The correct answer is {tossup_ans_text}."), moderator_only=True)

                    await self.outbound_queue.put({"type": "UPDATE_SCORE", "payload": {"score_a": self.state.team_a_score, "score_b": self.state.team_b_score}})

                    if bonus_earned_by and self.manual_reader:
                        self.state.transition_to(GameState.READING_BONUS)
                        await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": f"🌟 BONUS for {bonus_earned_by}. Click 'Start Bonus'"}})

                        safe_bonus_preload = str(q_data.get('bonus_text', '')).strip()
                        await self.outbound_queue.put({"type": "UPDATE_QUESTION", "payload": {
                            "text": safe_bonus_preload if safe_bonus_preload else "[WARNING: PYTHON MEMORY HAS NO BONUS TEXT]",
                            "raw_text": safe_bonus_preload,
                            "answer": str(q_data.get('bonus_answer', '')).strip(),
                            "category": str(q_data.get('category', 'GENERAL')),
                            "options": q_data.get('bonus_options', []),
                            "visual": q_data.get('bonus_visual', False),
                            "manual": self.manual_reader,
                            "phase": "BONUS"
                        }})
                        await self._set_ui_state("BONUS")
                        action = await self.wait_for_moderator_action(["MANUAL_START_BONUS", "OVERRIDE_LAST_QUESTION"])
                        action_type = action.type.name if hasattr(action.type, 'name') else str(action.type)
                        if action_type == "OVERRIDE_LAST_QUESTION":
                            await self._rewind_last_question()
                            continue
                        if action_type == "FORCE_RESET_STATE":
                            continue
                        if action_type == "CHALLENGE":
                            await self.log_event("CHALLENGE", "None", "Moderator", False, 0, "Bonus Dead", 0.0)
                            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "⚠️ CHALLENGE! Clock paused. Edit scoresheet, then Start Next Tossup."}})
                            self.state.next_question()
                            self.current_q_idx += 1
                            continue

                        await self._set_ui_state("NONE")
                        bonus_correct, bonus_action = await self._handle_manual_bonus(q_data, bonus_earned_by)
                        if bonus_action == "FORCE_RESET_STATE":
                            continue
                        if bonus_action == "CHALLENGE":
                            await self.log_event("CHALLENGE", "None", "Moderator", False, 0, "Bonus Dead", 0.0)
                            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "⚠️ CHALLENGE! Clock paused. Edit scoresheet, then Start Next Tossup."}})
                            self.state.next_question()
                            self.current_q_idx += 1
                            continue

                        self.state.transition_to(GameState.EVALUATING_BONUS)
                        self.state.apply_bonus_points(bool(bonus_correct))
                        bonus_ans_text = str(q_data.get('bonus_answer', ''))
                        if bonus_correct:
                            await self.log_event("BONUS", bonus_earned_by, "Captain", True, 10, "Full Read", 0.0)
                            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "✅ Bonus Correct! +10 points."}})
                        else:
                            await self.log_event("BONUS", bonus_earned_by, "Captain", False, 0, "Full Read", 0.0)
                            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": f"❌ Bonus Incorrect. The correct answer is {bonus_ans_text}."}})
                        await self.outbound_queue.put({"type": "UPDATE_SCORE", "payload": {"score_a": self.state.team_a_score, "score_b": self.state.team_b_score}})

                    elif bonus_earned_by:
                        self.state.transition_to(GameState.READING_BONUS) 
                        await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": f"🌟 BONUS for {bonus_earned_by}. Click 'Start Bonus'"}})
                    
                        safe_bonus_preload = str(q_data.get('bonus_text', '')).strip()
                        await self.outbound_queue.put({"type": "UPDATE_QUESTION", "payload": {
                            "text": safe_bonus_preload if safe_bonus_preload else "[WARNING: PYTHON MEMORY HAS NO BONUS TEXT]", 
                            "category": str(q_data.get('category', 'GENERAL')),
                            "options": q_data.get('bonus_options', []),
                            "visual": q_data.get('bonus_visual', False)
                        }})
                    
                        # TELL UI TO ONLY SHOW BONUS BUTTON
                        await self._set_ui_state("BONUS")
                    
                        action = await self.wait_for_moderator_action(["MANUAL_START_BONUS", "OVERRIDE_LAST_QUESTION"])
                        action_type = action.type.name if hasattr(action.type, 'name') else str(action.type)
                        if action_type == "OVERRIDE_LAST_QUESTION":
                            await self._rewind_last_question()
                            continue
                        if action_type == "FORCE_RESET_STATE": continue 
                        elif action_type == "CHALLENGE":
                            await self.log_event("CHALLENGE", "None", "Moderator", False, 0, "Bonus Dead", 0.0)
                            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "⚠️ CHALLENGE! Clock paused. Edit scoresheet, then Start Next Tossup."}})
                            self.state.next_question()
                            self.current_q_idx += 1
                            continue

                        # HIDE BUTTONS WHILE READING/EVALUATING
                        await self._set_ui_state("NONE")

                        await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "🔊 Reading Bonus..."}})
                    
                        raw_bonus = str(q_data.get('bonus_text', '')).strip()
                        bonus_options = q_data.get('bonus_options', [])
                        bonus_options_spoken = " ".join(bonus_options) if bonus_options else ""
                    
                        bonus_text_to_read = f"Bonus, " + raw_bonus + "  " + bonus_options_spoken if raw_bonus else ""
                    
                        if self.use_audio and bonus_text_to_read:
                            await self._speak_and_wait(bonus_text_to_read, announce_kind="bonus")

                        self.state.transition_to(GameState.BONUS_CONFERRING) 
                        spoken_bonus = ""
                        bonus_judge_failure = None
                        if self.online_mode:
                            self.answering_user_id = None
                    
                        if self.use_audio:
                            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "⏳ Conferring (20s). Buzz to answer!"}})
                            await self._set_online_buzz_window(True, team=bonus_earned_by, captain_only=True)
                        
                            start_bonus_time = asyncio.get_running_loop().time()
                            buzzed_in = False
                            challenge_triggered = False
                        
                            # --- CAPTAIN'S BUZZ / 20 SEC TIMEOUT LOGIC ---
                            while True:
                                elapsed = asyncio.get_running_loop().time() - start_bonus_time
                                remaining = 20.0 - elapsed
                            
                                if remaining <= 0:
                                    break
                                
                                try:
                                    reply = await asyncio.wait_for(self.inbound_queue.get(), timeout=remaining)
                                    evt_type = reply.type.name if hasattr(reply.type, 'name') else str(reply.type)
                                
                                    if evt_type == "BUZZ":
                                        if not self.online_mode or (reply.payload.get("team") == bonus_earned_by and reply.payload.get("captain")):
                                            buzzed_in = True
                                            if self.online_mode:
                                                self.answering_user_id = reply.user_id
                                            await self._set_online_buzz_window(False)
                                            break
                                    elif evt_type == "CHALLENGE":
                                        await self._set_online_buzz_window(False)
                                        challenge_triggered = True
                                        break
                                    elif await self.event_pump.dispatch_admin(reply):
                                        continue
                                except asyncio.TimeoutError:
                                    break
                            if self.online_mode and not buzzed_in:
                                await self._set_online_buzz_window(False)
                                
                            if challenge_triggered:
                                await self.log_event("CHALLENGE", "None", "Moderator", False, 0, "Bonus Dead", 0.0)
                                await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "⚠️ CHALLENGE! Clock paused. Edit scoresheet, then Start Next Tossup."}})
                                self.state.next_question()
                                self.current_q_idx += 1
                                continue

                            # --- DYNAMIC LISTEN TIMEOUT (ANSWER WINDOW OR REMAINING TIME) ---
                            elapsed_confer = asyncio.get_running_loop().time() - start_bonus_time
                            remaining_confer = max(0.0, 20.0 - elapsed_confer)
                            listen_time = min(ANSWER_WINDOW_SECONDS, remaining_confer)
                            if self.online_mode and not buzzed_in:
                                listen_time = 0.0
                        
                            if listen_time > 0.5: # Only open mic if there is meaningful time left
                                current_sys_time = time.time()
                                target_user_id = self.answering_user_id if self.online_mode else None
                                if not self.online_mode or (buzzed_in and target_user_id):
                                    self.answer_audio_open = True
                                    await self.outbound_queue.put({
                                        "type": "START_LISTENING", 
                                        "payload": {"timeout": listen_time, "expires_at": current_sys_time + listen_time, **({"target_user_id": target_user_id} if self.online_mode else {})}
                                    })
                            
                                try:
                                    while True:
                                        reply = await asyncio.wait_for(self.inbound_queue.get(), timeout=listen_time + 4.0)
                                        evt_type = reply.type.name if hasattr(reply.type, 'name') else str(reply.type)
                                    
                                        if evt_type == "ANSWER_AUDIO" and (not self.online_mode or reply.user_id == self.answering_user_id):
                                            self.answer_audio_open = False
                                            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "⏳ Transcribing Bonus..."}})
                                            b64_audio = reply.payload.get("audio_data", "")
                                            try:
                                                spoken_bonus = await self._transcribe_with_judge(b64_audio)
                                            except JudgeUnavailable as exc:
                                                bonus_judge_failure = str(exc)
                                            else:
                                                await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": f"🗣️ Heard: '{spoken_bonus}'"}})
                                            break
                                        elif evt_type == "CHALLENGE":
                                            challenge_triggered = True
                                            break
                                        elif await self.event_pump.dispatch_admin(reply):
                                            continue
                                except asyncio.TimeoutError:
                                    spoken_bonus = ""
                                finally:
                                    self.answer_audio_open = False
                                
                            if challenge_triggered:
                                await self.log_event("CHALLENGE", "None", "Moderator", False, 0, "Bonus Dead", 0.0)
                                await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "⚠️ CHALLENGE! Clock paused. Edit scoresheet, then Start Next Tossup."}})
                                self.state.next_question()
                                self.current_q_idx += 1
                                continue
                            
                        else:
                            await asyncio.sleep(1)
                            spoken_bonus = "Chloroplast"

                        self.state.transition_to(GameState.EVALUATING_BONUS) 
                        bonus_ans_text = str(q_data.get('bonus_answer', ''))

                        bonus_correct = None
                        if bonus_judge_failure is None:
                            try:
                                bonus_correct = await self._evaluate_with_judge(
                                    spoken_bonus,
                                    bonus_ans_text,
                                    str(q_data.get('category', 'GENERAL')),
                                )
                            except JudgeUnavailable as exc:
                                bonus_judge_failure = str(exc)

                        if bonus_judge_failure is not None:
                            await self._advance_unjudged(
                                bonus_earned_by,
                                "Captain",
                                "Full Read",
                                0.0,
                                bonus_judge_failure,
                            )
                            continue
                    
                        self.state.apply_bonus_points(bonus_correct) 
                        if bonus_correct:
                            await self.log_event("BONUS", bonus_earned_by, "Captain", True, 10, "Full Read", 0.0)
                            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "✅ Bonus Correct! +10 points."}})
                            await self._speak_and_wait("That is correct.")
                        else:
                            await self.log_event("BONUS", bonus_earned_by, "Captain", False, 0, "Full Read", 0.0)
                            await self.outbound_queue.put({"type": "UPDATE_STATUS", "payload": {"text": "❌ Bonus Incorrect."}})
                            await self._speak_and_wait(f"That is incorrect. The correct answer is {bonus_ans_text}.", moderator_only=True)
                        
                        await self.outbound_queue.put({"type": "UPDATE_SCORE", "payload": {"score_a": self.state.team_a_score, "score_b": self.state.team_b_score}})
                    
                    self.state.next_question() 
                    self.current_q_idx += 1
                    await self.outbound_queue.put({"type": "SYNC_LOG", "payload": {"log": self.match_log}})
                    self.answering_user_id = None
                    self.answer_audio_open = False
                    await asyncio.sleep(2)


                except MatchEnded:
                    await self._notify_match_complete()
                    await asyncio.sleep(0.05)
                    continue

        except asyncio.CancelledError:
            pass
        finally:
            print("Shutting down game loop...")

def load_question_bank(file_path: str) -> list:
    candidate = Path(file_path)
    if not candidate.is_absolute() and candidate.parts[:1] != ("questions",):
        candidate = PROJECT_ROOT / "questions" / candidate
    elif not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    candidate = candidate.resolve()
    if not candidate.is_relative_to((PROJECT_ROOT / "questions").resolve()):
        raise ValueError("Question bank path must be inside the questions directory")
    file_path = str(candidate)
    if not os.path.exists(file_path):
        print(f"\n❌ [FATAL ERROR]: FILE NOT FOUND: {file_path}")
        sys.exit(1)
        
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            bank = validate_question_bank(json.load(f))
            print(f"\n[DEBUG]: Successfully loaded {len(bank)} questions from '{file_path}'")
            return bank
    except json.JSONDecodeError as e:
        print(f"\n❌ [FATAL ERROR]: JSON FILE BROKEN: {e}")
        sys.exit(1)

async def main():
    parser = argparse.ArgumentParser(description="Agentic Moderator Backend")
    parser.add_argument("--set", type=str, default="m_round01_bank.json")
    parser.add_argument("--no-audio", action="store_true", help="Disable TTS and Mic")
    parser.add_argument("--use-camera", action="store_true", help="Enable OpenCV Vision")
    args = parser.parse_args()

    questions = load_question_bank(args.set)
    
    moderator = ScienceBowlModerator(
        use_audio=not args.no_audio, 
        use_camera=args.use_camera,
        initial_bank=questions 
    )

    tasks = [
        asyncio.create_task(moderator.ui.start_server()),
        asyncio.create_task(moderator.ui.update_ui_from_queue()),
        asyncio.create_task(moderator.run_game_loop()) 
    ]
    
    print("\n" + "="*50)
    print("🚀 [SYSTEM LIVE]: Backend and UI WebSocket are running!")
    print("👉 Open http://localhost:8080 in your web browser.")
    print("="*50 + "\n")
    
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    # Dynamically grab the port for Render deployment
    port = int(os.environ.get("PORT", 8080))
    
    print("==================================================")
    print(f"🚀 [SYSTEM LIVE]: Multi-Room Server booting on port {port}")
    print("==================================================")
    
    # Run the FastAPI app directly from the UI service
    uvicorn.run("services.ui_service:app", host="0.0.0.0", port=port)
