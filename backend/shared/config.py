"""Configuration. Everything comes from environment variables -- never from code.

Locally these live in backend/local.settings.json (gitignored).
In Azure they live in App Settings / Key Vault, provisioned by the deployment lead.
See docs/api-contract.md section 3 for the full list.
"""

import os


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# Storage
STORAGE_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
TABLE_REQUESTS = os.environ.get("TABLE_REQUESTS", "requests")
TABLE_SESSIONS = os.environ.get("TABLE_SESSIONS", "sessions")

# Agent
AI_FOUNDRY_ENDPOINT = os.environ.get("AI_FOUNDRY_ENDPOINT", "")
AI_FOUNDRY_AGENT_ID = os.environ.get("AI_FOUNDRY_AGENT_ID", "")
AGENT_TIMEOUT_MS = _int("AGENT_TIMEOUT_MS", 6000)

# Speech
SPEECH_KEY = os.environ.get("SPEECH_KEY", "")
SPEECH_REGION = os.environ.get("SPEECH_REGION", "")
SPEECH_VOICE = os.environ.get("SPEECH_VOICE", "en-CA-ClaraNeural")
SPEECH_TIMEOUT_MS = _int("SPEECH_TIMEOUT_MS", 2500)

# Behaviour thresholds
SESSION_TTL_SECONDS = _int("SESSION_TTL_SECONDS", 60)
STT_CONFIDENCE_THRESHOLD = _float("STT_CONFIDENCE_THRESHOLD", 0.55)
MAX_CLARIFICATION_TURNS = _int("MAX_CLARIFICATION_TURNS", 2)
ESCALATION_MINUTES = _int("ESCALATION_MINUTES", 10)

# Device auth: "pi-3f-01:key1,pi-1f-02:key2"
_DEVICE_KEYS_RAW = os.environ.get("DEVICE_KEYS", "")
DEVICE_KEYS = {}
for pair in _DEVICE_KEYS_RAW.split(","):
    if ":" in pair:
        did, key = pair.split(":", 1)
        DEVICE_KEYS[did.strip()] = key.strip()

# When no Azure resources are configured, the app runs fully on local fallbacks
# (in-memory store, rule-based classifier, no TTS). This is the same degraded path
# the proposal promises for outages -- it is built first so development is not
# blocked on provisioning.
USE_TABLE_STORAGE = bool(STORAGE_CONNECTION_STRING)
USE_AGENT = bool(AI_FOUNDRY_ENDPOINT and AI_FOUNDRY_AGENT_ID)
USE_SPEECH = bool(SPEECH_KEY and SPEECH_REGION)
