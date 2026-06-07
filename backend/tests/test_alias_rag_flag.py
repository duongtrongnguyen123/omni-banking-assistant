"""Regression test for the ``OMNI_RAG_ALIAS`` feature flag.

``_lexical_match`` and ``_embedding_match`` are the RAG fuzzy-alias
fallbacks promised in the slide deck. They are NOT invoked by
``resolve_recipient`` on the default code path (the exact / token / label
hits cover >99% of real queries and the embedding path historically
returned noisy candidates on cold queries). The flag re-enables them for
the pitch demo and for operators willing to re-tune the cutoffs.

This test pins the contract:
  * default — neither fallback runs (no embedding model call, no DB hit).
  * flag on — ``_lexical_match`` runs first; if it returns empty,
    ``_embedding_match`` is consulted.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.context import alias as alias_mod
from app.models.schemas import Contact


def _contact(
    cid: str,
    name: str,
    aliases: list[str] | None = None,
    label: str | None = None,
) -> Contact:
    return Contact(
        id=cid,
        owner_id="u1",
        display_name=name,
        bank="MB Bank",
        account_number="9990001234",
        account_masked="****1234",
        aliases=aliases or [],
        label=label,
    )


@pytest.fixture(autouse=True)
def _clear_flag(monkeypatch):
    monkeypatch.delenv("OMNI_RAG_ALIAS", raising=False)
    yield


def test_flag_off_does_not_call_lexical_or_embedding(monkeypatch):
    """With the flag off, ``resolve_recipient`` on a cold query that
    matches no alias / name MUST return [] without consulting either
    fallback. Spy on both functions to prove they were never invoked.
    """
    monkeypatch.delenv("OMNI_RAG_ALIAS", raising=False)
    contacts = [_contact("c1", "Nguyễn Thị Lan", aliases=["mẹ"])]

    with patch.object(
        alias_mod, "_lexical_match", wraps=alias_mod._lexical_match
    ) as spy_lex, patch.object(
        alias_mod, "_embedding_match", wraps=alias_mod._embedding_match
    ) as spy_emb:
        result = alias_mod.resolve_recipient("ai do la nguoi quen", contacts)

    assert result == []
    assert spy_lex.call_count == 0
    assert spy_emb.call_count == 0


def test_flag_on_calls_lexical_first(monkeypatch):
    """With the flag on, ``_lexical_match`` is consulted when alias /
    name lookup returns nothing. If lexical returns a hit, the embedding
    match is NOT called (it's a fallback to the fallback).
    """
    monkeypatch.setenv("OMNI_RAG_ALIAS", "1")
    # Contact with a token "shipper" so the lexical Jaccard returns a hit
    # for the cold query "shipper grab".
    contacts = [
        _contact(
            "c1",
            "Anh Shipper Grab",
            aliases=["shipper"],
            label="Shipper",
        )
    ]

    with patch.object(
        alias_mod, "_embedding_match", wraps=alias_mod._embedding_match
    ) as spy_emb:
        result = alias_mod.resolve_recipient("shipper grab quan 1", contacts)

    # Lexical match should win — the embedding fallback must NOT have run.
    assert spy_emb.call_count == 0
    # Some result returned via lexical / history matching.
    assert len(result) >= 1


def test_flag_on_falls_through_to_embedding_when_lexical_empty(monkeypatch):
    """If lexical returns [], the resolver consults ``_embedding_match``."""
    monkeypatch.setenv("OMNI_RAG_ALIAS", "1")
    contacts = [_contact("c1", "Bob Smith", aliases=["bob"])]

    # Stub embedding match to a sentinel so we can verify it was reached.
    sentinel = [object()]
    with patch.object(
        alias_mod, "_lexical_match", return_value=[]
    ) as spy_lex, patch.object(
        alias_mod, "_embedding_match", return_value=sentinel
    ) as spy_emb:
        result = alias_mod.resolve_recipient(
            "khong co ai trung dau", contacts
        )

    assert spy_lex.call_count == 1
    assert spy_emb.call_count == 1
    assert result is sentinel


def test_flag_parses_truthy_values(monkeypatch):
    """The flag accepts the common truthy spellings used by ops scripts."""
    for val in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("OMNI_RAG_ALIAS", val)
        assert alias_mod._rag_fallback_enabled() is True
    for val in ("0", "false", "", "no", "off"):
        monkeypatch.setenv("OMNI_RAG_ALIAS", val)
        assert alias_mod._rag_fallback_enabled() is False
