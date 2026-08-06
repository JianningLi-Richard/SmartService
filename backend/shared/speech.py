"""Azure AI Speech text-to-speech, embedded in the same HTTPS response.

Contract: on any failure or timeout we return ("", "mp3") rather than raising.
An empty audio_b64 tells the Pi to speak speech_reply with its local Piper voice,
so a slow speech service degrades the demo instead of breaking it.
"""

import base64
import logging
import time
import xml.sax.saxutils as saxutils

from . import config

log = logging.getLogger(__name__)

# 16 kHz mono MP3 -- roughly 20 KB for a five-second utterance.
_OUTPUT_FORMAT = "audio-16khz-32kbitrate-mono-mp3"


def _ssml(text, voice):
    return ('<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            'xml:lang="en-CA"><voice name="%s"><prosody rate="+5%%">%s'
            '</prosody></voice></speak>' % (voice, saxutils.escape(text)))


def synthesize(text):
    """Return (audio_b64, audio_format, elapsed_ms). Empty audio means fall back."""
    if not text:
        return "", "mp3", 0
    if not config.USE_SPEECH:
        return "", "mp3", 0

    started = time.time()
    try:
        import requests
        url = ("https://%s.tts.speech.microsoft.com/cognitiveservices/v1"
               % config.SPEECH_REGION)
        resp = requests.post(
            url,
            headers={
                "Ocp-Apim-Subscription-Key": config.SPEECH_KEY,
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": _OUTPUT_FORMAT,
                "User-Agent": "smart-service-panel",
            },
            data=_ssml(text, config.SPEECH_VOICE).encode("utf-8"),
            timeout=config.SPEECH_TIMEOUT_MS / 1000.0,
        )
        elapsed = int((time.time() - started) * 1000)
        if resp.status_code != 200:
            log.warning("tts http %s in %dms -- device will use local voice",
                        resp.status_code, elapsed)
            return "", "mp3", elapsed
        return base64.b64encode(resp.content).decode("ascii"), "mp3", elapsed
    except Exception as exc:
        elapsed = int((time.time() - started) * 1000)
        log.warning("tts failed after %dms (%s) -- device will use local voice",
                    elapsed, exc)
        return "", "mp3", elapsed
