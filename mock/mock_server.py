#!/usr/bin/env python3
"""
Mock backend for the Smart Service Request System.

Implements API contract v1 (see docs/api-contract.md) with hardcoded logic so the
Pi firmware and the dashboard can be developed before the real Azure backend exists.

Zero dependencies -- standard library only, Python 3.8+. No Azure, no pip install.

    python3 mock/mock_server.py            # listens on 0.0.0.0:7071
    python3 mock/mock_server.py --port 8080

State is in memory and resets when you restart it.

The routing here is keyword-based and deliberately dumb. It is NOT the real agent;
it exists so that every branch the firmware must handle (clarification, status query,
safety, out of scope, low confidence) can be triggered on demand. Say one of the
phrases in TEST PHRASES below to hit each branch.
"""

import argparse
import base64
import io
import json
import math
import re
import struct
import time
import uuid
import wave
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# --------------------------------------------------------------------------
# Test phrases -- say these to the panel to exercise each branch
# --------------------------------------------------------------------------
# "the printer on floor two is jammed"  -> normal, single turn, request created
# "it's dirty in here"                  -> clarification: asks for location, listen_again
#   (answer "third floor washroom")     -> request created
#   (answer "um" twice)                 -> two-turn cap, escalated_to_human
# "what happened to my request"         -> status_query, no new record
# "someone fell down the stairs"        -> safety (keyword), red LED, supervisor
# "he isn't moving and i'm scared"      -> safety (semantic), same outcome
# "unlock the server room door"         -> out_of_scope, refused
# (any transcript with stt_confidence < 0.55) -> asks user to repeat

SESSION_TTL_SECONDS = 60
STT_CONFIDENCE_THRESHOLD = 0.55
MAX_CLARIFICATION_TURNS = 2

SAFETY_KEYWORDS = [
    "fell", "fall", "fallen", "blood", "bleeding", "fire", "smoke", "gas leak",
    "not breathing", "unconscious", "collapsed", "electric shock", "hurt", "injured",
]
SEMANTIC_SAFETY_HINTS = [
    "isn't moving", "isnt moving", "not moving", "i'm scared", "im scared",
    "help me", "emergency",
]
OUT_OF_SCOPE_HINTS = ["unlock", "open the door", "disable", "turn off the alarm", "override"]
STATUS_QUERY_HINTS = [
    "what happened", "status of", "my request", "did anyone", "is it done",
    "any update", "how long",
]

CATEGORY_KEYWORDS = {
    "cleaning": ["dirty", "spill", "trash", "garbage", "mess", "clean", "toilet", "washroom"],
    "it_support": ["printer", "wifi", "wi-fi", "computer", "laptop", "projector", "network", "screen"],
    "maintenance": ["broken", "leak", "light", "door", "heating", "ac", "air conditioning", "window"],
    "supplies": ["out of", "empty", "refill", "paper", "soap", "towel"],
}
TEAM_FOR_CATEGORY = {
    "cleaning": "custodial",
    "it_support": "it",
    "maintenance": "facilities",
    "supplies": "custodial",
    "other": "facilities",
}

LOCATION_PATTERNS = [
    (r"\b(?:third|3rd|floor three|floor 3)\b.*\bwashroom\b", "3F-Washroom"),
    (r"\b(?:second|2nd|floor two|floor 2)\b", "2F-Office"),
    (r"\b(?:third|3rd|floor three|floor 3)\b", "3F-Corridor"),
    (r"\b(?:first|1st|floor one|floor 1|ground)\b", "1F-Lobby"),
    (r"\blobby\b", "1F-Lobby"),
    (r"\bwashroom\b|\brestroom\b|\bbathroom\b", "3F-Washroom"),
    (r"\bcafeteria\b|\bcanteen\b", "1F-Cafeteria"),
]

# --------------------------------------------------------------------------
# In-memory state
# --------------------------------------------------------------------------
REQUESTS = {}   # request_id -> dict
SESSIONS = {}   # session_id -> dict
SEEN_TURNS = set()  # (session_id, turn, timestamp) for idempotency


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_request_id(location):
    tag = "".join(c for c in location.upper() if c.isalnum())[:3] or "GEN"
    return "REQ-%s-%04d" % (tag, len(REQUESTS) + 817)


# --------------------------------------------------------------------------
# Fake audio -- a short beep as base64 WAV.
# Real backend returns 16 kHz mono MP3. The firmware should key off
# `audio_format`, not assume mp3. Run with --no-audio to always return "" and
# exercise the local Piper fallback path instead.
# --------------------------------------------------------------------------
EMIT_AUDIO = True


def beep_wav_b64(duration_s=0.35, freq=880, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        total = int(rate * duration_s)
        for i in range(total):
            # fade in/out so it doesn't click
            env = min(1.0, min(i, total - i) / (rate * 0.02))
            v = int(12000 * env * math.sin(2 * math.pi * freq * i / rate))
            frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))
    return base64.b64encode(buf.getvalue()).decode("ascii")


_BEEP_CACHE = {}


def audio_for(reply_text):
    if not EMIT_AUDIO:
        return "", "mp3"
    key = len(reply_text) % 3
    if key not in _BEEP_CACHE:
        _BEEP_CACHE[key] = beep_wav_b64(freq=[660, 880, 990][key])
    return _BEEP_CACHE[key], "wav"


# --------------------------------------------------------------------------
# Device action helpers
# --------------------------------------------------------------------------
def actions_listening_again():
    return [
        {"actuator": "led_amber", "action": "pulse", "duration_ms": 2000},
        {"actuator": "speaker", "action": "play_reply"},
    ]


def actions_complete(request_id):
    return [
        {"actuator": "led_green", "action": "pulse", "duration_ms": 3000},
        {"actuator": "lcd", "action": "show", "text": request_id},
        {"actuator": "speaker", "action": "play_reply"},
    ]


def actions_safety():
    return [
        {"actuator": "led_red", "action": "blink_fast", "duration_ms": 5000},
        {"actuator": "buzzer", "action": "pattern_urgent", "duration_ms": 1500},
        {"actuator": "lcd", "action": "show", "text": "SUPERVISOR ALERTED"},
        {"actuator": "speaker", "action": "play_reply"},
    ]


def actions_refused():
    return [
        {"actuator": "led_amber", "action": "pulse", "duration_ms": 2000},
        {"actuator": "lcd", "action": "show", "text": "NOT SUPPORTED"},
        {"actuator": "speaker", "action": "play_reply"},
    ]


# --------------------------------------------------------------------------
# Dumb classification
# --------------------------------------------------------------------------
def detect_location(text):
    low = text.lower()
    for pattern, loc in LOCATION_PATTERNS:
        if re.search(pattern, low):
            return loc
    return None


def detect_category(text):
    low = text.lower()
    for cat, words in CATEGORY_KEYWORDS.items():
        if any(w in low for w in words):
            return cat
    return "other"


def keyword_safety(text):
    low = text.lower()
    return any(re.search(r"\b" + re.escape(k), low) for k in SAFETY_KEYWORDS)


def semantic_safety(text):
    low = text.lower()
    return any(h in low for h in SEMANTIC_SAFETY_HINTS)


def detect_intent(text):
    low = text.lower()
    if any(h in low for h in STATUS_QUERY_HINTS):
        return "status_query"
    if any(h in low for h in OUT_OF_SCOPE_HINTS):
        return "out_of_scope"
    return "new_request"


def priority_for(category, safety_flag):
    if safety_flag:
        return "critical"
    return {"it_support": "medium", "maintenance": "high",
            "cleaning": "medium", "supplies": "low"}.get(category, "medium")


# --------------------------------------------------------------------------
# Session handling
# --------------------------------------------------------------------------
def get_session(session_id, device_id):
    s = SESSIONS.get(session_id)
    if s and (now() - s["last_seen"]).total_seconds() > SESSION_TTL_SECONDS:
        del SESSIONS[session_id]
        s = None
    if not s:
        s = {"session_id": session_id, "device_id": device_id, "turns": [],
             "pending": None, "clarify_count": 0, "last_seen": now()}
        SESSIONS[session_id] = s
    s["last_seen"] = now()
    return s


def create_request(device_id, location, transcript, category, safety_flag, flag_source):
    rid = make_request_id(location)
    team = "supervisor" if safety_flag else TEAM_FOR_CATEGORY.get(category, "facilities")
    rec = {
        "request_id": rid,
        "created_at": iso(now()),
        "location": location,
        "device_id": device_id,
        "transcript": transcript,
        "category": category,
        "priority": priority_for(category, safety_flag),
        "assigned_team": team,
        "confidence": 0.91 if category != "other" else 0.62,
        "safety_flag": safety_flag,
        "flag_source": flag_source,
        "status": "escalated" if safety_flag else "open",
        "acknowledged_at": None,
        "acknowledged_by": None,
        "completed_at": None,
        "resolution_time_s": None,
    }
    REQUESTS[rid] = rec
    return rec


def request_view_for_device(rec):
    return {
        "request_id": rec["request_id"],
        "category": rec["category"],
        "location": rec["location"],
        "priority": rec["priority"],
        "assigned_team": rec["assigned_team"],
        "confidence": rec["confidence"],
        "safety_flag": rec["safety_flag"],
        "missing_fields": [],
    }


def envelope(session_id, turn, intent, state, listen_again, request, actions, reply):
    b64, fmt = audio_for(reply)
    return {
        "session_id": session_id,
        "turn": turn,
        "intent": intent,
        "state": state,
        "listen_again": listen_again,
        "request": request,
        "device_actions": actions,
        "speech_reply": reply,
        "audio_format": fmt,
        "audio_b64": b64,
    }


# --------------------------------------------------------------------------
# The one endpoint the Pi calls
# --------------------------------------------------------------------------
def handle_voice_turn(body):
    for field in ("session_id", "device_id", "location", "transcript", "timestamp"):
        if not body.get(field):
            return 400, {"error": "invalid_payload",
                         "message": "%s is required and must be non-empty" % field,
                         "session_id": body.get("session_id")}

    session_id = body["session_id"]
    turn = int(body.get("turn", 1))
    device_id = body["device_id"]
    panel_location = body["location"]
    transcript = body["transcript"].strip()
    confidence = float(body.get("stt_confidence", 1.0))
    timestamp = body["timestamp"]

    # Idempotency: a queued-and-retried transcript must not create a second ticket.
    key = (session_id, turn, timestamp)
    if key in SEEN_TURNS:
        print("  [dedup] replay of %s turn %s -- returning cached-style ack" % (session_id, turn))
        return 200, envelope(session_id, turn, "new_request", "complete", False, None,
                             [{"actuator": "led_green", "action": "pulse", "duration_ms": 1500}],
                             "That request was already received.")
    SEEN_TURNS.add(key)

    session = get_session(session_id, device_id)
    session["turns"].append({"turn": turn, "transcript": transcript})

    # --- low confidence: ask to repeat before guessing
    if confidence < STT_CONFIDENCE_THRESHOLD:
        reply = "Sorry, I didn't catch that. Could you say it again?"
        return 200, envelope(session_id, turn, "new_request", "awaiting_user", True,
                             None, actions_listening_again(), reply)

    # --- layer 1: keyword safety filter, runs before any "agent"
    safety_flag = keyword_safety(transcript)
    flag_source = "keyword" if safety_flag else None
    # --- layer 2: "agent" semantic flag
    if not safety_flag and semantic_safety(transcript):
        safety_flag = True
        flag_source = "agent"

    # --- layer 3: workflow enforces the consequence, whatever was proposed
    if safety_flag:
        location = detect_location(transcript) or panel_location
        rec = create_request(device_id, location, transcript, "safety", True, flag_source)
        reply = ("I've flagged this as urgent and a supervisor is being notified now. "
                 "Please stay with the person if it is safe to do so.")
        SESSIONS.pop(session_id, None)
        return 200, envelope(session_id, turn, "new_request", "escalated_to_human", False,
                             request_view_for_device(rec), actions_safety(), reply)

    intent = detect_intent(transcript)

    # --- status query: lookup tool, no new record
    if intent == "status_query":
        mine = [r for r in REQUESTS.values() if r["device_id"] == device_id]
        mine.sort(key=lambda r: r["created_at"], reverse=True)
        if not mine:
            reply = "I don't have any recent requests from this panel."
        else:
            r = mine[0]
            reply = ("Your %s request %s is currently %s, assigned to the %s team."
                     % (r["category"].replace("_", " "), r["request_id"], r["status"], r["assigned_team"]))
        return 200, envelope(session_id, turn, "status_query", "complete", False, None,
                             [{"actuator": "led_green", "action": "pulse", "duration_ms": 2000},
                              {"actuator": "speaker", "action": "play_reply"}], reply)

    # --- out of scope: refuse, no whitelisted action exists
    if intent == "out_of_scope":
        reply = ("I can't do that. This panel can only create service requests for "
                 "cleaning, maintenance, IT, and supplies.")
        return 200, envelope(session_id, turn, "out_of_scope", "rejected", False, None,
                             actions_refused(), reply)

    # --- clarification: is a location present, either now or from an earlier turn?
    pending = session.get("pending")
    location = detect_location(transcript)
    if pending:
        intent = "clarification_answer"
        transcript = pending["transcript"] + " -- " + transcript
        category = pending["category"]
        if not location:
            session["clarify_count"] += 1
            if session["clarify_count"] >= MAX_CLARIFICATION_TURNS:
                # two-turn cap: route to a supervisor rather than looping
                rec = create_request(device_id, panel_location, transcript, category, False, None)
                rec["assigned_team"] = "supervisor"
                rec["status"] = "escalated"
                reply = ("I still don't have the location, so I've passed this to a "
                         "supervisor to follow up.")
                SESSIONS.pop(session_id, None)
                return 200, envelope(session_id, turn, intent, "escalated_to_human", False,
                                     request_view_for_device(rec), actions_complete(rec["request_id"]),
                                     reply)
            reply = "Sorry, which floor or room is that?"
            return 200, envelope(session_id, turn, intent, "awaiting_user", True, None,
                                 actions_listening_again(), reply)
    else:
        category = detect_category(transcript)
        if not location:
            session["pending"] = {"transcript": transcript, "category": category}
            session["clarify_count"] += 1
            reply = "Sure. Which room or floor is that in?"
            return 200, envelope(session_id, turn, "new_request", "awaiting_user", True, None,
                                 actions_listening_again(), reply)

    rec = create_request(device_id, location, transcript, category, False, None)
    reply = ("Got it. A %s request for %s has been sent to the %s team."
             % (rec["category"].replace("_", " "), rec["location"].replace("-", " "), rec["assigned_team"]))
    SESSIONS.pop(session_id, None)
    return 200, envelope(session_id, turn, intent, "complete", False,
                         request_view_for_device(rec), actions_complete(rec["request_id"]), reply)


# --------------------------------------------------------------------------
# Dashboard endpoints
# --------------------------------------------------------------------------
def handle_list_requests(query):
    items = list(REQUESTS.values())
    status = query.get("status", [None])[0]
    location = query.get("location", [None])[0]
    if status:
        items = [r for r in items if r["status"] == status]
    if location:
        items = [r for r in items if r["location"] == location]
    items.sort(key=lambda r: r["created_at"], reverse=True)
    return 200, {"items": items, "continuation_token": None}


def handle_ack(rid, actor):
    rec = REQUESTS.get(rid)
    if not rec:
        return 404, {"error": "not_found", "message": rid}
    rec["status"] = "acknowledged"
    rec["acknowledged_at"] = iso(now())
    rec["acknowledged_by"] = actor
    return 200, {"status": rec["status"], "acknowledged_by": actor,
                 "acknowledged_at": rec["acknowledged_at"]}


def handle_complete(rid):
    rec = REQUESTS.get(rid)
    if not rec:
        return 404, {"error": "not_found", "message": rid}
    rec["status"] = "completed"
    rec["completed_at"] = iso(now())
    created = datetime.strptime(rec["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    rec["resolution_time_s"] = int((now() - created).total_seconds())
    return 200, {"status": "completed", "resolution_time_s": rec["resolution_time_s"]}


def handle_simulate(body):
    """Demo backup path: create a request from typed text, no Pi involved."""
    text = (body.get("transcript") or "").strip()
    if not text:
        return 400, {"error": "invalid_payload", "message": "transcript is required"}
    device_id = body.get("device_id", "dashboard-sim")
    location = detect_location(text) or body.get("location") or "1F-Lobby"
    flagged = keyword_safety(text) or semantic_safety(text)
    rec = create_request(device_id, location, text,
                         "safety" if flagged else detect_category(text),
                         flagged, "keyword" if flagged else None)
    return 201, rec


def handle_summary():
    items = list(REQUESTS.values())
    def count_by(field):
        out = {}
        for r in items:
            out[r[field]] = out.get(r[field], 0) + 1
        return out
    done = [r for r in items if r["resolution_time_s"] is not None]
    return 200, {
        "total": len(items),
        "by_category": count_by("category"),
        "by_team": count_by("assigned_team"),
        "by_priority": count_by("priority"),
        "by_status": count_by("status"),
        "mean_resolution_time_s": (sum(r["resolution_time_s"] for r in done) // len(done)) if done else None,
    }


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("  %s" % (fmt % args))

    def _send(self, code, payload):
        data = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Device-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return None

    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        u = urlparse(self.path)
        path, query = u.path.rstrip("/"), parse_qs(u.query)
        if path in ("/api/health", "/health"):
            return self._send(200, {"status": "ok", "agent": "mock", "speech": "mock",
                                    "storage": "in-memory", "version": "mock-1.0"})
        if path == "/api/requests":
            return self._send(*handle_list_requests(query))
        if path == "/api/reports/summary":
            return self._send(*handle_summary())
        m = re.match(r"^/api/requests/([A-Za-z0-9\-]+)$", path)
        if m:
            rec = REQUESTS.get(m.group(1))
            return self._send(200, rec) if rec else self._send(404, {"error": "not_found"})
        self._send(404, {"error": "not_found", "message": path})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        body = self._body()
        if body is None:
            return self._send(400, {"error": "invalid_payload", "message": "body is not valid JSON"})

        if path == "/api/voice/turn":
            print('  >> "%s"  (conf %.2f, session %s turn %s)'
                  % (body.get("transcript", ""), float(body.get("stt_confidence", 1.0)),
                     body.get("session_id"), body.get("turn")))
            code, payload = handle_voice_turn(body)
            if code == 200:
                print("  << state=%s listen_again=%s  \"%s\""
                      % (payload["state"], payload["listen_again"], payload["speech_reply"]))
            return self._send(code, payload)

        if path == "/api/requests/simulate":
            return self._send(*handle_simulate(body))

        m = re.match(r"^/api/requests/([A-Za-z0-9\-]+)/ack$", path)
        if m:
            return self._send(*handle_ack(m.group(1), body.get("actor", "demo-staff")))
        m = re.match(r"^/api/requests/([A-Za-z0-9\-]+)/complete$", path)
        if m:
            return self._send(*handle_complete(m.group(1)))

        self._send(404, {"error": "not_found", "message": path})


def main():
    global EMIT_AUDIO
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7071)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--no-audio", action="store_true",
                    help="always return audio_b64='' so the Pi uses its local Piper voice")
    args = ap.parse_args()
    EMIT_AUDIO = not args.no_audio

    print("Smart Service mock backend -- API contract v1")
    print("  listening on http://%s:%d/api" % (args.host, args.port))
    print("  audio: %s" % ("beep WAV in audio_b64" if EMIT_AUDIO else "empty (Piper fallback path)"))
    print("  try:   curl -s localhost:%d/api/health" % args.port)
    print()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
