"""Gemini text embeddings + cosine similarity helpers.

Used by the RAG-flavoured fallback in ``context/alias.py`` (semantic contact
lookup) and — when wired — by transaction history fuzzy search. Embeddings
are stored as float32 BLOBs in the SQLite ``contacts`` / ``transactions``
table so we only pay the API cost once per row.

If ``GEMINI_API_KEY`` isn't set, every function here returns ``None`` and
callers skip the semantic step transparently — the rule-based path keeps
working.
"""

from __future__ import annotations

import json
import logging
import struct
import urllib.error
import urllib.request
from typing import Optional

from ..config import get_settings

log = logging.getLogger("omni.nlp.embed")

_EMBED_MODEL = "gemini-embedding-001"
_EMBED_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{_EMBED_MODEL}:embedContent"
)


def embed(text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> Optional[list[float]]:
    """Call Gemini text-embedding-004. Returns the float vector or None on
    any failure (no key, 429, network error). Caller must handle None.

    ``task_type`` should be ``RETRIEVAL_DOCUMENT`` when embedding stored
    items and ``RETRIEVAL_QUERY`` for the user's incoming query — Gemini
    tweaks the embedding direction for each.
    """
    settings = get_settings()
    if not settings.gemini_api_key or not text.strip():
        return None

    body = {
        "model": f"models/{_EMBED_MODEL}",
        "content": {"parts": [{"text": text}]},
        "taskType": task_type,
    }
    # Gemini's embedContent endpoint authenticates via ?key= query param,
    # not Authorization Bearer (only the OpenAI-compat chat endpoint does).
    url = f"{_EMBED_URL}?key={settings.gemini_api_key}"
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
            err_body = e.read().decode("utf-8", "ignore")[:200]
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
# Encode / decode for SQLite BLOB storage
# ---------------------------------------------------------------------------


def pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


# ---------------------------------------------------------------------------
# Cosine
# ---------------------------------------------------------------------------


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
