"""Tests for the /help command and structured help_sections payload.

Covers:
- /help slash command returns OmniResponse with help_sections populated
  (5 skill categories + 1 shortcuts section).
- Vietnamese phrasing ("trợ giúp", "hướng dẫn") routes to the same
  deterministic help response.
- Plain-text fallback (``text`` field) still carries the prose copy so
  TTS / replay paths and screen readers receive an intelligible message
  even when the frontend can't render the structured card.
- The payload is intent-aligned (transfer / query / recurring / budget
  / tools) so the SkillsCard sidebar widget and the HelpCard share a
  single source of truth.

The orchestrator's help branch is deterministic — no NLU, no LLM — so
these assertions are stable in the offline-demo / no-API-key path.
"""

from __future__ import annotations

import pytest

from app.services.orchestrator import (
    handle_message,
    help_sections_payload,
)


_EXPECTED_SECTION_IDS = {"transfer", "query", "recurring", "budget", "tools"}
_EXPECTED_SECTION_TITLES = {
    "Chuyển tiền",
    "Truy vấn",
    "Định kỳ",
    "Ngân sách",
    "Công cụ",
}


def test_slash_help_returns_help_sections():
    resp = handle_message("u_help_slash", "/help")
    assert resp.intent == "smalltalk"
    assert resp.text  # plain-text fallback preserved
    assert resp.help_sections is not None
    # 5 skill sections + 1 shortcuts section.
    assert len(resp.help_sections) == 6
    skill_titles = {s["title"] for s in resp.help_sections if s.get("id") != "shortcuts"}
    assert skill_titles == _EXPECTED_SECTION_TITLES


def test_vietnamese_help_phrase_routes_to_help():
    # The user typed "trợ giúp" with diacritics — same dispatch path as
    # /help, same structured payload.
    resp = handle_message("u_help_vi", "trợ giúp")
    assert resp.intent == "smalltalk"
    assert resp.help_sections is not None
    section_ids = {s.get("id") for s in resp.help_sections}
    assert _EXPECTED_SECTION_IDS.issubset(section_ids)


def test_help_sections_have_example_chips():
    # Every non-shortcut section must expose at least one (label, example)
    # pair — the SkillsCard chips render off this.
    resp = handle_message("u_help_chips", "/help")
    assert resp.help_sections is not None
    for section in resp.help_sections:
        if section.get("id") == "shortcuts":
            continue
        items = section.get("items")
        assert items, f"section {section['id']} has no items"
        for item in items:
            assert item.get("label")
            assert item.get("example")


def test_shortcuts_section_present():
    payload = help_sections_payload()
    shortcuts = [s for s in payload if s.get("id") == "shortcuts"]
    assert len(shortcuts) == 1
    assert shortcuts[0].get("shortcuts"), "shortcuts list must not be empty"
    keys = {sc["keys"] for sc in shortcuts[0]["shortcuts"]}
    # Sanity: the most-advertised shortcuts (Cmd+K / Cmd+/) must be there.
    assert "Cmd/Ctrl+K" in keys
    assert "Cmd/Ctrl+/" in keys


def test_help_sections_payload_is_isolated_per_call():
    # Mutating the returned list must not affect subsequent calls — the
    # frontend serializes this through Pydantic, but Python tests could
    # easily mutate it; the helper returns a fresh copy each call.
    a = help_sections_payload()
    a.append({"id": "evil", "title": "x"})
    b = help_sections_payload()
    assert len(b) == 6
    assert all(s.get("id") != "evil" for s in b)
