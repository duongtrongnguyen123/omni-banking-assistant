from __future__ import annotations

from datetime import datetime

from app.models.schemas import Account, Contact, Transaction
from app.safety import rules


def _contact() -> Contact:
    return Contact(
        id="c_minh",
        owner_id="u_test",
        display_name="Minh",
        bank="VCB",
        account_number="123456789",
        account_masked="***6789",
        aliases=[],
        frequent=True,
    )


def _account() -> Account:
    return Account(
        id="a_test",
        bank="VCB",
        number="000111222",
        balance=100_000_000,
        primary=True,
    )


def _tx(amount: int) -> Transaction:
    return Transaction(
        id=f"t_{amount}",
        owner_id="u_test",
        contact_id="c_minh",
        amount=amount,
        description="seed",
        status="completed",
        created_at=datetime.now().astimezone(),
    )


def _flags(amount: int, monkeypatch, daily_total: int):
    monkeypatch.setattr(rules, "_daily_biometric_total", lambda user_id: daily_total)
    monkeypatch.setattr(rules, "_daily_completed_transfer_total", lambda user_id: 0)
    monkeypatch.setattr(
        rules,
        "_user_daily_transfer_limit",
        lambda user_id: ("normal", rules.DAILY_TRANSFER_LIMITS["normal"]),
    )
    return rules.evaluate(
        amount=amount,
        recipient_candidates=[],
        recipient=_contact(),
        transactions=[_tx(1_000_000), _tx(1_200_000), _tx(900_000)],
        account=_account(),
        user_id="u_test",
    )


def test_sub_10m_transfer_stays_otp_until_daily_total_reaches_20m(monkeypatch):
    flags = _flags(8_000_000, monkeypatch, daily_total=8_000_000)

    assert not any(f.code == "daily_biometric_limit" for f in flags)
    assert rules.auth_policy(flags) == ["otp"]


def test_sub_10m_transfer_requires_biometric_when_projected_daily_total_hits_20m(monkeypatch):
    flags = _flags(5_000_000, monkeypatch, daily_total=16_000_000)

    assert any(f.code == "daily_biometric_limit" for f in flags)
    assert rules.auth_policy(flags) == ["otp", "biometric"]


def test_single_transfer_from_10m_requires_biometric(monkeypatch):
    flags = _flags(10_000_000, monkeypatch, daily_total=0)

    assert any(f.code == "large_amount" for f in flags)
    assert rules.auth_policy(flags) == ["otp", "biometric"]


def test_ekyc_daily_limit_blocks_when_projected_total_exceeds_10m(monkeypatch):
    monkeypatch.setattr(rules, "_daily_biometric_total", lambda user_id: 0)
    monkeypatch.setattr(rules, "_daily_completed_transfer_total", lambda user_id: 8_000_000)
    monkeypatch.setattr(rules, "_user_daily_transfer_limit", lambda user_id: ("ekyc", 10_000_000))

    flags = rules.evaluate(
        amount=3_000_000,
        recipient_candidates=[],
        recipient=_contact(),
        transactions=[_tx(1_000_000), _tx(1_200_000), _tx(900_000)],
        account=_account(),
        user_id="u_test",
    )

    limit_flag = next(f for f in flags if f.code == "daily_transfer_limit_exceeded")
    assert limit_flag.severity == "block"
    assert "chi nhánh" in limit_flag.message
    assert rules.auth_policy(flags) == []


def test_normal_daily_limit_blocks_when_projected_total_exceeds_2b(monkeypatch):
    monkeypatch.setattr(rules, "_daily_biometric_total", lambda user_id: 0)
    monkeypatch.setattr(rules, "_daily_completed_transfer_total", lambda user_id: 1_999_000_000)
    monkeypatch.setattr(rules, "_user_daily_transfer_limit", lambda user_id: ("normal", 2_000_000_000))

    flags = rules.evaluate(
        amount=2_000_000,
        recipient_candidates=[],
        recipient=_contact(),
        transactions=[_tx(1_000_000), _tx(1_200_000), _tx(900_000)],
        account=_account(),
        user_id="u_test",
    )

    limit_flag = next(f for f in flags if f.code == "daily_transfer_limit_exceeded")
    assert limit_flag.severity == "block"
    assert "2.000.000.000đ" in limit_flag.message
    assert "chi nhánh" in limit_flag.message
    assert rules.auth_policy(flags) == []
