# SmartService Firmware and Hardware Presentation & Testing Guide

## 1. Presentation Overview

SmartService is a wall-mounted voice service-request panel powered by a
Raspberry Pi. A user holds the TALK button, describes a building issue, and
releases the button. The device converts speech to text locally, sends the text
to the Azure backend, receives a safe structured response, operates the local
hardware, and plays a spoken reply.

The current demonstration setup uses:

- Raspberry Pi running Raspberry Pi OS (32-bit userland)
- Physical TALK button on GPIO 17
- Green LED on GPIO 27
- Red LED on GPIO 22
- Buzzer on GPIO 18
- 16x2 I2C LCD at address `0x27`
- JBL Wave Beam as the Bluetooth microphone
- Bose speaker as the audio output
- Vosk for offline speech-to-text
- Piper for normal local speech replies
- espeak-ng for immediate emergency replies
- Azure Functions for the service workflow
- Azure Key Vault-backed device authentication
- Azure Static Web Apps staff dashboard

## 2. End-to-End Flow

1. The user holds the TALK button.
2. The device beeps and records from the JBL microphone.
3. The user releases the TALK button.
4. Vosk converts the recording to text locally.
5. The Raspberry Pi sends the transcript, session data, device ID, confidence,
   and authentication header to Azure Functions.
6. The backend validates the device and classifies the request.
7. If information is missing, the device asks a short follow-up question.
8. If the request is complete, it is stored and displayed on the staff dashboard.
9. The backend returns only allowlisted device actions.
10. The Raspberry Pi updates its LED, buzzer, LCD, and Bose speaker.

The payload field `location: 3F-Washroom` identifies the physical panel. It must
not be automatically used as the requested service location. A user must speak a
room, floor, or specific location before a normal request is created.

## 3. Safety and Security Behaviour

- The device never unlocks doors, grants access, disables alarms, or bypasses
  security controls.
- Emergency phrases receive critical priority and are routed to a supervisor.
- The system does not call 911 and must never claim that it has called 911.
- For an emergency, the red LED and buzzer operate at the same time.
- Emergency speech uses espeak-ng because it responds immediately on this
  32-bit Raspberry Pi. Normal replies use the higher-quality Piper voice.
- Device actions returned by the backend are checked against an allowlist.
- The device key is stored locally in `firmware/smartservice.env`, protected with
  file permission `600`. It must never be committed or shown during a demo.

## 4. Pre-Demo Checklist

Run these checks after powering on the Raspberry Pi:

```bash
systemctl --user status smartservice --no-pager -l
pactl info | grep -E "Default Sink|Default Source"
tail -n 30 ~/SmartService/smartservice.log
```

Expected conditions:

- `smartservice.service` is `active (running)`.
- The default source is the JBL Wave Beam microphone.
- The default sink is the Bose speaker.
- Startup reports `Device authentication: configured`.
- The configured Vosk model exists.
- JBL and Bose have enough battery for the presentation.
- The Raspberry Pi has Internet access.
- The SmartService staff dashboard is open in a browser.

Do not display the real value of `SMARTSERVICE_DEVICE_KEY`.

## 5. Recommended Presentation Script

### Scenario A: Missing Location

Say:

> The washroom is dirty.

Expected result:

- State: `awaiting_user`
- The system asks for a room, floor, or location.
- No ticket is created yet.

Then say:

> Third floor washroom.

Expected result:

- State: `complete`
- Category: `cleaning`
- Location: `3F-Washroom`
- Assigned team: `custodial`
- Green LED pulses and the Bose speaker confirms the request.
- The new ticket appears on the dashboard.

### Scenario B: Complete IT Request

Say:

> The printer in room two zero five is broken.

Expected result:

- State: `complete`
- Category: `it_support`
- Location: `Room-205`
- Assigned team: `it`
- Only one ticket is created.

### Scenario C: Emergency

Say:

> Someone fell down the stairs. Call nine one one.

Expected result:

- State: `escalated_to_human`
- Priority: `critical`
- Assigned team: `supervisor`
- Red LED flashes while the buzzer sounds.
- The Bose speaker immediately announces that a supervisor is being notified.
- Explain that the system does not directly call emergency services.

### Scenario D: Restricted Security Command

Say:

> Unlock the door on the second floor.

Expected result:

- State: `rejected`
- No service ticket is created.
- No door or security action is executed.

### Scenario E: False Emergency Protection

Say:

> Please clean near the fireplace in room two zero five.

Expected result:

- A normal cleaning request is created.
- `fireplace` is not interpreted as a fire emergency.

### Scenario F: Dashboard Workflow

1. Open the newly created ticket on the staff dashboard.
2. Confirm its category, location, priority, and assigned team.
3. Acknowledge the ticket.
4. Mark the ticket complete.
5. Verify that the status and staff attribution are updated.

## 6. Test Matrix

| Test | Input or action | Expected result |
|---|---|---|
| Service first | `The printer is broken` | Ask for location; create no ticket |
| Location first | `Room two zero five` | Ask what service is required |
| Complete request | `The printer in room 205 is broken` | One IT ticket for Room 205 |
| Generic washroom | `The washroom is dirty` | Ask for explicit location |
| Emergency | `Someone fell down; call 911` | Critical supervisor escalation |
| False emergency | `Clean near the fireplace` | Cleaning; no safety escalation |
| Restricted action | `Open the door` | Rejected; no ticket/action |
| Status question | `What happened to my request?` | Look up status; create no ticket |
| Low confidence | Quiet or unclear speech | Ask the user to repeat |
| Empty speech | Press and release without speaking | No request; prompt to try again |
| Duplicate | Repeat the same network turn | No duplicate ticket |
| Wrong device key | Use a temporary invalid key | HTTP 401; no request created |
| Backend offline | Disconnect network | Visible failure; client remains stable |
| Reboot | Reboot Raspberry Pi | Service and audio recover automatically |

## 7. Performance Evidence

The log reports separate timing measurements:

```text
Local STT processing: X.XXs
Backend round trip: X.XXs
```

Use these values to explain system performance:

- Local STT time is affected by the Vosk model, Raspberry Pi CPU, recording
  length, and Bluetooth audio quality.
- Backend round-trip time covers the network request and Azure workflow.
- The fast-routing mode avoids a slow remote-agent round trip for Raspberry Pi
  requests.
- Piper voice generation is local but can be slow on 32-bit hardware.
- Emergency replies use espeak-ng to avoid delaying a safety notification.

## 8. Quick Recovery Commands

Restart the application:

```bash
systemctl --user restart smartservice
tail -f ~/SmartService/smartservice.log
```

Restore the JBL microphone and Bose speaker defaults:

```bash
pactl set-default-source bluez_input.08:EB:ED:FE:ED:CC
pactl set-default-sink bluez_output.2C_41_A1_2B_F7_BA.1
systemctl --user restart smartservice
```

Confirm audio devices:

```bash
pactl info | grep -E "Default Sink|Default Source"
wpctl status
```

Confirm the service after reboot:

```bash
systemctl --user enable smartservice
sudo loginctl enable-linger vinh
systemctl --user status smartservice --no-pager -l
```

Check recent logs:

```bash
tail -n 100 ~/SmartService/smartservice.log
```

## 9. Known Limitations

- Bluetooth speech recognition can mishear free-form speech, especially room
  numbers. Short phrases such as `room two zero five` are recommended.
- The small Vosk model is faster; the lgraph model is more accurate but too slow
  for this Raspberry Pi 32-bit setup.
- Piper provides better voice quality but is slower than espeak-ng.
- The service depends on Internet access for Azure request processing.
- The current system notifies a supervisor for emergencies; it does not contact
  police, fire, ambulance, or 911 directly.

## 10. Closing Statement

The SmartService prototype demonstrates authenticated voice-based building
service requests from physical hardware to Azure and back to a staff dashboard.
It supports multi-turn clarification, local hardware feedback, emergency
escalation, restricted-action rejection, duplicate protection, and staff request
management while keeping secrets outside source control.
