"""Regression coverage for ``_annotate_predicted_category`` —
the read-path categorizer hook from feat/category-knn-v2.

The helper takes a single history-facts entry (a dict) and:

  1. Returns it unchanged if ``category`` is anything other than
     ``"other"`` (don't re-classify rows that already have a label).
  2. Calls ``ml.categorizer.categorize`` on the description.
  3. Leaves the row untouched when the categorizer also returns
     ``"other"`` (no signal) or its confidence is below the floor.
  4. Otherwise adds ``predicted_category`` + ``predicted_confidence``
     to the returned dict — original keys are preserved verbatim.

Errors inside the categorizer are swallowed: history replies must
never break on ML.

We monkeypatch ``categorize_description`` (the name the orchestrator
imported the function as) so we drive each branch deterministically
without spinning up the TF-IDF index.
"""

from __future__ import annotations

from app.services import orchestrator
from app.services.orchestrator import _annotate_predicted_category


# ---------------------------------------------------------------------------
# 1. Non-other rows are NEVER re-classified.
# ---------------------------------------------------------------------------


def test_already_categorised_row_is_untouched(monkeypatch):
    sentinel_called = {"hit": False}

    def _spy(_desc: str):
        sentinel_called["hit"] = True
        return ("food", 0.99)

    monkeypatch.setattr(orchestrator, "categorize_description", _spy)
    row = {
        "recipient": "Mẹ",
        "amount": 100_000,
        "description": "Trả nợ thẻ",
        "category": "debt",  # already labelled
        "created_at": "2026-06-01",
    }
    out = _annotate_predicted_category(row)
    assert out == row
    assert sentinel_called["hit"] is False  # never called


def test_other_with_clear_description_gets_annotated(monkeypatch):
    monkeypatch.setattr(
        orchestrator, "categorize_description", lambda _d: ("rent", 0.95),
    )
    row = {
        "recipient": "Landlord",
        "amount": 5_000_000,
        "description": "tiền nhà",
        "category": "other",
        "created_at": "2026-06-01",
    }
    out = _annotate_predicted_category(row)
    assert out["predicted_category"] == "rent"
    assert out["predicted_confidence"] == 0.95
    # Original fields preserved verbatim.
    for k, v in row.items():
        assert out[k] == v


# ---------------------------------------------------------------------------
# 2. The categorizer returning "other" (no signal) is treated the same as
#    being below confidence — leave the row alone.
# ---------------------------------------------------------------------------


def test_categorizer_returning_other_does_not_annotate(monkeypatch):
    """Noise descriptions (\"hi\", \"asdf\") send the categorizer back to
    \"other\" — we must NOT then add ``predicted_category: \"other\"`` to
    the dict; that would be a confusing no-op chip on the UI."""
    monkeypatch.setattr(
        orchestrator, "categorize_description", lambda _d: ("other", 0.0),
    )
    row = {
        "recipient": "X",
        "amount": 50_000,
        "description": "hi",
        "category": "other",
        "created_at": "2026-06-01",
    }
    out = _annotate_predicted_category(row)
    assert "predicted_category" not in out
    assert "predicted_confidence" not in out
    assert out == row


def test_low_confidence_prediction_does_not_annotate(monkeypatch):
    """Prediction at 0.4 confidence (below the 0.5 floor) is dropped —
    better to leave \"other\" than to render a weak chip."""
    monkeypatch.setattr(
        orchestrator, "categorize_description", lambda _d: ("food", 0.4),
    )
    row = {
        "description": "đi ăn",
        "category": "other",
    }
    out = _annotate_predicted_category(row)
    assert "predicted_category" not in out


def test_confidence_at_floor_is_accepted(monkeypatch):
    """0.5 (the floor) should annotate — the guard is `< floor`, not
    `<= floor`."""
    monkeypatch.setattr(
        orchestrator, "categorize_description", lambda _d: ("food", 0.5),
    )
    row = {"description": "đi ăn", "category": "other"}
    out = _annotate_predicted_category(row)
    assert out["predicted_category"] == "food"
    assert out["predicted_confidence"] == 0.5


# ---------------------------------------------------------------------------
# 3. Categorizer raising MUST NOT break history replies.
# ---------------------------------------------------------------------------


def test_categorizer_exception_falls_through_silently(monkeypatch):
    def _explode(_d: str):
        raise RuntimeError("TF-IDF index not loaded")

    monkeypatch.setattr(orchestrator, "categorize_description", _explode)
    row = {
        "description": "tiền điện",
        "category": "other",
    }
    out = _annotate_predicted_category(row)
    assert out == row  # untouched, no crash
    assert "predicted_category" not in out


# ---------------------------------------------------------------------------
# 4. Defensive defaults — missing description / category keys.
# ---------------------------------------------------------------------------


def test_missing_description_treated_as_empty(monkeypatch):
    """A history row without `description` shouldn't crash; we just
    pass empty string to the categorizer."""
    seen = {}

    def _spy(desc: str):
        seen["desc"] = desc
        return ("other", 0.0)

    monkeypatch.setattr(orchestrator, "categorize_description", _spy)
    row = {"category": "other"}
    out = _annotate_predicted_category(row)
    assert seen["desc"] == ""
    assert out == row


def test_missing_category_treated_as_non_other(monkeypatch):
    """A history row without `category` shouldn't enter the
    re-classification path at all — by convention, downstream code
    treats absent labels as untouchable."""
    sentinel = {"hit": False}

    def _spy(_d):
        sentinel["hit"] = True
        return ("food", 0.99)

    monkeypatch.setattr(orchestrator, "categorize_description", _spy)
    out = _annotate_predicted_category({"description": "tiền nhà"})
    assert sentinel["hit"] is False
    assert "predicted_category" not in out
