"""Speech endpoint: STT via faster-whisper.

POST /api/speech/stt
  multipart/form-data with field `audio` (audio/* file)
  → {"text": "<vietnamese transcript>"}"""

from __future__ import annotations

import os
import tempfile

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from ..routes.deps import current_user
from .stt import transcribe_file

router = APIRouter(prefix="/api/speech", tags=["speech"])


class STTResponse(BaseModel):
    text: str


def _suffix_for(filename: str | None, content_type: str | None) -> str:
    if filename and "." in filename:
        return "." + filename.rsplit(".", 1)[1].lower()
    if content_type:
        if "webm" in content_type:
            return ".webm"
        if "ogg" in content_type:
            return ".ogg"
        if "mp4" in content_type or "m4a" in content_type:
            return ".m4a"
        if "wav" in content_type:
            return ".wav"
        if "mpeg" in content_type or "mp3" in content_type:
            return ".mp3"
    return ".webm"


@router.post("/stt", response_model=STTResponse)
async def stt(
    audio: UploadFile = File(...),
    user_id: str = Depends(current_user),
) -> STTResponse:
    suffix = _suffix_for(audio.filename, audio.content_type)
    data = await audio.read()
    if not data:
        raise HTTPException(status_code=400, detail="Audio rỗng.")
    if len(data) > 10 * 1024 * 1024:  # 10MB hard cap
        raise HTTPException(status_code=413, detail="Audio quá lớn (>10MB).")
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="omni-stt-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:
            text = transcribe_file(path)
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Lỗi nhận diện giọng nói: {exc}"
            )
        return STTResponse(text=text)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
