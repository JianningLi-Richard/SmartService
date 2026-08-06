"""Azure Functions HTTP API -- Smart Service Request System.

Routes implement docs/api-contract.md v1. Business logic lives in shared/workflow.py
so it can be unit-tested without the Functions runtime (see tests/).
"""

import json
import logging

import azure.functions as func

from shared import config, workflow
from shared.store import get_store

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("smartservice")

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

CORS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type, X-Device-Key",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
}


def _json(payload, status=200):
    return func.HttpResponse(json.dumps(payload), status_code=status, headers=CORS)


def _body(req):
    try:
        return req.get_json()
    except ValueError:
        return None


def _actor(req):
    """Staff identity for accountability. Azure Static Web Apps / App Service auth
    injects x-ms-client-principal-name once the deployment lead enables sign-in."""
    return (req.headers.get("x-ms-client-principal-name")
            or req.headers.get("X-Actor")
            or "unauthenticated")


# --------------------------------------------------------------------------
# Device endpoint -- the only one the Pi calls
# --------------------------------------------------------------------------
@app.route(route="voice/turn", methods=["POST", "OPTIONS"])
def voice_turn(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return _json({})

    body = _body(req)
    if body is None:
        return _json({"error": "invalid_payload", "message": "body is not valid JSON"}, 400)

    try:
        status, payload, telemetry = workflow.handle_turn(
            body, device_key=req.headers.get("X-Device-Key"))
    except workflow.PayloadError as exc:
        return _json({"error": "invalid_payload", "message": exc.message,
                      "session_id": body.get("session_id")}, 400)
    except Exception:
        log.exception("voice_turn failed")
        return _json({"error": "internal",
                      "message": "the request could not be processed",
                      "session_id": body.get("session_id")}, 500)

    # Step 11 -- structured log for Application Insights
    log.info("turn_complete %s", json.dumps(telemetry))
    return _json(payload, status)


# --------------------------------------------------------------------------
# Dashboard endpoints
# --------------------------------------------------------------------------
@app.route(route="requests", methods=["GET", "OPTIONS"])
def list_requests(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return _json({})
    items = get_store().list_requests(
        status=req.params.get("status"),
        location=req.params.get("location"),
        since=req.params.get("since"),
    )
    return _json({"items": items, "continuation_token": None})


@app.route(route="requests/{request_id}", methods=["GET"])
def get_request(req: func.HttpRequest) -> func.HttpResponse:
    rec = get_store().get_request(req.route_params.get("request_id"))
    return _json(rec) if rec else _json({"error": "not_found"}, 404)


@app.route(route="requests/{request_id}/ack", methods=["POST", "OPTIONS"])
def ack_request(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return _json({})
    actor = _actor(req)
    rec = workflow.acknowledge(req.route_params.get("request_id"), actor)
    if not rec:
        return _json({"error": "not_found"}, 404)
    log.info("acknowledged %s by %s", rec["request_id"], actor)
    return _json({"status": rec["status"], "acknowledged_by": rec["acknowledged_by"],
                  "acknowledged_at": rec["acknowledged_at"]})


@app.route(route="requests/{request_id}/complete", methods=["POST", "OPTIONS"])
def complete_request(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return _json({})
    actor = _actor(req)
    rec = workflow.complete_request(req.route_params.get("request_id"), actor)
    if not rec:
        return _json({"error": "not_found"}, 404)
    log.info("completed %s by %s in %ss", rec["request_id"], actor, rec["resolution_time_s"])
    return _json({"status": "completed", "resolution_time_s": rec["resolution_time_s"]})


@app.route(route="requests/simulate", methods=["POST", "OPTIONS"])
def simulate_request(req: func.HttpRequest) -> func.HttpResponse:
    """Demo backup path -- create a request without the panel."""
    if req.method == "OPTIONS":
        return _json({})
    body = _body(req) or {}
    try:
        return _json(workflow.simulate(body), 201)
    except workflow.PayloadError as exc:
        return _json({"error": "invalid_payload", "message": exc.message}, 400)


@app.route(route="reports/summary", methods=["GET", "OPTIONS"])
def reports_summary(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return _json({})
    items = get_store().list_requests(since=req.params.get("from"), limit=1000)
    if req.params.get("to"):
        items = [r for r in items if r["created_at"] <= req.params["to"]]

    def count_by(field):
        out = {}
        for r in items:
            out[r.get(field)] = out.get(r.get(field), 0) + 1
        return out

    done = [r for r in items if r.get("resolution_time_s") is not None]
    return _json({
        "total": len(items),
        "by_category": count_by("category"),
        "by_team": count_by("assigned_team"),
        "by_priority": count_by("priority"),
        "by_status": count_by("status"),
        "safety_flagged": sum(1 for r in items if r.get("safety_flag")),
        "mean_resolution_time_s": (sum(r["resolution_time_s"] for r in done) // len(done))
                                  if done else None,
    })


@app.route(route="health", methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    return _json({
        "status": "ok",
        "agent": "foundry" if config.USE_AGENT else "rule-based-fallback",
        "speech": "azure" if config.USE_SPEECH else "device-fallback",
        "storage": get_store().name,
        "version": "1.0.0",
    })


# --------------------------------------------------------------------------
# Step 9 -- escalation timer (every minute)
# --------------------------------------------------------------------------
@app.timer_trigger(schedule="0 * * * * *", arg_name="timer", run_on_startup=False)
def escalation_timer(timer: func.TimerRequest) -> None:
    escalated = workflow.escalation_sweep()
    if escalated:
        log.info("escalation_sweep %s", json.dumps({"escalated": escalated}))
