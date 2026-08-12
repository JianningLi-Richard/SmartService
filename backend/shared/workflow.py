"""The eleven-step workflow from section 7 of the proposal.

  1. Receive and validate the transcript payload
  2. Run the safety keyword filter
  3. Load prior turns for this session
  4. Invoke the Routing Agent
  5. Validate every device action against the whitelist
  6. Branch on state
  7. Synthesise the spoken reply
  8. Return the single response
  9. (timer) escalate unacknowledged requests      -> escalation_sweep()
 10. (dashboard) acknowledge / complete            -> function_app.py
 11. Log the outcome                               -> Application Insights

Steps 2 and 6 are where safety is *enforced*. The agent's proposed team is
overridden in code; nothing here trusts the model's routing decision.
"""

import logging
import time
import uuid

from . import agent, config, safety, speech, whitelist
from .store import get_store, iso, now, parse_iso

log = logging.getLogger(__name__)

REQUIRED_FIELDS = ("session_id", "device_id", "location", "transcript", "timestamp")


class PayloadError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


# --------------------------------------------------------------------------
# Step 1 -- validation
# --------------------------------------------------------------------------
def validate_payload(body):
    if not isinstance(body, dict):
        raise PayloadError("body must be a JSON object")
    for f in REQUIRED_FIELDS:
        if not body.get(f) or not str(body[f]).strip():
            raise PayloadError("%s is required and must be non-empty" % f)
    try:
        turn = int(body.get("turn", 1))
    except (TypeError, ValueError):
        raise PayloadError("turn must be an integer")
    try:
        conf = float(body.get("stt_confidence", 1.0))
    except (TypeError, ValueError):
        raise PayloadError("stt_confidence must be a number")
    if not 0.0 <= conf <= 1.0:
        raise PayloadError("stt_confidence must be between 0 and 1")
    if len(body["transcript"]) > 1000:
        raise PayloadError("transcript too long")
    return {
        "session_id": str(body["session_id"]).strip(),
        "turn": turn,
        "device_id": str(body["device_id"]).strip(),
        "location": str(body["location"]).strip(),
        "transcript": str(body["transcript"]).strip(),
        "stt_confidence": conf,
        "timestamp": str(body["timestamp"]).strip(),
        "prefer_local_tts": bool(body.get("prefer_local_tts", False)),
    }


def check_device_key(device_id, provided_key):
    """Returns True if authorised. With no DEVICE_KEYS configured, auth is open
    (local development); the deployment lead must set DEVICE_KEYS in Azure."""
    if not config.DEVICE_KEYS:
        return True
    return config.DEVICE_KEYS.get(device_id) == provided_key


# --------------------------------------------------------------------------
# Response envelope
# --------------------------------------------------------------------------
def _envelope(p, intent, state, listen_again, request, actions, reply, tts=True):
    use_cloud_tts = tts and not p.get("prefer_local_tts", False)
    audio_b64, audio_format, tts_ms = (speech.synthesize(reply)
                                             if use_cloud_tts else ("", "mp3", 0))
    clean, dropped = whitelist.validate_actions(actions, log)
    return {
        "session_id": p["session_id"],
        "turn": p["turn"],
        "intent": intent,
        "state": state,
        "listen_again": listen_again,
        "request": request,
        "device_actions": clean,
        "speech_reply": reply,
        "audio_format": audio_format,
        "audio_b64": audio_b64,
    }, {"tts_ms": tts_ms, "dropped_actions": dropped}


def _device_view(rec):
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


def _make_request_id(location, seq):
    tag = "".join(c for c in (location or "GEN").upper() if c.isalnum())[:3] or "GEN"
    return "REQ-%s-%04d" % (tag, seq)


def _create_request(store, p, transcript, cls, safety_flag, flag_source):
    req = cls.get("request") or {}
    location = req.get("location") or "Unspecified"
    category = "safety" if safety_flag else req.get("category", "other")
    # The model may recommend a team, but routing authority stays in code.
    team = ("supervisor" if safety_flag else
            agent.TEAM_FOR_CATEGORY.get(category, "facilities"))
    priority = "critical" if safety_flag else req.get("priority", "medium")

    rec = {
        "request_id": _make_request_id(location, store.next_sequence()),
        "created_at": iso(now()),
        "location": location,
        "device_id": p["device_id"],
        "session_id": p["session_id"],
        "transcript": transcript,
        "stt_confidence": p["stt_confidence"],
        "intent": cls.get("intent", "new_request"),
        "category": category,
        "priority": priority,
        "assigned_team": team,
        "confidence": float(req.get("confidence", 0.5)),
        "classifier": cls.get("source", "rules"),
        "safety_flag": bool(safety_flag),
        "flag_source": flag_source,
        "status": "escalated" if safety_flag else "open",
        "acknowledged_at": None,
        "acknowledged_by": None,
        "completed_at": None,
        "resolution_time_s": None,
    }
    store.put_request(rec)
    return rec


# --------------------------------------------------------------------------
# Steps 2-8 -- one conversational turn
# --------------------------------------------------------------------------
def handle_turn(body, device_key=None):
    """Returns (http_status, response_body, telemetry)."""
    started = time.time()
    p = validate_payload(body)

    if not check_device_key(p["device_id"], device_key):
        return 401, {"error": "bad_device_key", "message": "unknown device or key",
                     "session_id": p["session_id"]}, {}

    store = get_store()
    telemetry = {"device_id": p["device_id"], "session_id": p["session_id"],
                 "turn": p["turn"]}

    # Idempotency: a queued transcript retried after a network drop must not
    # create a second ticket. Keyed on the ORIGINAL utterance timestamp.
    if store.seen_turn(p["session_id"], p["turn"], p["timestamp"]):
        log.info("replay of %s turn %s -- deduplicated", p["session_id"], p["turn"])
        telemetry["deduplicated"] = True
        resp, extra = _envelope(p, "new_request", "complete", False, None,
                                [{"actuator": "led_green", "action": "pulse",
                                  "duration_ms": 1500}],
                                "That request was already received.")
        return 200, resp, {**telemetry, **extra}

    # Low STT confidence: ask the user to repeat rather than guessing.
    if p["stt_confidence"] < config.STT_CONFIDENCE_THRESHOLD:
        telemetry["low_confidence"] = True
        resp, extra = _envelope(p, "new_request", "awaiting_user", True, None,
                                whitelist.listening_again(),
                                "Sorry, I didn't catch that. Could you say it again?")
        return 200, resp, {**telemetry, **extra}

    # Step 3 -- prior turns
    session = store.get_session(p["device_id"], p["session_id"]) or {
        "session_id": p["session_id"], "device_id": p["device_id"],
        "turns": [], "clarify_count": 0, "pending_transcript": "",
    }
    prior_turns = session.get("turns", [])

    # Full transcript for this conversation: earlier turns plus this one.
    combined = p["transcript"]
    if session.get("pending_transcript"):
        combined = session["pending_transcript"] + " -- " + p["transcript"]

    # Step 2 -- keyword filter, BEFORE the agent
    kw_flag = safety.keyword_flag(combined)

    # Step 4 -- agent (or rule-based fallback)
    agent_started = time.time()
    cls = agent.classify(combined, p["location"], p["device_id"], prior_turns,
                         p["stt_confidence"], kw_flag)
    telemetry["agent_ms"] = int((time.time() - agent_started) * 1000)
    telemetry["classifier"] = cls.get("source", "rules")

    # Step 2 continued -- layer 2 (agent's own flag) folded in
    agent_flag = bool((cls.get("request") or {}).get("safety_flag"))
    safety_flag, flag_source = safety.resolve(combined, agent_flag)
    telemetry["safety_flag"] = safety_flag
    telemetry["flag_source"] = flag_source

    # Step 6a -- SAFETY OVERRIDE. Enforced here in code, not by the agent.
    if safety_flag:
        rec = _create_request(store, p, combined, cls, True, flag_source)
        store.delete_session(p["device_id"], p["session_id"])
        reply = ("I've flagged this as urgent and a supervisor is being notified now. "
                 "Please stay with the person if it is safe to do so.")
        telemetry["request_id"] = rec["request_id"]
        resp, extra = _envelope(p, "new_request", "escalated_to_human", False,
                                _device_view(rec), whitelist.safety(), reply)
        return 200, resp, {**telemetry, **extra, "total_ms": int((time.time()-started)*1000)}

    intent = cls.get("intent", "new_request")

    # Step 6b -- status query: tool call, no new record
    if intent == "status_query":
        results = agent.lookup_requests(device_id=p["device_id"], limit=1)
        telemetry["tool_calls"] = ["lookup_requests"]
        if not results:
            reply = "I don't have any recent requests from this panel."
        else:
            r = results[0]
            reply = ("Your %s request %s is currently %s, assigned to the %s team."
                     % (r["category"].replace("_", " "), r["request_id"],
                        r["status"], r["assigned_team"]))
        resp, extra = _envelope(p, "status_query", "complete", False, None,
                                [{"actuator": "led_green", "action": "pulse",
                                  "duration_ms": 2000},
                                 {"actuator": "speaker", "action": "play_reply"}], reply)
        return 200, resp, {**telemetry, **extra, "total_ms": int((time.time()-started)*1000)}

    # Step 6c -- out of scope: refuse, no whitelisted action exists
    if intent == "out_of_scope":
        resp, extra = _envelope(p, "out_of_scope", "rejected", False, None,
                                whitelist.refused(), cls.get("speech_reply") or
                                ("I can't do that. This panel can only create service "
                                 "requests for cleaning, maintenance, IT, and supplies."))
        return 200, resp, {**telemetry, **extra, "total_ms": int((time.time()-started)*1000)}

    # Step 6d -- clarification, capped at MAX_CLARIFICATION_TURNS
    if cls.get("state") == "awaiting_user":
        clarify_count = session.get("clarify_count", 0) + 1
        if clarify_count >= config.MAX_CLARIFICATION_TURNS:
            # Still incomplete after the cap: route to a supervisor, do not loop.
            rec = _create_request(store, p, combined, cls, False, None)
            store.update_request(rec["request_id"],
                                 {"assigned_team": "supervisor", "status": "escalated"})
            rec["assigned_team"], rec["status"] = "supervisor", "escalated"
            store.delete_session(p["device_id"], p["session_id"])
            telemetry["request_id"] = rec["request_id"]
            telemetry["clarify_capped"] = True
            missing = ", ".join((cls.get("request") or {}).get("missing_fields", []))
            reply = ("I still need %s, so I've passed this to a supervisor to follow up."
                     % (missing or "more request details"))
            resp, extra = _envelope(p, "clarification_answer", "escalated_to_human",
                                    False, _device_view(rec),
                                    whitelist.complete(rec["request_id"]), reply)
            return 200, resp, {**telemetry, **extra,
                               "total_ms": int((time.time()-started)*1000)}

        session["clarify_count"] = clarify_count
        session["pending_transcript"] = combined
        session["turns"] = prior_turns + [{"turn": p["turn"], "transcript": p["transcript"],
                                           "agent_state": "awaiting_user"}]
        store.put_session(session)
        telemetry["clarify_turn"] = clarify_count
        resp, extra = _envelope(p, intent, "awaiting_user", True, None,
                                whitelist.listening_again(),
                                cls.get("speech_reply") or "What service and location do you need?")
        return 200, resp, {**telemetry, **extra, "total_ms": int((time.time()-started)*1000)}

    # Step 6e -- complete
    rec = _create_request(store, p, combined, cls, False, None)
    store.delete_session(p["device_id"], p["session_id"])
    telemetry["request_id"] = rec["request_id"]
    reply = cls.get("speech_reply") or (
        "Got it. A %s request for %s has been sent to the %s team."
        % (rec["category"].replace("_", " "), rec["location"].replace("-", " "),
           rec["assigned_team"]))
    resp, extra = _envelope(p, cls.get("intent", "new_request"), "complete", False,
                            _device_view(rec), whitelist.complete(rec["request_id"]), reply)
    return 200, resp, {**telemetry, **extra, "total_ms": int((time.time()-started)*1000)}


# --------------------------------------------------------------------------
# Step 9 -- escalation timer
# --------------------------------------------------------------------------
def escalation_sweep():
    """Escalate unacknowledged high-priority requests past the configured window."""
    store = get_store()
    escalated = []
    cutoff_s = config.ESCALATION_MINUTES * 60
    for rec in store.list_requests(status="open", limit=500):
        if rec.get("priority") not in ("high", "critical"):
            continue
        age = (now() - parse_iso(rec["created_at"])).total_seconds()
        if age >= cutoff_s:
            store.update_request(rec["request_id"],
                                 {"status": "escalated", "priority": "critical"})
            escalated.append(rec["request_id"])
    if escalated:
        log.warning("escalated unacknowledged requests: %s", escalated)
    return escalated


# --------------------------------------------------------------------------
# Step 10 -- dashboard actions
# --------------------------------------------------------------------------
def acknowledge(request_id, actor):
    store = get_store()
    rec = store.get_request(request_id)
    if not rec:
        return None
    return store.update_request(request_id, {
        "status": "acknowledged", "acknowledged_at": iso(now()), "acknowledged_by": actor})


def complete_request(request_id, actor):
    store = get_store()
    rec = store.get_request(request_id)
    if not rec:
        return None
    resolution = int((now() - parse_iso(rec["created_at"])).total_seconds())
    return store.update_request(request_id, {
        "status": "completed", "completed_at": iso(now()),
        "completed_by": actor, "resolution_time_s": resolution})


def simulate(body):
    """Demo backup path: create a request from typed text, no panel involved."""
    text = (body.get("transcript") or "").strip()
    if not text:
        raise PayloadError("transcript is required")
    p = {"session_id": "sim-" + uuid.uuid4().hex[:6], "turn": 1,
         "device_id": body.get("device_id", "dashboard-sim"),
         "location": body.get("location", "1F-Lobby"),
         "transcript": text, "stt_confidence": 1.0, "timestamp": iso(now())}
    kw = safety.keyword_flag(text)
    cls = agent.classify(text, p["location"], p["device_id"], [], 1.0, kw)
    flag, source = safety.resolve(text, bool((cls.get("request") or {}).get("safety_flag")))
    return _create_request(get_store(), p, text, cls, flag, source)
