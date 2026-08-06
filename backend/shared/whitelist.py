"""Actuator whitelist.

The agent may only emit device actions from this fixed enumeration. Every action
is validated here before it reaches the device, so a model error cannot become an
unintended physical action. Anything unrecognised is discarded, not passed through.

This table must stay in sync with docs/api-contract.md and the Pi firmware.
"""

ACTUATORS = {
    "led_blue":  {"on", "off", "pulse"},
    "led_amber": {"on", "off", "pulse"},
    "led_green": {"on", "off", "pulse"},
    "led_red":   {"on", "off", "blink_fast"},
    "buzzer":    {"beep_short", "beep_double", "pattern_urgent"},
    "lcd":       {"show", "clear"},
    "speaker":   {"play_reply"},
}

MAX_DURATION_MS = 10000
MAX_LCD_CHARS = 32
MAX_ACTIONS = 6


def validate_actions(actions, log=None):
    """Return only the actions that are structurally valid and whitelisted.

    Returns (clean_actions, dropped) so the caller can log what was rejected --
    a model repeatedly proposing invalid actions is a signal worth seeing.
    """
    clean, dropped = [], []
    if not isinstance(actions, list):
        return [], [{"reason": "not_a_list", "value": repr(actions)[:200]}]

    for a in actions[:MAX_ACTIONS]:
        if not isinstance(a, dict):
            dropped.append({"reason": "not_an_object", "value": repr(a)[:200]})
            continue
        actuator, action = a.get("actuator"), a.get("action")
        if actuator not in ACTUATORS:
            dropped.append({"reason": "unknown_actuator", "actuator": actuator})
            continue
        if action not in ACTUATORS[actuator]:
            dropped.append({"reason": "unknown_action", "actuator": actuator, "action": action})
            continue

        out = {"actuator": actuator, "action": action}
        if "duration_ms" in a:
            try:
                out["duration_ms"] = max(0, min(int(a["duration_ms"]), MAX_DURATION_MS))
            except (TypeError, ValueError):
                pass
        if actuator == "lcd" and action == "show":
            out["text"] = str(a.get("text", ""))[:MAX_LCD_CHARS]
        clean.append(out)

    if len(actions) > MAX_ACTIONS:
        dropped.append({"reason": "too_many_actions", "count": len(actions)})
    if dropped and log:
        log.warning("dropped device actions: %s", dropped)
    return clean, dropped


# --- canonical action sets the workflow emits for each outcome -------------

def listening_again():
    return [
        {"actuator": "led_amber", "action": "pulse", "duration_ms": 2000},
        {"actuator": "speaker", "action": "play_reply"},
    ]


def complete(request_id):
    return [
        {"actuator": "led_green", "action": "pulse", "duration_ms": 3000},
        {"actuator": "lcd", "action": "show", "text": request_id[:MAX_LCD_CHARS]},
        {"actuator": "speaker", "action": "play_reply"},
    ]


def safety():
    return [
        {"actuator": "led_red", "action": "blink_fast", "duration_ms": 5000},
        {"actuator": "buzzer", "action": "pattern_urgent", "duration_ms": 1500},
        {"actuator": "lcd", "action": "show", "text": "SUPERVISOR ALERTED"},
        {"actuator": "speaker", "action": "play_reply"},
    ]


def refused():
    return [
        {"actuator": "led_amber", "action": "pulse", "duration_ms": 2000},
        {"actuator": "lcd", "action": "show", "text": "NOT SUPPORTED"},
        {"actuator": "speaker", "action": "play_reply"},
    ]


def error():
    return [
        {"actuator": "led_red", "action": "on", "duration_ms": 2000},
        {"actuator": "buzzer", "action": "beep_short", "duration_ms": 300},
    ]
