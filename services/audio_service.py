import asyncio
import speech_recognition as sr
from core.events import Event

class AudioService:
    def __init__(self, event_queue: asyncio.Queue):
        self.event_queue = event_queue
        self.recognizer = sr.Recognizer()
        self.current_buzzpoint = ""

    def capture_mic_input(self, timeout_sec: float) -> str:
        """Captures mic audio and updates the UI status."""
        self.event_queue.put_nowait(Event(type="UPDATE_STATUS", payload={"text": "🎤 Listening for answer..."}))
        
        with sr.Microphone() as source:
            self.recognizer.adjust_for_ambient_noise(source, duration=0.2)
            try:
                audio_data = self.recognizer.listen(source, timeout=timeout_sec, phrase_time_limit=5.0)
                
                self.event_queue.put_nowait(Event(type="UPDATE_STATUS", payload={"text": "⏳ Processing Speech..."}))
                transcript = self.recognizer.recognize_google(audio_data)
                
                self.event_queue.put_nowait(Event(type="UPDATE_STATUS", payload={"text": f"🗣️ Heard: '{transcript}'"}))
                return transcript
                
            except sr.WaitTimeoutError:
                self.event_queue.put_nowait(Event(type="UPDATE_STATUS", payload={"text": "⏰ Time Expired (No Audio)"}))
                return ""
            except sr.UnknownValueError:
                self.event_queue.put_nowait(Event(type="UPDATE_STATUS", payload={"text": "❌ Audio Unintelligible"}))
                return ""