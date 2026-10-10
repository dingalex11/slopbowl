import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "slopbowl-fe316")
APP_ENV = os.getenv("APP_ENV", "development").lower()
MAX_ROOMS = int(os.getenv("MAX_ROOMS", "100"))
ROOM_IDLE_TTL_SECONDS = int(os.getenv("ROOM_IDLE_TTL_SECONDS", "300"))
INBOUND_QUEUE_SIZE = int(os.getenv("INBOUND_QUEUE_SIZE", "256"))
OUTBOUND_QUEUE_SIZE = int(os.getenv("OUTBOUND_QUEUE_SIZE", "512"))
MAX_WEBSOCKET_MESSAGE_BYTES = 1024 * 1024
MAX_FRAME_BYTES = 256 * 1024
MAX_AUDIO_BYTES = 700 * 1024
MAX_QUESTIONS_PER_BANK = 500
