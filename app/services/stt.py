from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO, Union

from groq import Groq

from app.config import settings

_client: Groq | None = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=settings.groq_api_key)
    return _client


def transcribe_audio(audio: Union[str, Path, BinaryIO]) -> str:
    """Transcribes a voice note to text via Groq's Whisper Large v3 Turbo
    endpoint. `audio` is either a path to a local audio file or an
    already-open binary file handle - eventually a WhatsApp voice note
    downloaded to a temp file/buffer will be passed straight through here,
    then the returned text flows into parser.parse_message()."""
    if isinstance(audio, (str, os.PathLike)):
        with open(audio, "rb") as f:
            return _transcribe(f, Path(audio).name)
    return _transcribe(audio, getattr(audio, "name", "audio"))


def _transcribe(file: BinaryIO, filename: str) -> str:
    result = _get_client().audio.transcriptions.create(
        model="whisper-large-v3-turbo",
        file=(filename, file.read()),
        response_format="text",
    )
    # response_format="text" returns the transcript directly as a str, not
    # a Transcription object with a `.text` attribute (verified empirically
    # against the installed groq==1.6.0 client, not assumed from docs).
    return result if isinstance(result, str) else result.text
