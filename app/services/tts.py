from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

import httpx

from app.config import settings

TTS_ENDPOINT = "https://texttospeech.googleapis.com/v1/text:synthesize"

# Neural2 is the warmest voice tier Google Cloud TTS offers (Studio voices
# exist but are priced much higher and weren't part of the original cost
# comparison in system-design.md) - still a deliberate step down from
# ElevenLabs, per that doc's honest tradeoff note.
DEFAULT_VOICE = "en-US-Neural2-F"
DEFAULT_LANGUAGE_CODE = "en-US"


def synthesize_speech(
    text: str,
    output_path: Optional[str | Path] = None,
    voice_name: str = DEFAULT_VOICE,
    language_code: str = DEFAULT_LANGUAGE_CODE,
    speaking_rate: float = 1.0,
) -> bytes:
    """Generates speech audio (MP3) for `text` via Google Cloud TTS's REST
    API, authenticated with a plain restricted API key (no service account
    file) per the earlier design decision. Returns the raw MP3 bytes, and
    also writes them to `output_path` if given. This is the output side for
    briefings, nudges, and check-ins - kept isolated so swapping to
    ElevenLabs later is a contained change (see system-design.md)."""
    response = httpx.post(
        TTS_ENDPOINT,
        params={"key": settings.google_tts_api_key},
        json={
            "input": {"text": text},
            "voice": {"languageCode": language_code, "name": voice_name},
            "audioConfig": {"audioEncoding": "MP3", "speakingRate": speaking_rate},
        },
        timeout=30.0,
    )
    response.raise_for_status()
    audio_bytes = base64.b64decode(response.json()["audioContent"])

    if output_path is not None:
        Path(output_path).write_bytes(audio_bytes)

    return audio_bytes
