"""
core/services.py — Service configuration constants for Clara.

Extracted from clara2.py to decouple service endpoints and API keys from
business logic. All external service addresses, API credentials, and tuning
parameters live here as a single source of truth.
"""

from dotenv import load_dotenv
import os

load_dotenv()

# ── Service Endpoints ────────────────────────────────────────
TTS_HOST = "10.0.0.200"
TTS_PORT = 5050
MEMORY_BASE = "http://10.0.0.12:8765"   # was 10.0.0.10:8765
GEMMA_BASE_URL = "http://10.0.0.12:8082/v1"
GEMMA_API_KEY = os.getenv("GEMMA_API_KEY", "")

# ── LLM Tuning ─────────────────────────────────────────────
CLARA_TEMPERATURE = 0.15

# ── Memory Service ─────────────────────────────────────────
USER_ID = 2
INSTANCE = "clara_2"
MAX_SEARCHES_PER_SESSION = 10

# ── Pin Phrases ────────────────────────────────────────────
# Phrases that trigger pinning of any facts extracted from this exchange
PIN_PHRASES = [
    "anything like remember this", "anything like this is important", "anything like you need to remember",
    "anything like don't forget this", "anything like make sure you remember", "anything like remember that"
]