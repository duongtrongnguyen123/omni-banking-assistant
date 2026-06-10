"""OpenRouter as the third-tier LLM fallback.

Pins the provider-pool wiring so a future refactor can't silently drop
OpenRouter and leave the demo with no fallback when Groq + Gemini both
hit their daily quotas (the "load mãi không trả lời" failure mode the
user hit on day-of-demo). Two cheap checks:

1. Pool composition — when an OPENROUTER_API_KEY is present, the chain
   ends with at least one ``openrouter*`` provider.
2. Wire shape — the OpenRouter provider carries the leaderboard
   attribution headers OpenRouter expects (HTTP-Referer + X-Title).
   They're cosmetic but easy to drop accidentally, and showing up in
   the OpenRouter dashboard is one of the few tangible benefits of the
   integration.
"""

from __future__ import annotations

from app.config import get_settings


def _reset_settings() -> None:
    get_settings.cache_clear()


def test_openrouter_enters_pool_when_key_present(monkeypatch) -> None:
    monkeypatch.setenv("OMNI_OFFLINE_DEMO", "0")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-1")
    _reset_settings()
    from app.nlp.llm import _enabled_providers

    pool = _enabled_providers()
    names = [p.name for p in pool]
    assert any(n.startswith("openrouter") for n in names), names

    # Comes AFTER Groq + Gemini in the chain — fallback semantics.
    or_idx = next(i for i, p in enumerate(pool) if p.name.startswith("openrouter"))
    groq_idxs = [i for i, p in enumerate(pool) if p.name.startswith("groq")]
    gemini_idxs = [i for i, p in enumerate(pool) if p.name.startswith("gemini")]
    if groq_idxs:
        assert or_idx > max(groq_idxs)
    if gemini_idxs:
        assert or_idx > max(gemini_idxs)


def test_openrouter_provider_carries_leaderboard_headers(monkeypatch) -> None:
    monkeypatch.setenv("OMNI_OFFLINE_DEMO", "0")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-1")
    _reset_settings()
    from app.nlp.llm import _enabled_providers

    pool = _enabled_providers()
    or_provider = next(p for p in pool if p.name.startswith("openrouter"))
    assert or_provider.url == "https://openrouter.ai/api/v1/chat/completions"
    assert "HTTP-Referer" in or_provider.extra_headers
    assert "X-Title" in or_provider.extra_headers
    # Free tier model — the constant slug we pin in config. If the free
    # roster rotates and this model disappears, this assertion catches
    # it before the demo does.
    assert ":free" in or_provider.model


def test_openrouter_pool_grows_with_numbered_keys(monkeypatch) -> None:
    """``OPENROUTER_API_KEY_1..N`` join the pool like the Groq pool does."""
    monkeypatch.setenv("OMNI_OFFLINE_DEMO", "0")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY_1", "sk-or-test-a")
    monkeypatch.setenv("OPENROUTER_API_KEY_2", "sk-or-test-b")
    monkeypatch.setenv("OPENROUTER_API_KEY_3", "sk-or-test-c")
    _reset_settings()
    from app.nlp.llm import _enabled_providers

    pool = _enabled_providers()
    or_entries = [p for p in pool if p.name.startswith("openrouter")]
    assert len(or_entries) == 3
    assert {p.name for p in or_entries} == {"openrouter#1", "openrouter#2", "openrouter#3"}
    # All three carry the same model + headers — each is just a different key.
    for p in or_entries:
        assert p.model == or_entries[0].model
        assert p.extra_headers == or_entries[0].extra_headers


def test_offline_demo_still_skips_openrouter(monkeypatch) -> None:
    """The offline-demo flag wins over OPENROUTER_API_KEY — no outbound
    calls allowed when the operator has explicitly turned the LLM tier
    off (e.g. demo laptop without wifi)."""
    monkeypatch.setenv("OMNI_OFFLINE_DEMO", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-1")
    _reset_settings()
    from app.nlp.llm import _enabled_providers

    assert _enabled_providers() == []
