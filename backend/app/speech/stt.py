"""Speech-to-text via faster-whisper.

Lazy-loads the model on first use (download ~75MB on first run, cached
in ~/.cache/huggingface). Subsequent calls reuse the in-memory model."""

from __future__ import annotations

import logging
from threading import Lock
from typing import Optional

from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)

MODEL_SIZE = "base"
DEVICE = "cpu"
COMPUTE_TYPE = "int8"  # int8 is fast on CPU with minor accuracy loss
LANGUAGE = "vi"

_model: Optional[WhisperModel] = None
_lock = Lock()


def get_model() -> WhisperModel:
    global _model
    with _lock:
        if _model is None:
            logger.info(
                "Loading faster-whisper model size=%s device=%s compute=%s",
                MODEL_SIZE,
                DEVICE,
                COMPUTE_TYPE,
            )
            _model = WhisperModel(
                MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE
            )
    return _model


def transcribe_file(path: str) -> str:
    """Transcribe an audio file into Vietnamese text."""
    model = get_model()
    segments, _info = model.transcribe(
        path,
        language=LANGUAGE,
        beam_size=5,
        vad_filter=True,  # skip silence at start/end → faster
        vad_parameters={"min_silence_duration_ms": 300},
    )
    text = "".join(seg.text for seg in segments).strip()
    return text
