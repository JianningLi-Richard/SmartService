# SmartService Raspberry Pi Hardware Setup and Testing Guide

This guide explains how to assemble, configure, run, and test the Raspberry Pi voice client for the SmartService Request System.

The client performs the following workflow:

1. Records speech through a USB microphone.
2. Converts English speech to text locally with Vosk.
3. Sends the transcript and device information to the SmartService backend.
4. Receives a reply, state, and LED action from the backend.
5. Uses the red and green LEDs to show the current result.

> Important: the Vosk model is intentionally excluded from Git because it is large. Each Raspberry Pi must download the model during setup.

## 1. Required hardware

| Item                               | Quantity  | Notes                                   |
| ---------------------------------- | --------- | --------------------------------------- |
| Raspberry Pi                       | 1         | Raspberry Pi 3, 4, or 5 recommended     |
| microSD card                       | 1         | 16 GB or larger                         |
| Raspberry Pi power supply          | 1         | Use the correct supply for the Pi model |
| USB microphone or anything related | 1         | A USB headset microphone also works     |
| Red LED                            | 1         | Standard 3 mm or 5 mm LED               |
| Green LED                          | 1         | Standard 3 mm or 5 mm LED               |
| 220 Ω resistor                     | 2         | One resistor is required for each LED   |
| Breadboard                         | 1         | Recommended for prototyping             |
| Female-to-male jumper wires        | 4 or more | Used between GPIO header and breadboard |
| Network connection                 | 1         | Wi-Fi or Ethernet                       |

## 2. Safety rules

- Shut down and disconnect power before changing GPIO wiring.
- Never connect an LED directly to a GPIO pin. Always use a 220–330 Ω resistor.
- Use a GPIO pin as a 3.3 V signal only. Do not apply 5 V to a GPIO pin.
- Check the LED polarity before powering the Pi:
  - Long leg: anode, connected toward the GPIO pin through a resistor.
  - Short leg/flat side: cathode, connected to GND.
- Use BCM GPIO numbers in the Python program, not physical header numbers.

## 3. GPIO wiring

These values match the current `firmware/pi_voice_client.py` configuration.

| Component         | Raspberry Pi BCM pin | Physical header pin | Connection                       |
| ----------------- | -------------------- | ------------------- | -------------------------------- |
| TALK button       | GPIO17               | Pin 11              | Button between GPIO17 and GND    |
| Green LED anode   | GPIO27               | Pin 13              | GPIO27 → resistor → long LED leg |
| Green LED cathode | GND                  | Pin 9               | Short LED leg → GND              |
| Red LED anode     | GPIO22               | Pin 15              | GPIO22 → resistor → long LED leg |
| Red LED cathode   | GND                  | Pin 14              | Short LED leg → GND              |
| Buzzer signal     | GPIO18               | Pin 12              | Active buzzer signal             |
| LCD               | I2C address `0x27`    | SDA/SCL              | PCF8574 I2C backpack             |

Wiring path for each LED:

```text
GPIO output → 220–330 Ω resistor → LED long leg
LED short leg → Raspberry Pi GND
```

Before wiring, display the pin layout on the Raspberry Pi:

```bash
pinout
```

Then inspect the pin constants in the client:

```bash
grep -nE 'LED|GPIO|PIN' firmware/pi_voice_client.py
```

If `pi_voice_client.py` uses different BCM numbers, either wire the LEDs to those BCM pins or update the constants in the code. The physical pin numbers in the table must also be updated if the BCM numbers change.

## 4. Connect the microphone

1. Plug the USB microphone into the Raspberry Pi.
2. Boot the Raspberry Pi.
3. Open a terminal.
4. Confirm that Linux detects the microphone:

```bash
arecord -l
```

List the audio devices visible to Python later with:

```bash
python3 -c "import sounddevice as sd; print(sd.query_devices())"
```

The microphone must show at least one input channel. HDMI and headphone-only devices are output devices and should not be selected as the microphone.

## 5. Prepare Raspberry Pi OS

Update the package list and install the required system packages:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip \
  libportaudio2 portaudio19-dev alsa-utils unzip wget
```

Check Python:

```bash
python3 --version
```

## 6. Clone the project

```bash
cd ~
git clone https://github.com/JianningLi-Richard/SmartService.git
cd SmartService
```

If the repository already exists:

```bash
cd ~/project_smart_service_request_system/SmartService
git pull origin main
```

Confirm that the client exists:

```bash
ls -l firmware/pi_voice_client.py
```

## 7. Create the Python environment

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

If the repository includes `requirements.txt`, install it:

```bash
pip install -r requirements.txt
```

If no requirements file exists yet, install the packages used by the Raspberry Pi client:

```bash
pip install vosk sounddevice requests gpiozero
```

Verify the imports:

```bash
python -c "import vosk, sounddevice, requests, gpiozero; print('Python dependencies: OK')"
```

The virtual environment must be activated again after opening a new terminal:

```bash
cd ~/project_smart_service_request_system/SmartService
source .venv/bin/activate
```

## 8. Download the Vosk English model

Small Vosk models are appropriate for Raspberry Pi. Download the English small model from the official Vosk model page:

https://alphacephei.com/vosk/models

From the repository root, create the local model directory:

```bash
mkdir -p models
cd models
```

Download the small US English model. The model name used by the project may look similar to:

```text
vosk-model-small-en-us-0.15
```

After downloading the ZIP file, extract it into `models/`:

```bash
unzip vosk-model-small-en-us-0.15.zip
cd ..
```

Confirm the model path:

```bash
ls models/vosk-model-small-en-us-0.15
```

The directory should contain model data such as `am`, `conf`, and `graph`. Do not point the client at the ZIP file; point it at the extracted directory.

`models/` is listed in `.gitignore`, so the model remains local and is not uploaded to GitHub.

## 9. Configure the client

Copy the tracked example to an ignored local file. Replace the device-key
placeholder on the Raspberry Pi only:

```bash
cp firmware/smartservice.env.example firmware/smartservice.env
nano firmware/smartservice.env
chmod 600 firmware/smartservice.env
source firmware/smartservice.env
```

The file uses `NAME=value` syntax without `export`, so it works both with a
shell and with systemd's `EnvironmentFile=` directive. When loading it in a
shell, export its variables with:

```bash
set -a
source firmware/smartservice.env
set +a
```

For a systemd service, find the USB microphone index first:

```bash
source .venv/bin/activate
python -m sounddevice
```

Set that input index in `SMARTSERVICE_MICROPHONE_DEVICE`. This avoids relying
on PortAudio device `-1`, which is often unavailable to a system service.

The public Azure endpoint is included in the example for review. The real device
key must remain only in `firmware/smartservice.env` and Azure Key Vault; `*.env`
is ignored by Git.

Check the following settings:

| Setting      | Example                              | Purpose                                |
| ------------ | ------------------------------------ | -------------------------------------- |
| Backend URL  | `http://SERVER_IP:PORT/...`          | API endpoint receiving the request     |
| Device key   | environment variable                 | Sent as the `X-Device-Key` header       |
| Timeout      | `25` seconds                         | Allows time for Foundry and Speech      |
| Device ID    | `pi-3f-01`                           | Unique name for this Raspberry Pi      |
| Location     | `3F-Washroom`                        | Physical service location              |
| Model path   | `models/vosk-model-small-en-us-0.15` | Extracted Vosk model directory         |
| Sample rate  | `16000`                              | Speech audio sample rate               |
| Green GPIO   | `27`                                 | BCM number for the green LED           |
| Red GPIO     | `22`                                 | BCM number for the red LED             |
| Input device | default or device index              | USB microphone selected by sounddevice |

If a relative model path fails because the program is launched from another directory, use an absolute path, for example:

```text
/home/vinh/project_smart_service_request_system/SmartService/models/vosk-model-small-en-us-0.15
```

Save in Nano with `Ctrl+O`, press Enter, and exit with `Ctrl+X`.

## 10. Test the LEDs separately

Do this before running the complete client. It confirms the wiring and GPIO numbers.

Test the green LED:

```bash
python - <<'PY'
from gpiozero import LED
from time import sleep

green = LED(27)
green.on()
sleep(2)
green.off()
green.close()
print("Green LED test complete")
PY
```

Test the red LED:

```bash
python - <<'PY'
from gpiozero import LED
from time import sleep

red = LED(22)
red.on()
sleep(2)
red.off()
red.close()
print("Red LED test complete")
PY
```

If an LED does not light:

1. Disconnect power.
2. Reverse the LED if its polarity is incorrect.
3. Check the resistor and GND connection.
4. Confirm BCM versus physical pin numbering.
5. Confirm that the GPIO number matches the Python client.

## 11. Test the microphone separately

Record five seconds of audio:

```bash
arecord -d 5 -f S16_LE -r 16000 -c 1 microphone-test.wav
```

Play the recording:

```bash
aplay microphone-test.wav
```

If the wrong input is selected, inspect devices:

```bash
arecord -l
python -c "import sounddevice as sd; print(sd.query_devices())"
```

Set the correct input device index in `pi_voice_client.py` if the program supports an input-device setting.

## 12. Test backend connectivity

The Raspberry Pi and backend must be reachable over the network.

Check basic connectivity, replacing the host with the real backend address:

```bash
ping -c 4 SERVER_IP
```

If the backend provides a health endpoint:

```bash
curl -i http://SERVER_IP:PORT/health
```

If the server runs on another computer, do not configure the client with `localhost` or `127.0.0.1`. From the Raspberry Pi, those addresses refer to the Raspberry Pi itself. Use the server computer's LAN IP address or the deployed API URL.

## 13. Run the complete voice client

From the repository root:

```bash
source .venv/bin/activate
set -a
source firmware/smartservice.env
set +a
python firmware/pi_voice_client.py
```

Speak clearly after the client begins listening. Use one of the fixed demo sentences expected by the application.

A successful run should show output similar to:

```text
Raw transcript: it is on the second floor
Vosk confidence: 0.84
Matched transcript: it is on the second floor
Demo match score: 0.90
Sending request:
{
  "session_id": "s-example",
  "turn": 1,
  "device_id": "pi-3f-01",
  "location": "3F-Washroom",
  "transcript": "it is on the second floor",
  "stt_confidence": 0.84,
  "timestamp": "..."
}
Server response:
State: ...
Reply: ...
Action: led_green -> pulse
```

The exact state and LED action depend on the backend response. The test passes when:

- The microphone captures speech.
- Vosk produces a transcript.
- The client sends a correctly formed request.
- The backend returns a successful response.
- The client prints the state, reply, and action.
- The requested LED performs the expected action.

Stop the client with:

```text
Ctrl+C
```

## 14. Recommended end-to-end test cases

Run and record the result of each test.

| Test             | Action                              | Expected result                                                |
| ---------------- | ----------------------------------- | -------------------------------------------------------------- |
| Power-on         | Start the Pi and client             | No GPIO or model-loading error                                 |
| Green LED        | Run green LED test                  | Green LED lights for two seconds                               |
| Red LED          | Run red LED test                    | Red LED lights for two seconds                                 |
| Microphone       | Record and play five seconds        | Speech is understandable                                       |
| Normal phrase    | Speak a complete supported phrase   | Good transcript and backend response                           |
| Quiet speech     | Speak softly                        | Client either recognizes it or reports low confidence safely   |
| Background noise | Test with moderate noise            | Client does not crash                                          |
| Partial phrase   | Say only part of a sentence         | Client follows its low-match/retry behavior                    |
| Backend offline  | Stop/disconnect backend temporarily | Client reports a connection error without crashing permanently |
| Recovery         | Start backend and retry             | Request succeeds again                                         |

For a demonstration, capture evidence of:

1. Raspberry Pi and LED wiring.
2. USB microphone detected by `arecord -l`.
3. Both standalone LED tests.
4. A successful raw and matched transcript.
5. The JSON request sent by the client.
6. The server response.
7. The correct LED action.

## 15. Troubleshooting

### `ModuleNotFoundError`

Activate the environment and reinstall dependencies:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

Or, if no requirements file exists:

```bash
pip install vosk sounddevice requests gpiozero
```

### `PortAudioError` or no input device

```bash
sudo apt install -y libportaudio2 portaudio19-dev alsa-utils
arecord -l
python -c "import sounddevice as sd; print(sd.query_devices())"
```

Select a device that has input channels.

### Vosk model not found

- Confirm that the ZIP file was extracted.
- Confirm that the configured path points to the extracted model folder.
- Run the program from the repository root.
- Use an absolute model path if necessary.

### Transcript contains only part of the sentence

For example, the user may say “it is on the second floor,” but Vosk may return only “it is in.” This is an STT limitation rather than a wiring failure.

Try the following:

- Place the microphone closer to the speaker.
- Reduce background noise.
- Speak at a steady pace and wait until the listening prompt appears.
- Increase the recording/listening window if it is too short.
- Confirm the microphone sample rate is 16 kHz mono.
- Use the demo phrase matcher only as a fallback; keep a minimum match-score threshold so an unrelated phrase is not accepted.

### Backend connection refused or timed out

- Confirm the backend is running.
- Confirm the host, port, and API path.
- Do not use `localhost` for a backend running on another machine.
- Confirm both devices are on reachable networks.
- Check the server firewall.

### LED does not respond after a valid server response

- First repeat the standalone LED tests.
- Print/log the returned action value.
- Confirm the action name exactly matches the values handled by the client.
- Confirm the code uses the same BCM GPIO numbers as the wiring.
- Ensure another process is not already controlling the same GPIO pins.

### GPIO permission or backend error

Avoid running the entire client with `sudo` unless it is truly required. Installing the application in a virtual environment and using `gpiozero` is preferred. If the GPIO backend reports an error, record the exact message before changing packages or permissions.

## 16. Updating the Raspberry Pi code

Before pulling updates, confirm that local work is committed or intentionally ignored:

```bash
git status
git pull --rebase origin main
```

The `models/` directory remains local because it is ignored. After pulling, reactivate the environment and test again:

```bash
source .venv/bin/activate
python firmware/pi_voice_client.py
```

## 17. Completion checklist

- [ ] Raspberry Pi boots and has network access.

- [ ] USB microphone appears in `arecord -l`.

- [ ] Red and green LEDs each use a resistor.

- [ ] LED GPIO wiring matches `pi_voice_client.py`.

- [ ] Python virtual environment is created.

- [ ] Required Python packages are installed.

- [ ] Vosk model is downloaded and extracted locally.

- [ ] Backend URL, device ID, and location are correct.

- [ ] Standalone green LED test passes.

- [ ] Standalone red LED test passes.

- [ ] Microphone recording test passes.

- [ ] Backend health/connectivity test passes.

- [ ] Complete voice request reaches the backend.

- [ ] Server response is printed.

- [ ] Correct LED action is performed.

- [ ] Test evidence/screenshots are saved for the project demonstration.

## References

- Raspberry Pi documentation: https://www.raspberrypi.com/documentation/computers/raspberry-pi.html
- Raspberry Pi OS GPIO examples: https://www.raspberrypi.com/documentation/computers/os.html
- Official Vosk model list: https://alphacephei.com/vosk/models
- Python SoundDevice documentation: https://python-sounddevice.readthedocs.io/
