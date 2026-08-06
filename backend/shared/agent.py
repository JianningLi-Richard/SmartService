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

You receive a transcript of what someone said, the panel's location, and any
earlier turns of the same conversation. Determine the intent, extract category,
location and urgency, recommend a priority and responsible team, and write a short
spoken reply (one or two sentences, plain speech, no markdown, no lists).

Rules you must not break:
- Only emit device actions from the whitelist you were given. Never invent one.
- Never dispatch emergency services. If someone may be hurt, set safety_flag true
  and say a supervisor is being notified.
- Never invent a location or a request ID. If the location is missing, set
  state to awaiting_user, list "location" in missing_fields, set listen_again true,
  and ask one short question.
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
        if re.search(pattern, low):
            return loc
    return None


def detect_category(text):
    low = (text or "").lower()
    for cat, words in CATEGORY_KEYWORDS.items():
        if any(w in low for w in words):
            return cat
    return "other"


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
    if not location:
        return {
            "intent": intent, "state": "awaiting_user", "listen_again": True,
            "speech_reply": "Sure. Which room or floor is that in?",
            # Priority follows from the category, not the location -- so a request
            # created without a location (clarification cap, dashboard simulate)
            # is still prioritised correctly.
            "request": {"category": category, "location": None,
                        "priority": PRIORITY_FOR_CATEGORY.get(category, "medium"),
                        "assigned_team": TEAM_FOR_CATEGORY.get(category, "facilities"),
                        "confidence": 0.5, "safety_flag": False,
                        "missing_fields": ["location"]},
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
    """TODO(Aug 9-11): wire to Azure AI Foundry Agent Service.

    Sketch, so the shape is agreed before the code lands:

        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential
        client = AIProjectClient(endpoint=config.AI_FOUNDRY_ENDPOINT,
                                 credential=DefaultAzureCredential())
        thread = client.agents.threads.create()
        client.agents.messages.create(thread.id, role="user", content=json.dumps(payload))
        run = client.agents.runs.create_and_process(
            thread_id=thread.id, agent_id=config.AI_FOUNDRY_AGENT_ID)
        # handle requires_action -> lookup_requests(**args) -> submit_tool_outputs
        # parse the final message as JSON against AGENT_OUTPUT_SCHEMA

    Until then this raises, and classify() falls back to the rules above.
    """
    raise NotImplementedError("Foundry agent not wired yet")


def classify(transcript, panel_location, device_id, prior_turns, stt_confidence, safety_flag):
    """Classify one utterance. Never raises -- falls back to rules on any failure."""
    if config.USE_AGENT:
        try:
            out = _call_foundry(transcript, panel_location, device_id,
                                prior_turns, stt_confidence)
            out["source"] = "agent"
            return out
        except NotImplementedError:
            pass
        except Exception as exc:
            log.error("agent call failed (%s) -- using rule-based fallback", exc)
    return classify_with_rules(transcript, panel_location, prior_turns, safety_flag)
