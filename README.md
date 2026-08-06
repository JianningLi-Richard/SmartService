# Smart Service Request System

Voice-driven AI request routing. INTP302 final project — public demonstration **Aug 19, 2026**.

| Role | Owner | Scope |
|---|---|---|
| Backend + dashboard | Shikun Zhang | Functions API, Routing Agent, workflow, safety enforcement, storage, dashboard |
| Cloud deployment | — | Azure provisioning, Key Vault, App Insights, CI/CD, Static Web App |
| Hardware / firmware | — | Pi panel, GPIO, Vosk STT, Piper TTS, executing `device_actions` |

**Start here: [docs/api-contract.md](docs/api-contract.md).** It is the agreement between all
three of us. It is frozen for the demo — if it has to change, say so in the team chat and bump
the version in its change log.

```
backend/     Azure Functions app (Python) — the real backend
mock/        Zero-dependency stub of the same contract — for firmware/dashboard development
dashboard/   Single-file staff dashboard
docs/        API contract
```

---

## For the hardware lead — start here, no Azure needed

The mock server implements the whole contract with hardcoded logic. Standard library
only: no `pip install`, no Azure account, works on the Pi itself.

```bash
python3 mock/mock_server.py
```

It listens on `0.0.0.0:7071`, so point the Pi at your laptop's LAN address:
`http://<laptop-ip>:7071/api/voice/turn`.

```bash
curl -s localhost:7071/api/health

curl -s -X POST localhost:7071/api/voice/turn \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"s-a3f9c1","turn":1,"device_id":"pi-3f-01","location":"3F-Washroom",
       "transcript":"the printer on floor two is jammed","stt_confidence":0.88,
       "timestamp":"2026-08-06T13:40:12Z"}'
```

Say these to exercise every branch the firmware must handle:

| Say | You get back |
|---|---|
| "the printer on floor two is jammed" | `state=complete`, green LED, request ID on the LCD |
| "it's dirty in here" | `state=awaiting_user`, `listen_again=true` — **reopen the mic without a button press** |
| → then "third floor washroom" | `state=complete` |
| → instead answer vaguely twice | `state=escalated_to_human`, no infinite loop |
| "what happened to my request" | `intent=status_query`, `request` is `null` — no new ticket |
| "someone fell down the stairs" | `state=escalated_to_human`, red LED + urgent buzzer |
| "unlock the server room door" | `intent=out_of_scope`, `state=rejected` |
| anything with `stt_confidence` < 0.55 | asks the user to repeat |

Two things that are easy to get wrong:

- **`timestamp` is when the user spoke, not when you sent the request.** On a network drop,
  queue the transcript with its original timestamp and resend it unchanged. The server
  deduplicates on `session_id` + `turn` + `timestamp`, which is the only reason a retry
  doesn't create a second ticket.
- **`audio_b64` can be empty.** That means TTS failed or timed out — speak `speech_reply`
  with the local Piper voice. Run `python3 mock/mock_server.py --no-audio` to test that path
  on demand. The mock otherwise returns a beep; check `audio_format`, don't assume MP3.

Ignore any actuator you don't recognise. The API already drops anything outside the
whitelist in [docs/api-contract.md](docs/api-contract.md), but defence in depth is free.

---

## Backend

Runs fully on local fallbacks — in-memory store, rule-based classifier, no TTS — until the
Azure resources exist. That is the same degraded path the proposal promises for outages,
built first so nothing is blocked on provisioning.

```bash
cd backend
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp local.settings.json.example local.settings.json   # gitignored; fill in as resources appear
func start
```

Requires Python 3.11 or 3.12 — the Functions Python worker does not support 3.14.

Tests are the ten evaluation cases from section 6 of the proposal, plus contract checks.
They run against the workflow directly, no Functions runtime and no Azure:

```bash
cd backend && python3 tests/test_workflow.py
```

`GET /api/health` reports which backend each dependency resolved to:

```json
{"status":"ok","agent":"rule-based-fallback","speech":"device-fallback","storage":"in-memory"}
```

### Layout

| File | Contains |
|---|---|
| `function_app.py` | HTTP routes and the escalation timer trigger |
| `shared/workflow.py` | The eleven-step workflow from section 7 — **the core** |
| `shared/agent.py` | Foundry agent client, `lookup_requests` tool, rule-based fallback |
| `shared/safety.py` | Layer 1 keyword filter (runs *before* the agent) |
| `shared/whitelist.py` | Actuator whitelist and validation |
| `shared/store.py` | Table Storage, with an in-memory backend |
| `shared/speech.py` | Azure TTS with timeout → empty audio → device falls back |
| `shared/config.py` | Every setting, read from the environment |

Safety is enforced in three layers and the last one is the one that counts: the keyword
filter flags independently of the model, the agent may also flag, and `workflow.py`
overrides the agent's proposed team in code. Nothing trusts the model's routing decision.

---

## Dashboard

Static, no build step. Open `dashboard/index.html` and set the backend URL in the header
(it remembers). Works against the mock server or the real API.

The text box creates a request without the panel — that is the demo backup path if the room
acoustics fail. Build and test it early.

---

## For the deployment lead

The backend reads configuration from environment variables only; nothing is in code and
nothing is in this repo. The full list is in
[docs/api-contract.md](docs/api-contract.md) section 3. Set them in App Settings / Key Vault.

Also needed:

- Point an Application Insights availability test at `GET /api/health`.
- Enable authenticated sign-in on the dashboard. The API reads
  `x-ms-client-principal-name` for the staff identity on acknowledge/complete; without it
  every action is attributed to `unauthenticated`.
- Set `DEVICE_KEYS` (`pi-3f-01:<key>,…`). **With it unset, device auth is open** — fine
  locally, not fine for the deployed demo.

---

## Status

- [x] API contract v1 frozen
- [x] Mock server — all ten evaluation cases
- [x] Functions skeleton, workflow, safety layers, whitelist, escalation timer
- [x] Rule-based fallback classifier
- [x] Dashboard: live list, acknowledge/complete, simulate, reporting
- [ ] Wire the Foundry agent (`shared/agent.py::_call_foundry`) — Aug 9–11
- [ ] Wire Azure Table Storage (code done, needs a connection string)
- [ ] Wire Azure Speech (code done, needs key + region)
- [ ] Deploy to Azure, App Insights alerts, latency tuning
