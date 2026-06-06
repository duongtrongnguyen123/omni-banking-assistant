"""Speech-to-text providers.

By default (auto) this prefers Groq when GROQ_API_KEY is set, then OpenAI when
OPENAI_API_KEY is set, otherwise it falls back to local faster-whisper. Set
SPEECH_STT_PROVIDER=groq, openai or local to force one provider.

Groq uses the OpenAI-compatible audio API with a custom base_url, so it reuses
the openai SDK already in requirements.
"""

from __future__ import annotations

import logging
from threading import Lock
from typing import Any, Optional

from ..config import get_settings

logger = logging.getLogger(__name__)

MODEL_SIZE = "base"
DEVICE = "cpu"
COMPUTE_TYPE = "int8"  # int8 is fast on CPU with minor accuracy loss
LANGUAGE = "vi"

_model: Optional[Any] = None
_lock = Lock()


def get_model() -> Any:
    global _model
    with _lock:
        if _model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise RuntimeError(
                    "faster-whisper is not installed. Set "
                    "SPEECH_STT_PROVIDER=openai and OPENAI_API_KEY, or install "
                    "backend requirements."
                ) from exc
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


def transcribe_local(path: str) -> str:
    """Transcribe an audio file into Vietnamese text with faster-whisper."""
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


def transcribe_openai(path: str) -> str:
    """Transcribe an audio file into Vietnamese text with OpenAI Audio API."""
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required for OpenAI speech STT.")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "openai package is not installed. Run pip install -r requirements.txt."
        ) from exc

    client = OpenAI(api_key=settings.openai_api_key)
    with open(path, "rb") as audio_file:
        transcription = client.audio.transcriptions.create(
            model=settings.openai_stt_model,
            file=audio_file,
            language=LANGUAGE,
            prompt=(
                "Audio tiếng Việt trong ứng dụng ngân hàng. "
                "Giữ đúng tên người, số tiền, số tài khoản và mốc thời gian."
            ),
        )
    return getattr(transcription, "text", str(transcription)).strip()


def transcribe_groq(path: str) -> str:
    """Transcribe an audio file into Vietnamese text with Groq Whisper.

    Groq exposes an OpenAI-compatible /audio/transcriptions endpoint, so we
    reuse the openai SDK pointed at Groq's base_url.
    """
    settings = get_settings()
    if not settings.groq_api_key:
        raise RuntimeError("GROQ_API_KEY is required for Groq speech STT.")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "openai package is not installed. Run pip install -r requirements.txt."
        ) from exc

    client = OpenAI(
        api_key=settings.groq_api_key, base_url=settings.groq_base_url
    )
    with open(path, "rb") as audio_file:
        transcription = client.audio.transcriptions.create(
            model=settings.groq_stt_model,
            file=audio_file,
            language=LANGUAGE,
            prompt=(
                "Audio tiếng Việt trong ứng dụng ngân hàng. "
                "Giữ đúng tên người, số tiền, số tài khoản và mốc thời gian."
            ),
        )
    return getattr(transcription, "text", str(transcription)).strip()


def transcribe_file(path: str) -> str:
    """Transcribe an audio file into Vietnamese text."""
    settings = get_settings()
    provider = settings.speech_stt_provider.lower().strip()

    if provider == "auto":
        if settings.groq_api_key:
            provider = "groq"
        elif settings.openai_api_key:
            provider = "openai"
        else:
            provider = "local"

    if provider == "groq":
        return transcribe_groq(path)
    if provider == "openai":
        return transcribe_openai(path)
    if provider in {"local", "faster-whisper", "whisper"}:
        return transcribe_local(path)

    raise RuntimeError(
        "SPEECH_STT_PROVIDER must be one of: auto, groq, openai, local."
    )
