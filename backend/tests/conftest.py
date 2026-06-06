"""Test bootstrap for the NLU corpus.

The goal of this test suite is to prove the **rule-based fallback** in
`app.nlp.pipeline.understand` behaves correctly when the LLM is unavailable
(rate-limited, network down, missing API keys). To make the suite
deterministic and reproducible we must:

1. Force every LLM provider to be disabled by emptying the relevant API key
   environment variables BEFORE `app.config.get_settings()` is first called.
2. Skip the embedding model backfill on import so tests start fast (no ONNX
   download / disk thrash).
3. Point the SQLite store at an isolated temp directory so a developer
   running pytest never corrupts their dev `omni.db`.
4. Reset Pydantic Settings' lru_cache after we mutate the environment,
   otherwise stale credentials from `backend/.env` would still leak through.

Everything here runs at collection time — no fixtures are needed by the
corpus tests themselves; they just import `app.nlp.pipeline.understand`
and call it directly.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def _bootstrap_test_env() -> None:
    # Repo layout: backend/tests/conftest.py — the importable package root
    # is the parent of `app/`, i.e. `backend/`.
    backend_root = Path(__file__).resolve().parent.parent
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))

    # Empty out LLM credentials so `_enabled_providers()` returns []. We set
    # them explicitly (rather than relying on absence) because the dev .env
    # may already define real keys. CRITICAL: clear the *numbered key pool*
    # too (GROQ_API_KEY_1..N / GEMINI_API_KEY_1..N) — `_collect_keys` reads
    # those straight from os.environ, and a dev .env with a 36-key pool
    # would otherwise leave the LLM live during tests, making every
    # response-phrasing assertion (smalltalk/insights fallback copy)
    # non-deterministic.
    # Set to "" rather than pop: ``app.config`` calls
    # ``load_dotenv(override=False)`` on import, which RE-ADDS any key that
    # is absent from os.environ — popping would let the real .env pool leak
    # back in. An existing empty string is not "absent", so override=False
    # leaves it alone and ``_collect_keys`` skips it (it drops empties).
    os.environ["GROQ_API_KEY"] = ""
    os.environ["GEMINI_API_KEY"] = ""
    for _prefix in ("GROQ_API_KEY", "GEMINI_API_KEY"):
        for _n in range(1, 200):
            os.environ[f"{_prefix}_{_n}"] = ""

    # Skip the fastembed backfill — irrelevant to NLU rule testing and
    # downloads ~120MB on first run.
    os.environ["OMNI_SKIP_EMBED_BACKFILL"] = "1"

    # Default to the in-memory session backend so we don't accidentally
    # talk to a Redis instance configured in the dev shell. Tests that
    # need to exercise the Redis wire path construct
    # ``FakeRedisSessionStore`` directly.
    os.environ.setdefault("OMNI_SESSION_BACKEND", "memory")

    # Isolate the runtime SQLite database so tests can't clobber the dev
    # seed. The `bootstrap` module honours BANKING_DATA_DIR for both the
    # JSON seed location and the omni.db file.
    tmp_data = Path(tempfile.mkdtemp(prefix="omni-nlu-tests-"))
    os.environ.setdefault("BANKING_DATA_DIR", str(tmp_data))

    # Make sure Pydantic Settings re-reads the environment we just set,
    # otherwise the (potentially cached) instance from a previous import
    # would still hold the real keys from `backend/.env`.
    try:
        from app.config import get_settings  # noqa: WPS433 — intentional late import

        get_settings.cache_clear()
    except Exception:  # config not importable yet — that's fine, it will
        # see the cleaned env when it does import.
        pass


_bootstrap_test_env()
