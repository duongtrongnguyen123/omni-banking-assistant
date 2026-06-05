"""Embedding provider chain.

Order tried by ``embed()``:
  1. **fastembed** (local, free, offline) — primary path. Uses
     ``sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`` —
     384-dim, 220MB on disk, ~10-15ms/text on CPU after warmup.
  2. **Gemini ``gemini-embedding-001``** (remote) — fallback for the day
     a permitted key is configured. Returns 403 today because the
     ``embedContent`` permission isn't granted on the project of the key
     in `.env`.

The fastembed model is lazy-loaded — the first call pays a ~2s init cost,
subsequent calls reuse the cached encoder. We pre-warm it from the FastAPI
``@startup`` hook so user-visible latency stays under 50ms.
"""

from __future__ import annotations

import json
import logging
import struct
import threading
import urllib.error
import urllib.request
from typing import Optional

from ..config import get_settings

log = logging.getLogger("omni.nlp.embed")

_FASTEMBED_MODEL_NAME = (
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)
_FASTEMBED_DIM = 384

_GEMINI_MODEL = "gemini-embedding-001"
_GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{_GEMINI_MODEL}:embedContent"
)

# Lazy-loaded fastembed singleton.
_FASTEMBED_LOCK = threading.Lock()
_FASTEMBED_MODEL = None
_FASTEMBED_AVAILABLE: Optional[bool] = None  # None = not probed yet


def _try_load_fastembed():
    global _FASTEMBED_MODEL, _FASTEMBED_AVAILABLE
    if _FASTEMBED_AVAILABLE is False:
        return None
    if _FASTEMBED_MODEL is not None:
        return _FASTEMBED_MODEL
    with _FASTEMBED_LOCK:
        if _FASTEMBED_MODEL is not None:
            return _FASTEMBED_MODEL
        try:
            from fastembed import TextEmbedding  # type: ignore

            log.info("Loading fastembed model %s …", _FASTEMBED_MODEL_NAME)
            _FASTEMBED_MODEL = TextEmbedding(model_name=_FASTEMBED_MODEL_NAME)
            _FASTEMBED_AVAILABLE = True
            log.info("fastembed model ready (dim=%s)", _FASTEMBED_DIM)
            return _FASTEMBED_MODEL
        except Exception as e:
            log.warning("fastembed unavailable: %s", e)
            _FASTEMBED_AVAILABLE = False
            return None


def warmup() -> bool:
    """Force-load the local model. Safe to call from startup."""
    return _try_load_fastembed() is not None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def embed(text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> Optional[list[float]]:
    """Return a vector for ``text`` or ``None`` if no provider is available.

    ``task_type`` is preserved for API parity with Gemini's embedContent
    body — fastembed's MiniLM doesn't need a task-prefix, but a future E5
    model would.
    """
    if not text.strip():
        return None

    vec = _fastembed_one(text)
    if vec is not None:
        return vec
    return _gemini_embed(text, task_type)


def embed_many(texts: list[str]) -> list[Optional[list[float]]]:
    """Batched embed. Much faster than calling ``embed`` in a loop because
    fastembed amortises the model overhead across the batch."""
    if not texts:
        return []

    model = _try_load_fastembed()
    if model is not None:
        try:
            return [list(v) for v in model.embed(texts)]
        except Exception as e:
            log.warning("fastembed batch failed: %s", e)
    # Fall back per-item via Gemini (slower; only used if local unavailable)
    return [embed(t) for t in texts]


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


def _fastembed_one(text: str) -> Optional[list[float]]:
    model = _try_load_fastembed()
    if model is None:
        return None
    try:
        vec = next(iter(model.embed([text])))
        return list(vec)
    except Exception as e:
        log.warning("fastembed encode failed: %s", e)
        return None


def _gemini_embed(text: str, task_type: str) -> Optional[list[float]]:
    settings = get_settings()
    if not settings.gemini_api_key:
        return None
    body = {
        "model": f"models/{_GEMINI_MODEL}",
        "content": {"parts": [{"text": text}]},
        "taskType": task_type,
    }
    url = f"{_GEMINI_URL}?key={settings.gemini_api_key}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "omni-embed/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload["embedding"]["values"]
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", "ignore")[:160]
        except Exception:
            err_body = ""
        log.warning("Gemini embed HTTP %s: %s", e.code, err_body)
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("Gemini embed network error: %s", e)
        return None
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        log.warning("Gemini embed parse error: %s", e)
        return None


# ---------------------------------------------------------------------------
# BLOB pack/unpack + cosine
# ---------------------------------------------------------------------------


def pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))
