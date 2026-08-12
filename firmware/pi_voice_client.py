import json
import time
import uuid
import base64
import os
import subprocess
import re
import shutil
import sys
import wave
from datetime import datetime, timezone

import requests
import sounddevice as sd
from gpiozero import Button, LED, Buzzer
from RPLCD.i2c import CharLCD
from vosk import Model, KaldiRecognizer

# =========================================================
# Configuration
# =========================================================

def env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default

API_URL = os.getenv(
    "SMARTSERVICE_API_URL",
    "http://localhost:7071/api/voice/turn"
)  # Use local Functions by default; set the env variable for Azure.
DEVICE_KEY = os.getenv("SMARTSERVICE_DEVICE_KEY", "")
REQUEST_TIMEOUT_SECONDS = env_int("SMARTSERVICE_REQUEST_TIMEOUT_SECONDS", 25)
MICROPHONE_DEVICE_RAW = os.getenv("SMARTSERVICE_MICROPHONE_DEVICE", "").strip()
MICROPHONE_DEVICE = (int(MICROPHONE_DEVICE_RAW)
                     if MICROPHONE_DEVICE_RAW.lstrip("-").isdigit()
                     else (MICROPHONE_DEVICE_RAW or None))
MICROPHONE_WARMUP_SECONDS = env_float("SMARTSERVICE_MICROPHONE_WARMUP_SECONDS", 0.8)
MODEL_PATH = os.getenv(
    "SMARTSERVICE_VOSK_MODEL_PATH",
    "models/vosk-model-small-en-us-0.15",
)

PIPER_PATH = "tools/piper/piper"
PIPER_MODEL = "models/piper/en_US-lessac-medium.onnx"
PIPER_VOLUME = "1.5"

try:
    from piper import PiperVoice
except ImportError:
    PiperVoice = None

piper_voice = None

DEVICE_ID = "pi-3f-01"
DEVICE_LOCATION = "3F-Washroom"
SAMPLE_RATE = 16000

# =========================================================
# Hardware
# =========================================================

talk_button = Button(
    17,
    pull_up=True,
    bounce_time=0.15
)

green_led = LED(27)
red_led = LED(22)
buzzer = Buzzer(18)

lcd = CharLCD(
    i2c_expander="PCF8574",
    address=0x27,
    port=1,
    cols=16,
    rows=2,
    charmap="A02",
    auto_linebreaks=False,
    backlight_enabled=True
)
# =========================================================
# Vosk
# =========================================================

print("Loading Vosk model...")
vosk_model = Model(MODEL_PATH)
print("Vosk model loaded.")

if PiperVoice is not None and os.path.isfile(PIPER_MODEL):
    try:
        print("Loading Piper voice...")
        piper_voice = PiperVoice.load(PIPER_MODEL)
        print("Piper voice loaded.")
    except Exception as error:
        print(f"Piper voice could not be loaded: {error}")


# =========================================================
# Recording state
# =========================================================

audio_chunks = []
audio_stream = None
recording = False
processing = False

current_session_id = None
current_turn =1


# =========================================================
# Display and sound
# =========================================================

def show_lcd(line1="", line2=""):
    """Show two lines on the LCD."""

    line1 = str(line1)[:16].ljust(16)
    line2 = str(line2)[:16].ljust(16)

    lcd.clear()
    lcd.cursor_pos = (0, 0)
    lcd.write_string(line1)

    lcd.cursor_pos = (1, 0)
    lcd.write_string(line2)


def beep(duration=0.15):
    """Play one beep."""

    buzzer.on()
    time.sleep(duration)
    buzzer.off()


def beep_double():
    """Play two beeps."""

    for _ in range(2):
        beep()
        time.sleep(0.15)


def urgent_pattern(duration_ms):
    """Play an urgent buzzer pattern."""

    end_time = time.time() + duration_ms / 1000

    while time.time() < end_time:
        beep(0.1)
        time.sleep(0.1)


# =========================================================
# Microphone recording
# =========================================================

def audio_callback(indata, frames, time_info, status):
    """Receive audio from the JBL microphone."""

    if status:
        print(f"Audio status: {status}")

    if recording:
        audio_chunks.append(bytes(indata))


def start_recording():
    """Start recording using the already-warm Bluetooth stream."""

    global audio_stream
    global audio_chunks
    global recording

    audio_chunks = []
    recording = True

    if audio_stream is None:
        audio_stream = sd.RawInputStream(
            device=MICROPHONE_DEVICE,
            samplerate=SAMPLE_RATE,
            blocksize=4000,
            dtype="int16",
            channels=1,
            callback=audio_callback
        )
        audio_stream.start()


def stop_recording():
    """Stop collecting audio while keeping Bluetooth capture warm."""

    global audio_stream
    global recording

    recording = False



def close_audio_stream():
    """Close the persistent stream during application shutdown."""
    global audio_stream
    if audio_stream is not None:
        audio_stream.stop()
        audio_stream.close()
        audio_stream = None

# =========================================================
# Speech recognition
# =========================================================

def calculate_confidence(result):
    """Calculate average confidence."""

    words = result.get("result", [])

    if not words:
        return 0.0

    scores = [
        word.get("conf", 0.0)
        for word in words
    ]

    return sum(scores) / len(scores)
#clean transcript
def clean_transcript(transcript):
    """Remove unknown words and repair common headset number mistakes."""

    words = [
        word
        for word in transcript.split()
        if word != "[unk]"
    ]

    cleaned = " ".join(words).strip()
    digit_words = {
        "zero": "0", "oh": "0", "one": "1", "won": "1",
        "two": "2", "to": "2", "too": "2", "true": "2",
        "three": "3", "tree": "3", "four": "4", "for": "4",
        "five": "5", "fly": "5", "six": "6", "seven": "7",
        "eight": "8", "ate": "8", "nine": "9",
    }

    def normalize_room(match):
        tokens = match.group(1).lower().split()
        digits = [digit_words.get(token) for token in tokens]
        return ("room " + "".join(digits)) if all(digits) else match.group(0)

    number_words = "|".join(sorted(map(re.escape, digit_words), key=len, reverse=True))
    return re.sub(
        rf"\broom(?:\s+(?:number|is|in|the))*\s+((?:(?:{number_words})\s*){{1,5}})\b",
        normalize_room, cleaned, flags=re.IGNORECASE,
    ).strip()


def is_local_safety_alert(transcript):
    """Conservative emergency phrases that can alert before the network reply."""
    low = (transcript or "").lower()
    phrases = (
        "fell down", "has fallen", "collapsed", "unconscious",
        "not breathing", "can't breathe", "cant breathe", "bleeding",
        "someone is hurt", "call 911", "emergency", "fire", "smoke",
        "gas leak", "electric shock", "choking", "seizure",
    )
    return any(phrase in low for phrase in phrases)

def transcribe_audio():
    """Convert recorded audio into free-form text."""

    recognizer = KaldiRecognizer(
        vosk_model,
        SAMPLE_RATE
    )

    recognizer.SetWords(True)

    for chunk in audio_chunks:
        recognizer.AcceptWaveform(chunk)

    result = json.loads(
        recognizer.FinalResult()
    )

    transcript = result.get("text", "")
    confidence = calculate_confidence(result)

    return transcript, confidence


# =========================================================
# Server device actions
# =========================================================

def execute_action(item):
    """Execute one action returned by the server."""

    actuator = item.get("actuator")
    action = item.get("action")
    duration_ms = item.get("duration_ms", 1000)

    print(f"Action: {actuator} -> {action}")

    if actuator == "led_green":
        if action == "on":
            green_led.on()

        elif action == "off":
            green_led.off()

        elif action == "pulse":
            green_led.on()
            time.sleep(duration_ms / 1000)
            green_led.off()

    elif actuator == "led_red":
        if action == "off":
            red_led.off()

        elif action == "on":
            red_led.on()
            time.sleep(duration_ms / 1000)
            red_led.off()

        elif action == "blink_fast":
            end_time = time.time() + duration_ms / 1000

            while time.time() < end_time:
                red_led.on()
                time.sleep(0.15)
                red_led.off()
                time.sleep(0.15)

    elif actuator == "buzzer":
        if action == "beep_short":
            beep()

        elif action == "beep_double":
            beep_double()

        elif action == "pattern_urgent":
            urgent_pattern(duration_ms)

    elif actuator == "lcd":
        if action == "show":
            show_lcd(
                "Request status",
                item.get("text", "")
            )

        elif action == "clear":
            lcd.clear()

    elif actuator == "speaker":
        print("Speaker action received.")

    elif actuator in ("led_blue", "led_amber"):
        print(f"{actuator} is not connected yet.")

    else:
        print(f"Unknown action ignored: {actuator}")


def execute_actions(actions):
    """Execute all device actions."""

    for item in actions:
        execute_action(item)
        
# =========================================================
# JBL audio playback
# =========================================================

def play_server_audio(result):
    """Decode and play the server audio through JBL."""

    audio_b64 = result.get("audio_b64", "")
    audio_format = result.get("audio_format", "wav").lower()

    if not audio_b64:
        print("No server audio received. Using local TTS.")

        speak_local(
            result.get("speech_reply", "")
        )
        return

    if audio_format not in ("wav", "mp3"):
        print(f"Unsupported audio format: {audio_format}")
        return

    audio_path = f"/tmp/smart_service_reply.{audio_format}"

    try:
        audio_data = base64.b64decode(audio_b64)

        with open(audio_path, "wb") as audio_file:
            audio_file.write(audio_data)

        print("Playing server reply through JBL...")

        playback = subprocess.run(
            [
                "ffplay",
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "quiet",
                audio_path
            ],
            check=False
        )

        if playback.returncode != 0:
            print(
                f"Playback failed with code: "
                f"{playback.returncode}"
            )

    except Exception as error:
        print(f"Audio playback failed: {error}")

    finally:
        if os.path.exists(audio_path):
            os.remove(audio_path)
            
def speak_local(text):
    """Generate speech locally and play it through the default speaker."""

    if not text:
        return

    raw_audio_path = "/tmp/smartservice_reply_raw.wav"
    loud_audio_path = "/tmp/smartservice_reply_loud.wav"

    try:
        if piper_voice is not None:
            print("Generating speech with cached Piper voice...")
            with wave.open(raw_audio_path, "wb") as wav_file:
                piper_voice.synthesize_wav(text, wav_file)
            voice_engine = "Piper"
        elif os.path.isfile(PIPER_PATH) and os.path.isfile(PIPER_MODEL):
            print("Generating speech with Piper...")
            subprocess.run(
                [PIPER_PATH, "--model", PIPER_MODEL,
                 "--output_file", raw_audio_path],
                input=text, text=True, check=True
            )
            voice_engine = "Piper"
        elif shutil.which("espeak-ng"):
            print("Piper is not installed. Generating speech with espeak-ng...")
            subprocess.run(
                ["espeak-ng", "-v", "en-us", "-s", "155",
                 "-w", raw_audio_path, text],
                check=True
            )
            voice_engine = "espeak-ng"
        else:
            print("No local TTS engine found. Install espeak-ng.")
            show_lcd("Reply received", text[:16])
            return

        print("Increasing speech volume...")

        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel", "quiet",
                "-i", raw_audio_path,
                "-filter:a", f"volume={PIPER_VOLUME}",
                loud_audio_path
            ],
            check=True
        )

        print(f"Playing {voice_engine} reply through default speaker...")

        subprocess.run(
            [
                "pw-play",
                loud_audio_path
            ],
            check=False
        )

    except FileNotFoundError as error:
        print(f"Local audio program not found: {error}")

    except subprocess.CalledProcessError as error:
        print(f"Local speech command failed: {error}")

    except Exception as error:
        print(f"Local TTS failed: {error}")

    finally:
        for audio_path in (
            raw_audio_path,
            loud_audio_path
        ):
            if os.path.exists(audio_path):
                os.remove(audio_path)
    
# =========================================================
# Backend request
# =========================================================

def send_request(transcript, confidence, session_id, turn):
    payload = {
        "session_id": session_id,
        "turn": turn,
        "device_id": DEVICE_ID,
        "location": DEVICE_LOCATION,
        "transcript": transcript,
        "stt_confidence": round(confidence, 2),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "prefer_local_tts": True,
    }

    print("Sending request:")
    print(json.dumps(payload, indent=2))

    show_lcd(
        "Processing...",
        "Please wait"
    )

    try:
        request_started = time.monotonic()
        headers = {}
        if DEVICE_KEY:
            headers["X-Device-Key"] = DEVICE_KEY
        response = requests.post(
            API_URL,
            json=payload,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS
        )

        response.raise_for_status()
        result = response.json()
        print(f"Backend round trip: {time.monotonic() - request_started:.2f}s")

        print("Server response:")
        print(f"State: {result.get('state')}")
        print(f"Reply: {result.get('speech_reply')}")

        execute_actions(
            result.get("device_actions", [])
        )
        play_server_audio(result)
        if result.get("listen_again"):
            show_lcd(
                "More details",
                "Press TALK"
            )

        return result

    except requests.RequestException as error:
        print(f"Request failed: {error}")

        red_led.on()
        beep()

        show_lcd(
            "Request failed",
            "Check server"
        )

        time.sleep(2)
        red_led.off()

        return None


# =========================================================
# Talk button events
# =========================================================

def button_pressed():
    """Start recording when the button is pressed."""

    global processing
    global audio_chunks

    if processing:
        return

    red_led.off()
    green_led.on()

    show_lcd(
        "Getting ready...",
        "Wait for beep"
    )

    # The Bluetooth stream is kept warm between presses, so this beep is prompt.
    start_recording()
    beep()
    time.sleep(0.1)
    audio_chunks = []

    print("Listening... Speak now.")
    show_lcd(
        "Listening...",
        "Speak now"
    )


def button_released():
    """Stop recording, transcribe and send the request."""

    global processing
    global current_session_id
    global current_turn

    if processing or not recording:
        return

    processing = True

    try:
        print("Recording stopped.")

        green_led.off()
        stop_recording()

        show_lcd(
            "Recognizing...",
            "Please wait"
        )

        transcript, confidence = transcribe_audio()
        transcript = clean_transcript(transcript)

        print(f"Raw transcript: {transcript}")
        print(f"Vosk confidence: {confidence:.2f}")

        print(f"Clean transcript: {transcript}")

        if not transcript or transcript == "[unk]":
            red_led.on()
            beep_double()

            show_lcd(
                "Not understood",
                "Try again"
            )

            time.sleep(2)
            red_led.off()
            return

        if current_session_id is None:
            current_session_id = "s-" + uuid.uuid4().hex[:6]
            current_turn = 1

        # Alert locally first; still send the request so staff are notified.
        if is_local_safety_alert(transcript):
            print("Local safety alert detected; notifying backend now.")
            show_lcd("URGENT ALERT", "Notifying staff")
            red_led.on()
            urgent_pattern(700)
            red_led.off()

        result = send_request(
            transcript,
            confidence,
            current_session_id,
            current_turn
        )

        if result and result.get("listen_again"):
            current_turn += 1

            print(
                f"Waiting for follow-up: "
                f"session={current_session_id}, "
                f"turn={current_turn}"
            )

            show_lcd(
                "More details",
                "Press TALK"
            )

        else:
            current_session_id = None
            current_turn = 1

    finally:
        processing = False
# =========================================================
# Startup
# =========================================================

talk_button.when_pressed = button_pressed
talk_button.when_released = button_released

green_led.off()
red_led.off()
buzzer.off()

show_lcd(
    "Smart Service",
    "System Ready"
)

# Keep Bluetooth HFP capture active so a button press does not wait for profile
# negotiation. Discard startup audio after the configured one-time warm-up.
start_recording()
time.sleep(MICROPHONE_WARMUP_SECONDS)
audio_chunks = []
recording = False

print()
print("Voice client started.")
print(f"Backend: {API_URL}")
print(f"Device authentication: {'configured' if DEVICE_KEY else 'not configured'}")
print(f"Microphone device: {MICROPHONE_DEVICE if MICROPHONE_DEVICE is not None else 'system default'}")
print(f"Microphone warm-up: {MICROPHONE_WARMUP_SECONDS}s")
print(f"Vosk model: {MODEL_PATH}")
print(f"Request timeout: {REQUEST_TIMEOUT_SECONDS}s")
print("Hold TALK, speak, then release.")
print("Press Ctrl+C to stop.")

try:
    while True:
        time.sleep(1)

except KeyboardInterrupt:
    print("\nStopping voice client...")

finally:
    stop_recording()
    close_audio_stream()

    green_led.off()
    red_led.off()
    buzzer.off()

    lcd.clear()
    lcd.close(clear=True)

