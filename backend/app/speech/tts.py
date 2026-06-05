"""edge-tts wrapper for Vietnamese text-to-speech.

Uses Microsoft Edge's free Read-Aloud endpoint. No API key required.
Voices chosen for natural Vietnamese pronunciation."""

from __future__ import annotations

from typing import AsyncIterator

import edge_tts

DEFAULT_VOICE = "vi-VN-HoaiMyNeural"  # Female, natural
MALE_VOICE = "vi-VN-NamMinhNeural"

ALLOWED_VOICES = {DEFAULT_VOICE, MALE_VOICE}


def normalize_voice(voice: str | None) -> str:
    if not voice or voice not in ALLOWED_VOICES:
        return DEFAULT_VOICE
    return voice


async def synthesize(text: str, voice: str = DEFAULT_VOICE) -> AsyncIterator[bytes]:
    """Stream MP3 chunks for the given text."""
    voice = normalize_voice(voice)
    communicate = edge_tts.Communicate(text, voice=voice)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            yield chunk["data"]
