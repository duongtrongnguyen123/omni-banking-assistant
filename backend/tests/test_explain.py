"""Tests for the explain service.

Covers the five distinct AuditEvent shapes the orchestrator produces:
  - draft_created (transfer staged, waiting for user)
  - executed (full happy path, OTP passed)
  - blocked (safety rejected — insufficient balance / ambiguous)
  - cancel-like decision (auth_failed for OTP mismatch)
  - select-like decision (auth_partial after disambiguation)

Each test asserts:
  - explain returns ≥ 3 steps
  - there is exactly one `nlu` step and its `source` field is set
  - banking decision matches the recorded event decision family
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models.schemas import AuditEvent
from app.services.explain import build_explanation


def _make_event(**overrides) -> AuditEvent:
    base = dict(
        id="ae_test",
        created_at=datetime(2026, 6, 6, 8, 14, tzinfo=timezone.utc),
        user_id="u_an",
        message="Chuyển mẹ 2 triệu",
        nlu_source="llm",
        intent="transfer",
        entities={
            "recipient_text": "mẹ",
            "amount": 2_000_000,
            "amount_text": "2 triệu",
        },
        resolved_recipient="Nguyễn Thị Lan",
        selected_account=None,
        safety_flags=[],
        auth_required=["otp"],
        auth_completed=[],
        decision="draft_created",
    )
    base.update(overrides)
    return AuditEvent(**base)


def _assert_nlu_step(steps: list[dict]) -> dict:
    nlu_steps = [s for s in steps if s["layer"] == "nlu"]
    assert len(nlu_steps) == 1, f"expected exactly one nlu step, got {nlu_steps}"
    assert nlu_steps[0]["source"] in {"llm", "rule", "unknown"}
    return nlu_steps[0]


def _assert_banking_decision(steps: list[dict], expected_decisions: set[str]) -> None:
    banking_steps = [s for s in steps if s["layer"] == "banking"]
    assert banking_steps, "missing banking step"
    last = banking_steps[-1]
    assert last["decision"] in expected_decisions, (
        f"banking decision={last['decision']!r} not in {expected_decisions}"
    )


# ---- Shape 1: draft_created --------------------------------------------------


def test_draft_created_produces_full_chain():
    ev = _make_event()
    out = build_explanation(ev)

    assert out["audit_id"] == ev.id
    assert "draft" in out["summary"].lower() or "chờ" in out["summary"]
    assert len(out["steps"]) >= 3

    nlu = _assert_nlu_step(out["steps"])
    assert nlu["source"] == "llm"
    _assert_banking_decision(out["steps"], {"draft"})

    # Context steps should mention both recipient and amount.
    decisions = [s["decision"] for s in out["steps"]]
    assert any("recipient=" in d for d in decisions), decisions
    assert any("amount=2000000" in d for d in decisions), decisions


# ---- Shape 2: executed -------------------------------------------------------


def test_executed_path_includes_auth_step():
    ev = _make_event(
        decision="executed",
        auth_completed=["otp"],
        nlu_source="rule",
        selected_account=None,
    )
    out = build_explanation(ev)

    assert len(out["steps"]) >= 3
    nlu = _assert_nlu_step(out["steps"])
    assert nlu["source"] == "rule"
    _assert_banking_decision(out["steps"], {"execute"})
    # Auth step should reflect completion.
    auth_steps = [
        s for s in out["steps"] if s["layer"] == "safety" and "auth=" in s["decision"]
    ]
    assert auth_steps, "auth step missing on executed shape"
    assert "hoàn tất" in auth_steps[0]["rationale"].lower() or "completed" in auth_steps[0]["rationale_en"].lower()


# ---- Shape 3: blocked --------------------------------------------------------


def test_blocked_event_marks_safety_decision_as_blocked():
    ev = _make_event(
        decision="blocked",
        safety_flags=["insufficient_balance"],
        auth_required=[],
    )
    out = build_explanation(ev)

    assert len(out["steps"]) >= 3
    _assert_nlu_step(out["steps"])
    safety_steps = [s for s in out["steps"] if s["layer"] == "safety"]
    assert safety_steps, "expected at least one safety step"
    assert safety_steps[0]["decision"] == "blocked"
    _assert_banking_decision(out["steps"], {"reject"})


# ---- Shape 4: cancel-like (auth_failed) --------------------------------------


def test_auth_failed_records_otp_mismatch():
    ev = _make_event(
        decision="auth_failed",
        auth_required=["otp"],
        auth_completed=[],
    )
    out = build_explanation(ev)

    assert len(out["steps"]) >= 3
    _assert_nlu_step(out["steps"])
    _assert_banking_decision(out["steps"], {"auth_failed"})


# ---- Shape 5: select-like (auth_partial after disambiguation) ----------------


def test_auth_partial_after_disambiguation():
    ev = _make_event(
        decision="auth_partial",
        message="Trần Hoàng Minh",
        entities={"recipient_text": "Minh"},
        resolved_recipient="Trần Hoàng Minh",
        safety_flags=["large_amount"],
        auth_required=["otp", "biometric"],
        auth_completed=["biometric"],
    )
    out = build_explanation(ev)

    assert len(out["steps"]) >= 3
    nlu = _assert_nlu_step(out["steps"])
    assert nlu["source"] in {"llm", "rule"}
    safety_steps = [s for s in out["steps"] if s["layer"] == "safety"]
    # Should have at least the rule-engine safety step plus the auth step.
    assert any("warn" in s["decision"] for s in safety_steps), safety_steps
    auth = [s for s in safety_steps if "auth=" in s["decision"]]
    assert auth, "expected auth step on partial-auth shape"
    assert "biometric" in auth[0]["decision"]
    _assert_banking_decision(out["steps"], {"auth_partial"})


# ---- Bonus: rationale_en is always present -----------------------------------


@pytest.mark.parametrize(
    "decision,flags,auth_req,auth_done",
    [
        ("draft_created", [], ["otp"], []),
        ("executed", [], ["otp"], ["otp"]),
        ("blocked", ["insufficient_balance"], [], []),
        ("auth_failed", [], ["otp"], []),
        ("auth_partial", ["large_amount"], ["otp", "biometric"], ["biometric"]),
    ],
)
def test_every_step_has_rationale_en(decision, flags, auth_req, auth_done):
    ev = _make_event(
        decision=decision,
        safety_flags=flags,
        auth_required=auth_req,
        auth_completed=auth_done,
    )
    out = build_explanation(ev)
    for s in out["steps"]:
        assert s.get("rationale"), f"missing rationale on {s}"
        assert s.get("rationale_en"), f"missing rationale_en on {s}"
