"""Routing Agent client, plus the rule-based classifier it falls back to.

The agent returns structured output only -- see AGENT_OUTPUT_SCHEMA. It never
touches storage directly; the lookup_requests tool is implemented here in the
Functions API and its result is handed back to the agent.

The fallback classifier is not a placeholder. Section 12 of the proposal commits
to "rule-based fallback classifier keyed on transcript keywords keeps the workflow
demoable" when the agent is slow or unavailable, so it is real production code and
also what runs before the agent is provisioned.
"""

import json
import logging
import re
import time

from . import config
from .store import get_store

log = logging.getLogger(__name__)

INTENTS = ("new_request", "status_query", "clarification_answer", "out_of_scope")
CATEGORIES = ("cleaning", "it_support", "maintenance", "supplies", "safety", "other")
PRIORITIES = ("low", "medium", "high", "critical")

AGENT_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["intent", "state", "listen_again", "speech_reply"],
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "state": {"type": "string",
                  "enum": ["complete", "awaiting_user", "escalated_to_human", "rejected"]},
        "listen_again": {"type": "boolean"},
        "speech_reply": {"type": "string", "maxLength": 300},
        "request": {
            "type": ["object", "null"],
            "properties": {
                "category": {"type": "string", "enum": list(CATEGORIES)},
                "location": {"type": ["string", "null"]},
                "priority": {"type": "string", "enum": list(PRIORITIES)},
                "assigned_team": {"type": "string"},
                "confidence": {"type": "number"},
                "safety_flag": {"type": "boolean"},
                "missing_fields": {"type": "array", "items": {"type": "string"}},
            },
        },
        "device_actions": {"type": "array"},
    },
}

SYSTEM_INSTRUCTIONS = """You are the Routing Agent for a wall-mounted service request panel.

You receive a transcript of what someone said, the panel's physical location, and
any earlier turns of the same conversation. Determine the intent, extract category,
the location explicitly spoken by the user, and urgency, recommend a priority and responsible team, and write a short
spoken reply (one or two sentences, plain speech, no markdown, no lists).

Rules you must not break:
- Only emit device actions from the whitelist you were given. Never invent one.
- Never dispatch emergency services. If someone may be hurt, set safety_flag true
  and say a supervisor is being notified.
- The panel_location is device metadata only. Never use it as the request location.
- Never invent a location or request ID. A request needs both an explicit service
  category and an explicit location/room/floor from the user.
- If location is missing, set state to awaiting_user, add "location" to
  missing_fields, set listen_again true, and ask for the room, floor, or location.
- If the service category is missing, set category to "other", add "category" to
  missing_fields, set listen_again true, and ask what service is needed (cleaning,
  maintenance, IT support, or supplies). A location alone is not a service request.
- Preserve details from earlier turns. If one missing field is supplied on a later
  turn, combine it with the previously supplied field before completing the request.
- Never resolve or cancel a request without spoken confirmation from the user.
- To answer a question about an existing request, call lookup_requests. Do not
  create a new request for a question.
Return structured output only."""

TEAM_FOR_CATEGORY = {
    "cleaning": "custodial",
    "it_support": "it",
    "maintenance": "facilities",
    "supplies": "custodial",
    "safety": "supervisor",
    "other": "facilities",
}

CATEGORY_KEYWORDS = {
    "it_support": ["printer", "wifi", "wi-fi", "computer", "laptop", "projector",
                   "network", "monitor", "screen", "password", "login"],
    "maintenance": ["broken", "leak", "leaking", "light", "lights", "door", "heating",
                    "air conditioning", "ac ", "window", "elevator", "lock"],
    "cleaning": ["dirty", "spill", "spilled", "trash", "garbage", "mess", "clean",
                 "toilet", "washroom", "smell", "stain"],
    "supplies": ["out of", "empty", "refill", "paper towel", "soap", "toilet paper",
                 "no paper"],
}

STATUS_QUERY_HINTS = ["what happened", "status of", "my request", "did anyone",
                      "is it done", "any update", "how long", "still waiting"]
OUT_OF_SCOPE_HINTS = ["unlock", "open the door", "disable", "turn off the alarm",
                      "override", "let me in", "give me access"]

LOCATION_PATTERNS = [
    (r"\broom\s+(\d+[a-z]?)\b", None),
    (r"\b(?:third|3rd|floor three|floor 3)\b[^.]{0,20}\b(?:washroom|restroom|bathroom)\b", "3F-Washroom"),
    (r"\b(?:second|2nd|floor two|floor 2)\b", "2F-Office"),
    (r"\b(?:third|3rd|floor three|floor 3)\b", "3F-Corridor"),
    (r"\b(?:first|1st|floor one|floor 1|ground floor)\b", "1F-Lobby"),
    (r"\blobby\b", "1F-Lobby"),
    (r"\b(?:cafeteria|canteen|kitchen)\b", "1F-Cafeteria"),
    (r"\b(?:washroom|restroom|bathroom)\b", "3F-Washroom"),
    (r"\b(?:server room|it room)\b", "2F-ServerRoom"),
]

PRIORITY_FOR_CATEGORY = {"safety": "critical", "maintenance": "high",
                         "it_support": "medium", "cleaning": "medium", "supplies": "low"}


# --------------------------------------------------------------------------
# Tool: lookup_requests -- registered with the agent, implemented here
# --------------------------------------------------------------------------
LOOKUP_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "lookup_requests",
        "description": ("Look up recent service requests for a location or device. "
                        "Use this to answer questions about an existing request. "
                        "Never create a new request to answer a question."),
        "parameters": {
            "type": "object",
            "properties": {
                "device_id": {"type": "string"},
                "location": {"type": "string"},
                "limit": {"type": "integer", "default": 3},
            },
        },
    },
}


def lookup_requests(device_id=None, location=None, limit=3):
    items = get_store().list_requests(location=location, limit=50)
    if device_id:
        items = [r for r in items if r.get("device_id") == device_id]
    return [{"request_id": r["request_id"], "category": r["category"],
             "location": r["location"], "status": r["status"],
             "assigned_team": r["assigned_team"], "created_at": r["created_at"]}
            for r in items[:limit]]


# --------------------------------------------------------------------------
# Rule-based fallback classifier
# --------------------------------------------------------------------------
def detect_location(text):
    low = (text or "").lower()
    for pattern, loc in LOCATION_PATTERNS:
        match = re.search(pattern, low)
        if match:
            return loc or "Room-%s" % match.group(1).upper()
    return None


def detect_category(text):
    low = (text or "").lower()
    for cat, words in CATEGORY_KEYWORDS.items():
        if any(w in low for w in words):
            return cat
    return None


def detect_intent(text):
    low = (text or "").lower()
    if any(h in low for h in STATUS_QUERY_HINTS):
        return "status_query"
    if any(h in low for h in OUT_OF_SCOPE_HINTS):
        return "out_of_scope"
    return "new_request"


def classify_with_rules(transcript, panel_location, prior_turns, safety_flag):
    """Deterministic classification. Returns the same shape the agent returns.

    Sets a deliberately low confidence so the dashboard shows it was not the agent
    and workflow.py can route low-confidence items for human review.
    """
    intent = "clarification_answer" if prior_turns else detect_intent(transcript)

    if safety_flag:
        return {
            "intent": "new_request", "state": "escalated_to_human", "listen_again": False,
            "speech_reply": ("I've flagged this as urgent and a supervisor is being "
                             "notified now."),
            "request": {"category": "safety", "location": detect_location(transcript) or panel_location,
                        "priority": "critical", "assigned_team": "supervisor",
                        "confidence": 0.5, "safety_flag": True, "missing_fields": []},
            "device_actions": [], "source": "rules",
        }

    if intent == "out_of_scope":
        return {
            "intent": "out_of_scope", "state": "rejected", "listen_again": False,
            "speech_reply": ("I can't do that. This panel can only create service "
                             "requests for cleaning, maintenance, IT, and supplies."),
            "request": None, "device_actions": [], "source": "rules",
        }

    if intent == "status_query":
        return {"intent": "status_query", "state": "complete", "listen_again": False,
                "speech_reply": "", "request": None, "device_actions": [],
                "needs_lookup": True, "source": "rules"}

    category = detect_category(transcript)
    location = detect_location(transcript)
    missing = []
    if not category:
        missing.append("category")
    if not location:
        missing.append("location")
    if missing:
        if missing == ["location"]:
            reply = "What room, floor, or location needs that service?"
        elif missing == ["category"]:
            reply = ("I have the location. What service do you need there: cleaning, "
                     "maintenance, IT support, or supplies?")
        else:
            reply = "What service do you need, and in which room, floor, or location?"
        return {
            "intent": intent, "state": "awaiting_user", "listen_again": True,
            "speech_reply": reply,
            # Priority follows from the category, not the location -- so a request
            # created without a location (clarification cap, dashboard simulate)
            # is still prioritised correctly.
            "request": {"category": category or "other", "location": location,
                        "priority": PRIORITY_FOR_CATEGORY.get(category, "medium"),
                        "assigned_team": TEAM_FOR_CATEGORY.get(category, "facilities"),
                        "confidence": 0.5, "safety_flag": False,
                        "missing_fields": missing},
            "device_actions": [], "source": "rules",
        }

    team = TEAM_FOR_CATEGORY.get(category, "facilities")
    return {
        "intent": intent, "state": "complete", "listen_again": False,
        "speech_reply": ("Got it. A %s request for %s has been sent to the %s team."
                         % (category.replace("_", " "), location.replace("-", " "), team)),
        "request": {"category": category, "location": location,
                    "priority": PRIORITY_FOR_CATEGORY.get(category, "medium"),
                    "assigned_team": team, "confidence": 0.5 if category == "other" else 0.6,
                    "safety_flag": False, "missing_fields": []},
        "device_actions": [], "source": "rules",
    }


# --------------------------------------------------------------------------
# Foundry agent call
# --------------------------------------------------------------------------
def _call_foundry(transcript, panel_location, device_id, prior_turns, stt_confidence):
    """Call the registered Foundry agent using the v1 threads/runs API.

    Authentication uses ``DefaultAzureCredential``. In Azure this resolves to the
    Function App's managed identity; developers can use ``az login`` locally.
    Failures bubble up so ``classify`` can safely use the rules fallback.
    """
    from azure.ai.agents.models import ToolOutput
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    payload = {
        "transcript": transcript,
        "device_id": device_id,
        "prior_turns": prior_turns,
        "stt_confidence": stt_confidence,
        "output_schema": AGENT_OUTPUT_SCHEMA,
    }
    client = AIProjectClient(endpoint=config.AI_FOUNDRY_ENDPOINT,
                             credential=DefaultAzureCredential())
    thread = client.agents.threads.create()
    client.agents.messages.create(thread_id=thread.id, role="user",
                                  content=json.dumps(payload))
    run = client.agents.runs.create(thread_id=thread.id,
                                    agent_id=config.AI_FOUNDRY_AGENT_ID)
    deadline = time.monotonic() + (config.AGENT_TIMEOUT_MS / 1000.0)

    while _run_status(run) in ("queued", "in_progress", "requires_action"):
        if time.monotonic() >= deadline:
            try:
                client.agents.runs.cancel(thread_id=thread.id, run_id=run.id)
            except Exception:
                log.debug("could not cancel timed-out Foundry run", exc_info=True)
            raise TimeoutError("Foundry agent exceeded AGENT_TIMEOUT_MS")
        if _run_status(run) == "requires_action":
            outputs = _execute_required_tools(run, ToolOutput)
            run = client.agents.runs.submit_tool_outputs(
                thread_id=thread.id, run_id=run.id, tool_outputs=outputs)
        else:
            time.sleep(0.25)
            run = client.agents.runs.get(thread_id=thread.id, run_id=run.id)

    if _run_status(run) != "completed":
        raise RuntimeError("Foundry run ended with status %s: %s" %
                           (run.status, getattr(run, "last_error", None)))
    for message in client.agents.messages.list(thread_id=thread.id):
        role = str(getattr(message, "role", "")).lower()
        if role.endswith("agent") or role.endswith("assistant"):
            text = _message_text(message)
            if text:
                return _parse_agent_output(text)
    raise ValueError("Foundry run completed without an agent text response")


def _run_status(run):
    status = getattr(run, "status", "")
    return str(getattr(status, "value", status)).lower()


def _execute_required_tools(run, tool_output_type):
    action = getattr(run, "required_action", None)
    details = getattr(action, "submit_tool_outputs", None)
    calls = getattr(details, "tool_calls", None) or []
    if not calls:
        raise ValueError("Foundry requested an action without tool calls")
    outputs = []
    for call in calls:
        function = getattr(call, "function", None)
        name = getattr(function, "name", None)
        if name != "lookup_requests":
            raise ValueError("Foundry requested unsupported tool: %s" % name)
        try:
            args = json.loads(getattr(function, "arguments", "{}") or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("Foundry supplied invalid tool arguments") from exc
        if not isinstance(args, dict) or set(args) - {"device_id", "location", "limit"}:
            raise ValueError("Foundry supplied unsupported lookup arguments")
        limit = args.get("limit", 3)
        if not isinstance(limit, int):
            raise ValueError("lookup_requests limit must be an integer")
        args["limit"] = max(1, min(limit, 10))
        outputs.append(tool_output_type(tool_call_id=call.id,
                                        output=json.dumps(lookup_requests(**args))))
    return outputs


def _message_text(message):
    text_messages = getattr(message, "text_messages", None) or []
    if text_messages:
        return getattr(getattr(text_messages[-1], "text", None), "value", "")
    for block in reversed(getattr(message, "content", None) or []):
        value = getattr(getattr(block, "text", None), "value", None)
        if value:
            return value
    return ""


def _parse_agent_output(text):
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        out = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError("Foundry response is not valid JSON") from exc
    _validate_agent_output(out)
    return out


def _validate_agent_output(out):
    if not isinstance(out, dict):
        raise ValueError("Foundry response must be a JSON object")
    if out.get("intent") not in INTENTS:
        raise ValueError("Foundry response has an invalid intent")
    if out.get("state") not in ("complete", "awaiting_user", "escalated_to_human", "rejected"):
        raise ValueError("Foundry response has an invalid state")
    if not isinstance(out.get("listen_again"), bool):
        raise ValueError("Foundry response listen_again must be boolean")
    if not isinstance(out.get("speech_reply"), str) or len(out["speech_reply"]) > 300:
        raise ValueError("Foundry response has an invalid speech_reply")
    actions = out.get("device_actions", [])
    if not isinstance(actions, list):
        raise ValueError("Foundry response device_actions must be an array")
    out["device_actions"] = actions
    request = out.get("request")
    if request is not None:
        if not isinstance(request, dict):
            raise ValueError("Foundry response request must be an object or null")
        if request.get("category") not in CATEGORIES:
            raise ValueError("Foundry response has an invalid category")
        if request.get("priority") not in PRIORITIES:
            raise ValueError("Foundry response has an invalid priority")
        if not isinstance(request.get("assigned_team"), str):
            raise ValueError("Foundry response has an invalid assigned_team")
        confidence = request.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError("Foundry response confidence must be between 0 and 1")
        if not isinstance(request.get("safety_flag"), bool):
            raise ValueError("Foundry response safety_flag must be boolean")
        if not isinstance(request.get("missing_fields", []), list):
            raise ValueError("Foundry response missing_fields must be an array")


def classify(transcript, panel_location, device_id, prior_turns, stt_confidence, safety_flag):
    """Classify one utterance. Never raises -- falls back to rules on any failure."""
    if config.USE_AGENT:
        try:
            out = _call_foundry(transcript, panel_location, device_id,
                                prior_turns, stt_confidence)
            out = _require_request_details(out)
            out["source"] = "agent"
            return out
        except Exception as exc:
            log.exception("agent call failed (%s) -- using rule-based fallback", exc)
    return classify_with_rules(transcript, panel_location, prior_turns, safety_flag)


def _require_request_details(out):
    """Enforce that a normal request has both service and spoken location.

    This is application policy, not a model suggestion. The Foundry payload does
    not include the panel location, so it cannot be copied into a ticket.
    """
    if out.get("intent") not in ("new_request", "clarification_answer"):
        return out
    request = out.get("request") or {}
    missing = []
    if not request.get("category") or request.get("category") == "other":
        missing.append("category")
    if not request.get("location"):
        missing.append("location")
    if not missing:
        return out
    request.setdefault("category", "other")
    request.setdefault("location", None)
    request.setdefault("priority", "medium")
    request.setdefault("assigned_team", "facilities")
    request.setdefault("confidence", 0.5)
    request.setdefault("safety_flag", False)
    request["missing_fields"] = missing
    out["request"] = request
    out["state"] = "awaiting_user"
    out["listen_again"] = True
    if missing == ["location"]:
        out["speech_reply"] = "What room, floor, or location needs that service?"
    elif missing == ["category"]:
        out["speech_reply"] = ("I have the location. What service do you need there: "
                               "cleaning, maintenance, IT support, or supplies?")
    else:
        out["speech_reply"] = "What service do you need, and in which room, floor, or location?"
    return out
