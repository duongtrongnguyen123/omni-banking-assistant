"""Smoke-level coverage for /api/demo/* and the telemetry overlay path.

The recorder, replayer, and telemetry switch all sit OUTSIDE the
default chat flow, so the production behaviour (`POST /api/chat` with
no query) must be byte-identical whether or not these features exist.
That invariant is asserted at the bottom of this file.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module", autouse=True)
def _seed_demo_user():
    """Copy the canonical JSON seed into the conftest's isolated tmp
    data dir so the demo user exists for /api/chat tests."""
    data_dir = Path(os.environ["BANKING_DATA_DIR"])
    src = Path(__file__).resolve().parent.parent / "app" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in ("users.json", "contacts.json", "transactions.json", "schedules.json"):
        target = data_dir / name
        if not target.exists() and (src / name).exists():
            shutil.copyfile(src / name, target)
    # Make sure the SQLite file is fresh so bootstrap re-runs against
    # the JSON we just copied. CRITICAL: also drop the in-process Store
    # / connection cache — otherwise a prior test module's cached
    # connection still points at the deleted file and reads come back
    # empty (users KeyError). The Store singleton lazily rebuilds on
    # next ``get_store()`` and triggers a fresh bootstrap.
    db_file = data_dir / "omni.db"
    if db_file.exists():
        db_file.unlink()
    from app.db.connection import reset_connection
    import app.store as _store_mod

    reset_connection()
    _store_mod._store = None


def _client():
    # Import inside the function so conftest can set env (skip embeddings,
    # blank LLM keys) before the FastAPI app instantiates its singletons.
    from app.main import app

    return TestClient(app)


def test_record_start_stop_roundtrip() -> None:
    c = _client()
    r = c.post("/api/demo/record/start")
    assert r.status_code == 200, r.text
    assert r.json()["recording"] is True

    # Drive a real chat turn — the recorder hook must capture it.
    chat = c.post("/api/chat", json={"message": "số dư"})
    assert chat.status_code == 200

    stop = c.post("/api/demo/record/stop")
    assert stop.status_code == 200, stop.text
    body = stop.json()
    assert body["recording"] is False
    assert body["turns"] >= 1
    assert body["jsonl"], "JSONL payload should be non-empty"

    # JSONL is parseable line-by-line.
    for line in body["jsonl"].splitlines():
        obj = json.loads(line)
        assert "user" in obj and "omni" in obj and "ts" in obj


def test_stop_without_start_is_safe() -> None:
    c = _client()
    # Make sure no recording is dangling from a previous test.
    c.post("/api/demo/record/stop")
    r = c.post("/api/demo/record/stop")
    assert r.status_code == 200
    assert r.json() == {
        "recording": False,
        "turns": 0,
        "duration_ms": 0,
        "jsonl": "",
        "script": [],
    }


def test_replay_drives_messages_through_handle_message() -> None:
    c = _client()
    script = [
        {"user": "chào omni"},
        {"user": "số dư"},
    ]
    r = c.post("/api/demo/replay", json={"script": script, "cadence_ms": 0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["played"] == 2
    assert len(body["transcript"]) == 2
    intents = {t["intent"] for t in body["transcript"]}
    # Whatever NLU classifies, at minimum balance must surface for the
    # second turn — rule extractor catches "số dư" deterministically.
    assert "balance" in intents


def test_telemetry_only_populated_when_dev_flag_set() -> None:
    c = _client()
    plain = c.post("/api/chat", json={"message": "số dư"})
    assert plain.status_code == 200
    assert plain.json().get("telemetry") is None, (
        "default chat call must NOT leak telemetry to clients"
    )

    dev = c.post("/api/chat?dev=1", json={"message": "số dư"})
    assert dev.status_code == 200
    tel = dev.json().get("telemetry")
    assert tel is not None
    assert "nlu_latency_ms" in tel
    assert tel["nlu_source"] in ("llm", "rule")
    assert "total_latency_ms" in tel
    assert "safety_flags" in tel


def test_health_includes_offline_flag_and_sha() -> None:
    c = _client()
    r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "git_sha" in body
    assert "offline_demo" in body


def test_offline_demo_setting_skips_llm_providers(monkeypatch) -> None:
    """Smoke: with OMNI_OFFLINE_DEMO=1, _enabled_providers must return [].

    This is the contract that gives offline-mode its "no outbound
    network" guarantee — every other knob (skip-embeddings, no schedule
    ticker) hangs off it.
    """
    monkeypatch.setenv("OMNI_OFFLINE_DEMO", "1")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fake")  # would otherwise enable Groq
    from app.config import get_settings
    from app.nlp import llm

    get_settings.cache_clear()
    try:
        assert get_settings().offline_demo is True
        assert llm._enabled_providers() == []
    finally:
        get_settings.cache_clear()
