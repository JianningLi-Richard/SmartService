"""Layer 1 of safety handling: a keyword filter that runs BEFORE the agent.

Safety detection is deliberately not left to the model alone. This filter sets
safety_flag independently of the agent; the agent may also set it from semantic
understanding (layer 2); and workflow.py enforces the supervisor route in code
(layer 3), overriding whatever team the agent proposed.

Keep this list conservative and auditable. False positives cost a supervisor a
glance at the dashboard; false negatives are the failure that matters.
"""

import re

SAFETY_KEYWORDS = [
    # injury
    "fell", "fall", "fallen", "collapsed", "unconscious", "not breathing",
    "blood", "bleeding", "injured", "hurt", "broken arm", "broken leg",
    "electric shock", "electrocuted", "burn", "burned", "choking", "seizure",
    # hazard
    "fire", "smoke", "gas leak", "flooding", "sparks", "exposed wire",
    "chemical spill", "carbon monoxide",
    # explicit calls for help
    "emergency", "call 911", "ambulance", "someone is hurt",
    "call nine one one", "phone nine one one",
]

SAFETY_PHRASES = [
    "isn't moving", "isnt moving", "is not moving", "won't wake", "wont wake",
    "can't breathe", "cant breathe", "needs help now",
    "fall down the stairs", "fell down the stairs",
    "fawn thou endure stairs", "for thou endure stairs",
]

_WORD_RE = [re.compile(r"\b" + re.escape(k) + r"\b") for k in SAFETY_KEYWORDS]
_PHRASE_RE = [re.compile(r"\b" + re.escape(p) + r"\b") for p in SAFETY_PHRASES]


def keyword_flag(transcript):
    """True if the transcript matches the pre-agent safety filter."""
    if not transcript:
        return False
    low = transcript.lower()
    if any(rx.search(low) for rx in _WORD_RE):
        return True
    return any(rx.search(low) for rx in _PHRASE_RE)


def resolve(transcript, agent_flag):
    """Combine layer 1 and layer 2. Returns (safety_flag, flag_source)."""
    if keyword_flag(transcript):
        return True, "keyword"
    if agent_flag:
        return True, "agent"
    return False, None
