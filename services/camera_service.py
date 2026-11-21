import asyncio
import cv2
import base64
import numpy as np
from core.events import Event


class VisionService:
    def __init__(self, inbound_queue: asyncio.Queue, outbound_queue: asyncio.Queue, send_debug_frames=True):
        self.inbound_queue = inbound_queue
        self.outbound_queue = outbound_queue
        self.buzz_lockout_until = 0.0

        # Strictly Ephemeral State (Memory Only)
        self.rois = {}
        self.baselines = {}
        self.armed = False
        self.send_debug_frames = bool(send_debug_frames)

        self.luma_threshold = 150
        self.tie_margin = 80
        self.calibrate_flag = False
        self.warmup_frames = 30

        self.current_frame = None
        print("[VISION SERVICE]: Booting up Annotated Luma Engine. Awaiting Manual Calibration...")

    def set_armed(self, armed: bool):
        """Only detect buzzes while the moderator is in a live read or rebound window."""
        armed = bool(armed)
        if armed and not self.armed:
            # Start each live window from the current room lighting.
            self.baselines.clear()
        self.armed = armed

    def update_rois(self, rois: dict):
        self.rois = rois
        self.baselines.clear()
        print(f"[VISION SERVICE]: Live updated {len(self.rois)} Manual ROIs in memory.")

    def clear_rois(self):
        """Wipes live regions from memory for the current session."""
        self.rois = {}
        self.baselines.clear()
        print("[VISION SERVICE]: Cleared all ROIs. System requires re-calibration.")

    def request_baseline_calibration(self):
        self.calibrate_flag = True

    async def run_vision_loop(self):
        while True:
            success, frame = self.cap.read()
            if success:
                self.current_frame = frame
                current_time = asyncio.get_running_loop().time()

                if self.warmup_frames > 0:
                    self.warmup_frames -= 1
                    await asyncio.sleep(0.033)
                    continue

                if self.calibrate_flag:
                    self._calibrate_lighting(frame)
                    self.calibrate_flag = False

                await self._detect_manual(frame, current_time)

            await asyncio.sleep(0.033)

    async def process_frame(self, frame, current_time):
        if self.calibrate_flag:
            self._calibrate_lighting(frame)
            self.calibrate_flag = False

        if not self.armed:
            return

        await self._detect_manual(frame, current_time)

    async def _detect_manual(self, frame, current_time):
        if not self.armed:
            return

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        valid_spikes = []

        display_frame = frame.copy() if self.send_debug_frames else None

        if self.rois:
            for seat, coords in self.rois.items():
                x1, y1 = int(coords[0] * w), int(coords[1] * h)
                x2, y2 = int(coords[2] * w), int(coords[3] * h)

                if display_frame is not None:
                    cv2.rectangle(display_frame, (x1, y1), (x2, y2), (255, 0, 0), 2)
                    cv2.putText(display_frame, seat, (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 0), 2)

                roi_crop = gray[y1:y2, x1:x2]
                if roi_crop.size == 0:
                    continue

                mean_luma = cv2.mean(roi_crop)[0]
                baseline = self.baselines.get(seat, mean_luma)
                self.baselines[seat] = baseline

                current_spike = mean_luma - baseline

                if display_frame is not None:
                    color = (0, 255, 0) if current_spike < self.luma_threshold else (0, 0, 255)
                    cv2.putText(display_frame, f"L:{int(mean_luma)} / B:{int(baseline)}", (x1, y2 + 25), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

                if current_spike > self.luma_threshold:
                    valid_spikes.append((seat, current_spike))

        if display_frame is not None:
            _, buffer = cv2.imencode('.jpg', display_frame, [cv2.IMWRITE_JPEG_QUALITY, 50])
            b64_str = base64.b64encode(buffer).decode('utf-8')
            await self.outbound_queue.put({"type": "DEBUG_FRAME", "payload": {"frame_data": "data:image/jpeg;base64," + b64_str}})

        if current_time > self.buzz_lockout_until and len(valid_spikes) > 0:
            if len(valid_spikes) == 1:
                winning_seat = valid_spikes[0][0]
                team_name = "Team A" if "A" in winning_seat else "Team B"
                await self._publish_buzz(team_name, winning_seat, current_time)

            elif len(valid_spikes) > 1:
                valid_spikes.sort(key=lambda x: x[1], reverse=True)
                top_seat, top_spike = valid_spikes[0]
                runner_up_seat, runner_up_spike = valid_spikes[1]

                if (top_spike - runner_up_spike) >= self.tie_margin:
                    team_name = "Team A" if "A" in top_seat else "Team B"
                    await self._publish_buzz(team_name, top_seat, current_time)

    def _calibrate_lighting(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        self.baselines.clear()

        for seat, coords in self.rois.items():
            x1, y1 = int(coords[0] * w), int(coords[1] * h)
            x2, y2 = int(coords[2] * w), int(coords[3] * h)
            roi_crop = gray[y1:y2, x1:x2]
            if roi_crop.size > 0:
                self.baselines[seat] = cv2.mean(roi_crop)[0]

        self.outbound_queue.put_nowait({"type": "UPDATE_STATUS", "payload": {"text": "✅ Ambient Lighting Re-zeroed!"}})

    async def _publish_buzz(self, team_name: str, player_name: str, trigger_time: float):
        self.buzz_lockout_until = trigger_time + 3.0
        # One detected buzz ends this read's detection window. The phase machine
        # re-arms on the next live read or rebound.
        self.armed = False
        print(f"\n[VISION SERVICE]: {team_name} ({player_name}) BUZZED IN!")
        await self.inbound_queue.put(Event(
            type="BUZZ",
            payload={"team": team_name, "player": player_name, "source": "camera"},
        ))
