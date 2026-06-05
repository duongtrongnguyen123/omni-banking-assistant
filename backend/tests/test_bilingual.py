"""Bilingual NLU smoke — 20 English utterances exercising every intent.

The rule extractor is what runs when GROQ_API_KEY / GEMINI_API_KEY are
absent (i.e. in CI and on a default contributor laptop), so these tests
are a fair check of the deterministic baseline. Importantly we don't
just assert intents — for the canonical English transfer example we
also check that the amount and recipient text fall out correctly,
because that's the user-visible promise: typing English into Omni
shouldn't lose the parse.

If a Groq key happens to be present locally the LLM path may produce
better answers — but never worse for these utterances. The asserts are
written to pass either way.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make `app.*` importable when pytest is invoked from repo root.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Force the rule path so the suite is deterministic.
os.environ.pop("GROQ_API_KEY", None)
os.environ.pop("GEMINI_API_KEY", None)

import pytest

from app.nlp.amount import parse_amount
from app.nlp.entities import extract
from app.nlp.intent import classify
from app.nlp.pipeline import understand
from app.services.responses import detect_lang, t as response_t


# (utterance, expected_intent) — 20 entries covering every intent we care
# about plus a few near-collision cases.
CASES = [
    # Transfers (5).
    ("transfer 2 million to mom", "transfer"),
    ("send Minh 500k", "transfer"),
    ("pay Hung 1 million for dinner", "transfer"),
    ("wire 5 million to dad", "transfer"),
    ("send mom 5 million like last month", "transfer"),
    # Balance (3).
    ("how much do I have", "balance"),
    ("check my balance", "balance"),
    ("account balance", "balance"),
    # History (4).
    ("show my transactions this month", "history"),
    ("how much did I spend last month", "history"),
    ("transaction history", "history"),
    ("total spent last week", "history"),
    # Schedule (2).
    ("schedule 2 million to mom every month", "schedule"),
    ("set up a recurring transfer every week", "schedule"),
    # Smalltalk (4).
    ("hello", "smalltalk"),
    ("good morning", "smalltalk"),
    ("thanks", "smalltalk"),
    ("ok", "smalltalk"),
    # Reminder (2).
    ("remind me to pay rent", "reminder"),
    ("remind me to send Minh", "reminder"),
]


@pytest.mark.parametrize("utterance, expected_intent", CASES)
def test_english_intents(utterance: str, expected_intent: str) -> None:
    nlu = understand(utterance)
    assert nlu.intent == expected_intent, (
        f"Expected {expected_intent} for {utterance!r}, got {nlu.intent}"
    )


def test_english_transfer_extracts_amount_and_recipient() -> None:
    """Anchor utterance from the task brief — must parse cleanly."""
    nlu = understand("transfer 2 million to mom")
    assert nlu.intent == "transfer"
    assert nlu.entities.amount == 2_000_000
    # Recipient text is lowercased by the regex pattern; just check it
    # contains "mom" (case-insensitive).
    assert nlu.entities.recipient_text is not None
    assert "mom" in nlu.entities.recipient_text.lower()


def test_english_amount_variants() -> None:
    cases = [
        ("send 5 million", 5_000_000),
        ("send 500k", 500_000),
        ("transfer 1 billion to mom", 1_000_000_000),
        ("pay 2 thousand", 2_000),
    ]
    for utterance, expected in cases:
        amount, _ = parse_amount(utterance)
        assert amount == expected, f"{utterance!r} → {amount}, expected {expected}"


def test_english_temporal_phrasing() -> None:
    """`last month`, `yesterday`, `last week` must surface as temporal refs."""
    for utterance in [
        "send mom 5 million like last month",
        "what did I spend yesterday",
        "transactions from last week",
    ]:
        out = extract(utterance)
        assert out.temporal_reference is not None, (
            f"No temporal_reference picked from {utterance!r}"
        )


def test_classify_does_not_misroute_transfer_to_balance() -> None:
    """Edge case — `transfer 2 million` should not hit the `balance` keyword."""
    intent, _ = classify("transfer 2 million to mom")
    assert intent == "transfer"


# ---------------------------------------------------------------------------
# Response translation helper.
# ---------------------------------------------------------------------------


def test_detect_lang_query_overrides_header() -> None:
    assert detect_lang(accept_language="vi-VN", query_lang="en") == "en"
    assert detect_lang(accept_language="en-US", query_lang="vi") == "vi"


def test_detect_lang_accept_language_header() -> None:
    assert detect_lang(accept_language="en-US,en;q=0.9,vi;q=0.5") == "en"
    assert detect_lang(accept_language="vi-VN,vi;q=0.9") == "vi"
    assert detect_lang() == "vi"  # default


def test_responses_translate() -> None:
    vi = response_t(
        "transfer_confirmed", "vi",
        amount="2.000.000đ", name="Mẹ", bank="VCB", tx_id="tx_1",
    )
    en = response_t(
        "transfer_confirmed", "en",
        amount="2,000,000 ₫", name="Mom", bank="VCB", tx_id="tx_1",
    )
    assert "Đã chuyển" in vi
    assert "Sent" in en
    assert "tx_1" in vi and "tx_1" in en


def test_responses_fall_back_to_vi_for_unknown_key() -> None:
    # An unknown key returns itself rather than crashing.
    out = response_t("does_not_exist", "en")
    assert out == "does_not_exist"
