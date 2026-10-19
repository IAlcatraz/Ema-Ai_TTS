"""Groq Whisper transcription wrapper.

Single public function: `transcribe_audio_groq(aud_path, prompt)` — POSTs
the file to Groq's audio.transcriptions endpoint and returns the text.

The Groq client is created lazily on first call so `import` is cheap and
doesn't require GROQ_API_KEY at import time (only at call time).
"""

from __future__ import annotations

import os
from typing import Optional

from dotenv import load_dotenv
from groq import Groq

from server.config import ASR_PROMPT

load_dotenv()

_client: Optional[Groq] = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        key = os.getenv("GROQ_API_KEY")
        if not key:
            raise EnvironmentError(
                "GROQ_API_KEY not set — required for server.asr.transcribe"
            )
        _client = Groq(api_key=key)
    return _client


def transcribe_audio_groq(
    aud_path: str = "conversation.wav",
    prompt: str = ASR_PROMPT,   # character-aware (server/config.py)
) -> str:
    """Transcribe a WAV via Groq Whisper-large-v3. Returns the spoken text."""
    client = _get_client()
    with open(aud_path, "rb") as file:
        transcription = client.audio.transcriptions.create(
            file=(aud_path, file.read()),
            model="whisper-large-v3",
            response_format="verbose_json",
            prompt=prompt,
        )
    text = transcription.text
    print(f"[asr.transcribe] {text!r}")
    return text


if __name__ == "__main__":
    from server.asr.recorder import record_on_speech

    out = "/tmp/asr_test.wav"
    record_on_speech(out, samplerate=16000, channels=1,
                     silence_threshold=0.02, silence_duration=0.25)
    print("[asr.transcribe.__main__] transcribing...")
    print(transcribe_audio_groq(out))
