"""Speech endpoints: voice-text (redact for TTS) and tts (audio stream).

Frontend usage:
1. POST /api/speech/voice-text with the OmniResponse JSON to get the spoken,
   redacted Vietnamese text (`{"text": "..."}`).
2. POST /api/speech/tts with `{text, voice?}` to stream MP3 audio."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..routes.deps import current_user
from .redact import to_voice_text
from .tts import DEFAULT_VOICE, normalize_voice, synthesize

router = APIRouter(prefix="/api/speech", tags=["speech"])


class VoiceTextRequest(BaseModel):
    # Accept arbitrary OmniResponse-shaped dict so the schema stays loose
    # (the frontend forwards whatever /api/chat returned).
    response: dict[str, Any]


class VoiceTextResponse(BaseModel):
    text: str


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    voice: Literal["vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural"] = DEFAULT_VOICE


@router.post("/voice-text", response_model=VoiceTextResponse)
def voice_text(
    req: VoiceTextRequest,
    user_id: str = Depends(current_user),
) -> VoiceTextResponse:
    """Convert an OmniResponse into safely-spoken Vietnamese text."""
    return VoiceTextResponse(text=to_voice_text(req.response))


@router.post("/tts")
async def tts(
    req: TTSRequest,
    user_id: str = Depends(current_user),
) -> StreamingResponse:
    """Stream MP3 audio for the given text."""
    voice = normalize_voice(req.voice)
    try:
        return StreamingResponse(
            synthesize(req.text, voice),
            media_type="audio/mpeg",
        )
    except Exception as exc:  # network/TTS service issue
        raise HTTPException(status_code=503, detail=f"TTS unavailable: {exc}")
