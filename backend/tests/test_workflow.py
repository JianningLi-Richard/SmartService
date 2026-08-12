"""The ten evaluation cases from section 6 of the proposal, as runnable tests.

    cd backend && python3 -m pytest tests/ -v
    cd backend && python3 tests/test_workflow.py     # no pytest needed

These run against the workflow directly -- no Functions runtime, no Azure.
When the Foundry agent is wired, these same tests must still pass.
"""

import os
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import agent, store, workflow  # noqa: E402


def ts(sec=0):
    return "2026-08-06T13:40:%02dZ" % (10 + sec)


def turn(session, n, text, conf=0.9, location="3F-Washroom", device="pi-3f-01"):
    status, body, telem = workflow.handle_turn({
        "session_id": session, "turn": n, "device_id": device, "location": location,
        "transcript": text, "stt_confidence": conf, "timestamp": ts(n),
    })
    return body


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        store._store = store.MemoryStore()  # fresh state per test

    # 1 Normal
    def test_normal_single_turn(self):
        r = turn("s-1", 1, "the printer on floor two is jammed")
        self.assertEqual(r["intent"], "new_request")
        self.assertEqual(r["state"], "complete")
        self.assertFalse(r["listen_again"])
        self.assertEqual(r["request"]["category"], "it_support")
        self.assertEqual(r["request"]["assigned_team"], "it")
        self.assertTrue(r["request"]["request_id"].startswith("REQ-"))

    # 2 Clarification
    def test_clarification_then_create(self):
        r1 = turn("s-2", 1, "it's dirty in here")
        self.assertEqual(r1["state"], "awaiting_user")
        self.assertTrue(r1["listen_again"])
        self.assertIsNone(r1["request"])  # no record created yet
        r2 = turn("s-2", 2, "third floor washroom")
        self.assertEqual(r2["state"], "complete")
        self.assertEqual(r2["request"]["location"], "3F-Washroom")
        self.assertEqual(r2["request"]["category"], "cleaning")

    # 3 Clarification limit
    def test_clarification_capped_at_two_turns(self):
        turn("s-3", 1, "it's dirty in here")
        r2 = turn("s-3", 2, "um over there")
        self.assertEqual(r2["state"], "awaiting_user")
        r3 = turn("s-3", 3, "still over there")
        self.assertEqual(r3["state"], "escalated_to_human")
        self.assertFalse(r3["listen_again"])  # no infinite loop
        self.assertEqual(r3["request"]["assigned_team"], "supervisor")

    def test_location_first_then_asks_for_service(self):
        r1 = turn("s-location-first", 1, "room 205")
        self.assertEqual(r1["state"], "awaiting_user")
        self.assertTrue(r1["listen_again"])
        self.assertIn("service", r1["speech_reply"].lower())
        self.assertIsNone(r1["request"])
        r2 = turn("s-location-first", 2, "the printer is broken")
        self.assertEqual(r2["state"], "complete")
        self.assertEqual(r2["request"]["location"], "Room-205")
        self.assertEqual(r2["request"]["category"], "it_support")

    def test_service_first_does_not_use_panel_location(self):
        r = turn("s-service-first", 1, "the printer is broken",
                 location="3F-Washroom")
        self.assertEqual(r["state"], "awaiting_user")
        self.assertIn("location", r["speech_reply"].lower())
        self.assertIsNone(r["request"])

    # 4 Tool use
    def test_status_query_uses_lookup_and_creates_nothing(self):
        turn("s-4a", 1, "the printer on floor two is jammed")
        before = len(store.get_store().list_requests())
        r = turn("s-4b", 1, "what happened to my request")
        self.assertEqual(r["intent"], "status_query")
        self.assertIsNone(r["request"])
        self.assertIn("REQ-", r["speech_reply"])
        self.assertEqual(len(store.get_store().list_requests()), before)

    # 5 Safety (keyword)
    def test_safety_keyword_forced_to_supervisor(self):
        r = turn("s-5", 1, "someone fell down the stairs")
        self.assertEqual(r["state"], "escalated_to_human")
        self.assertTrue(r["request"]["safety_flag"])
        self.assertEqual(r["request"]["assigned_team"], "supervisor")
        self.assertEqual(r["request"]["priority"], "critical")
        actuators = [a["actuator"] for a in r["device_actions"]]
        self.assertIn("led_red", actuators)
        self.assertIn("buzzer", actuators)

    # 6 Safety (semantic)
    def test_safety_semantic_same_enforced_outcome(self):
        r = turn("s-6", 1, "he isn't moving and I'm scared")
        self.assertEqual(r["state"], "escalated_to_human")
        self.assertTrue(r["request"]["safety_flag"])
        self.assertEqual(r["request"]["assigned_team"], "supervisor")

    # 7 Out of scope
    def test_out_of_scope_refused(self):
        r = turn("s-7", 1, "unlock the server room door")
        self.assertEqual(r["intent"], "out_of_scope")
        self.assertEqual(r["state"], "rejected")
        self.assertIsNone(r["request"])
        self.assertNotIn("lock", [a["actuator"] for a in r["device_actions"]])

    # 8 Edge: rapid double press
    def test_double_press_creates_one_request(self):
        turn("s-8", 1, "the light in the lobby is broken")
        before = len(store.get_store().list_requests())
        turn("s-8", 1, "the light in the lobby is broken")  # identical replay
        self.assertEqual(len(store.get_store().list_requests()), before)

    # 9 Failure: retry after network drop
    def test_retry_keeps_original_timestamp_and_does_not_duplicate(self):
        first = turn("s-9", 1, "there is a spill in the cafeteria")
        rid = first["request"]["request_id"]
        turn("s-9", 1, "there is a spill in the cafeteria")
        rows = [r for r in store.get_store().list_requests()
                if r["transcript"].startswith("there is a spill")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["request_id"], rid)
        self.assertEqual(rows[0]["created_at"][:4], "2026")

    # 10 Low confidence
    def test_low_confidence_asks_to_repeat(self):
        r = turn("s-10", 1, "mumble mumble", conf=0.3)
        self.assertEqual(r["state"], "awaiting_user")
        self.assertTrue(r["listen_again"])
        self.assertIsNone(r["request"])
        self.assertIn("again", r["speech_reply"].lower())


class ContractTests(unittest.TestCase):
    def setUp(self):
        store._store = store.MemoryStore()

    def test_every_response_matches_the_envelope(self):
        for text in ["the printer is jammed", "it's dirty in here",
                     "someone fell down the stairs", "unlock the server room door",
                     "what happened to my request"]:
            r = turn("s-env-" + text[:4], 1, text)
            for field in ("session_id", "turn", "intent", "state", "listen_again",
                          "request", "device_actions", "speech_reply",
                          "audio_format", "audio_b64"):
                self.assertIn(field, r, "%r missing %s" % (text, field))
            self.assertIsInstance(r["listen_again"], bool)
            self.assertIn(r["state"],
                          ("complete", "awaiting_user", "escalated_to_human", "rejected"))

    def test_only_whitelisted_actions_reach_the_device(self):
        from shared.whitelist import ACTUATORS
        for text in ["the printer is jammed", "someone fell down the stairs",
                     "unlock the server room door"]:
            r = turn("s-wl-" + text[:4], 1, text)
            for a in r["device_actions"]:
                self.assertIn(a["actuator"], ACTUATORS)
                self.assertIn(a["action"], ACTUATORS[a["actuator"]])

    def test_unknown_actions_are_dropped(self):
        from shared.whitelist import validate_actions
        clean, dropped = validate_actions([
            {"actuator": "door_lock", "action": "open"},          # not whitelisted
            {"actuator": "led_green", "action": "explode"},        # bad action
            {"actuator": "led_green", "action": "pulse", "duration_ms": 999999},
            {"actuator": "lcd", "action": "show", "text": "x" * 100},
        ])
        self.assertEqual(len(clean), 2)
        self.assertEqual(len(dropped), 2)
        self.assertEqual(clean[0]["duration_ms"], 10000)   # clamped
        self.assertEqual(len(clean[1]["text"]), 32)        # truncated

    def test_missing_fields_rejected(self):
        for missing in ("session_id", "device_id", "location", "transcript", "timestamp"):
            payload = {"session_id": "s", "turn": 1, "device_id": "d", "location": "l",
                       "transcript": "t", "stt_confidence": 0.9, "timestamp": ts()}
            payload[missing] = ""
            with self.assertRaises(workflow.PayloadError):
                workflow.handle_turn(payload)

    def test_foundry_json_response_is_validated(self):
        parsed = agent._parse_agent_output("""```json
        {"intent":"new_request","state":"complete","listen_again":false,
         "speech_reply":"Done","request":{"category":"cleaning","location":"1F-Lobby",
         "priority":"medium","assigned_team":"custodial","confidence":0.9,
         "safety_flag":false,"missing_fields":[]},"device_actions":[]}
        ```""")
        self.assertEqual(parsed["request"]["category"], "cleaning")
        with self.assertRaises(ValueError):
            agent._parse_agent_output('{"intent":"invented"}')

    def test_foundry_tool_calls_are_allowlisted_and_bounded(self):
        call = SimpleNamespace(
            id="call-1",
            function=SimpleNamespace(name="lookup_requests", arguments='{"limit":999}'),
        )
        run = SimpleNamespace(required_action=SimpleNamespace(
            submit_tool_outputs=SimpleNamespace(tool_calls=[call])))

        class Output:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        outputs = agent._execute_required_tools(run, Output)
        self.assertEqual(outputs[0].tool_call_id, "call-1")
        self.assertEqual(outputs[0].output, "[]")

        call.function.name = "delete_request"
        with self.assertRaises(ValueError):
            agent._execute_required_tools(run, Output)

    def test_unspoken_agent_location_is_rejected(self):
        out = {
            "intent": "new_request", "state": "complete", "listen_again": False,
            "speech_reply": "Created", "device_actions": [],
            "request": {"category": "it_support", "location": "3F-Washroom",
                        "priority": "medium", "assigned_team": "it",
                        "confidence": 0.9, "safety_flag": False, "missing_fields": []},
        }
        checked = agent._require_request_details(out, "the printer is broken")
        self.assertEqual(checked["state"], "awaiting_user")
        self.assertIsNone(checked["request"]["location"])
        self.assertIn("location", checked["request"]["missing_fields"])

    def test_spoken_room_location_is_accepted(self):
        out = {
            "intent": "new_request", "state": "complete", "listen_again": False,
            "speech_reply": "Created", "device_actions": [],
            "request": {"category": "cleaning", "location": "Room-205",
                        "priority": "medium", "assigned_team": "custodial",
                        "confidence": 0.9, "safety_flag": False, "missing_fields": []},
        }
        checked = agent._require_request_details(out, "cleaning is needed in room 205")
        self.assertEqual(checked["state"], "complete")


class DashboardTests(unittest.TestCase):
    def setUp(self):
        store._store = store.MemoryStore()

    def test_ack_and_complete_are_attributed(self):
        r = turn("s-d1", 1, "the printer on floor two is jammed")
        rid = r["request"]["request_id"]
        acked = workflow.acknowledge(rid, "staff@demo")
        self.assertEqual(acked["status"], "acknowledged")
        self.assertEqual(acked["acknowledged_by"], "staff@demo")
        done = workflow.complete_request(rid, "staff@demo")
        self.assertEqual(done["status"], "completed")
        self.assertIsNotNone(done["resolution_time_s"])

    def test_simulate_creates_a_request_without_a_panel(self):
        rec = workflow.simulate({"transcript": "trash overflowing in the cafeteria"})
        self.assertEqual(rec["location"], "1F-Cafeteria")
        self.assertEqual(rec["assigned_team"], "custodial")

    def test_escalation_sweep_promotes_stale_high_priority(self):
        rec = workflow.simulate({"transcript": "the elevator door is broken"})
        self.assertEqual(rec["priority"], "high")
        store.get_store().update_request(rec["request_id"],
                                         {"created_at": "2026-08-06T00:00:00Z"})
        escalated = workflow.escalation_sweep()
        self.assertIn(rec["request_id"], escalated)


if __name__ == "__main__":
    unittest.main(verbosity=2)
