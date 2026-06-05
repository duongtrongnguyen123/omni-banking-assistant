"""Chat-side handler for the ``insights`` intent.

Lives in its own module so the dispatch site in ``orchestrator.py`` is
the only thing that needs touching when wiring this in — the long
handler body stays out of the way of the many in-flight merges that
edit orchestrator.py concurrently.

Routed to by the rule classifier's Tier-1 keywords ("bat thuong",
"so voi thang truoc", "phan tich chi tieu", "kha nghi",
"dang ky dich vu", …). Mirrors ``/api/insights/summary`` but composes a
short Vietnamese reply targeted at *what* the user asked: anomaly hits,
month-over-month deltas, or subscription audit.

Why this isn't ``history``: history aggregates a single period, insights
*compares* periods, mines subscription patterns, and lists statistical
outliers. Different data shape, different response shape.
"""

from __future__ import annotations

from typing import Optional

from ..models.schemas import NLUResult, OmniResponse
from ..nlp.amount import format_vnd

_ASK_ANOMALY = (
    "bất thường", "bat thuong", "khả nghi", "kha nghi",
    "có gì lạ", "co gi la", "điểm lạ", "diem la",
)
_ASK_COMPARE = (
    "so với", "so voi", "so sánh", "so sanh",
    "nhiều hơn", "nhieu hon", "ít hơn", "it hon",
    "tăng", "tang", "giảm", "giam",
)
_ASK_SUBS = (
    "đăng ký dịch vụ", "dang ky dich vu", "subscription",
    "thuê bao", "thue bao", "cắt giảm", "cat giam",
    "khoản nào thừa", "khoan nao thua",
)


def handle_insights(
    user_id: str,
    nlu: NLUResult,
    history_msgs: Optional[list[dict]] = None,
) -> OmniResponse:
    del history_msgs  # facts come from the insights summary, not chat history
    # Import lazily so this module stays cheap to import — the orchestrator
    # touches it on every chat turn even when the intent isn't insights.
    from ..ml.insights import summary as _insights_summary

    data = _insights_summary(user_id)
    mom = data.get("mom") or {}
    anomalies = data.get("anomalies") or []
    subs = data.get("subscriptions") or []

    q = nlu.raw_text.lower()
    asks_anomaly = any(kw in q for kw in _ASK_ANOMALY)
    asks_compare = any(kw in q for kw in _ASK_COMPARE)
    asks_subs = any(kw in q for kw in _ASK_SUBS)

    parts: list[str] = []

    if asks_anomaly or (not asks_compare and not asks_subs and anomalies):
        if anomalies:
            top = anomalies[:3]
            lines = []
            for a in top:
                amt = format_vnd(a.get("amount", 0))
                # MAD detector returns "typical"; older variants "typical_amount" —
                # accept both so the line still renders cleanly.
                typ = a.get("typical") or a.get("typical_amount") or 0
                contact = a.get("contact") or a.get("contact_name") or "(không rõ)"
                lines.append(f"• {contact}: {amt} (thường ~{format_vnd(typ)})")
            parts.append(
                f"Mình thấy {len(anomalies)} giao dịch nổi bật so với thói "
                f"quen của bạn:\n" + "\n".join(lines)
            )
        elif asks_anomaly:
            parts.append("Mình chưa phát hiện giao dịch nào bất thường gần đây.")

    if asks_compare or (not parts and not asks_subs):
        deltas = sorted(
            (
                (cat, vals.get("this", 0), vals.get("last", 0), vals.get("delta_pct", 0))
                for cat, vals in mom.items()
            ),
            key=lambda x: abs(x[3]),
            reverse=True,
        )[:3]
        if deltas:
            lines = []
            for cat, this_v, last_v, pct in deltas:
                arrow = "↑" if pct > 0 else ("↓" if pct < 0 else "→")
                lines.append(
                    f"• {cat}: {format_vnd(this_v)} {arrow} "
                    f"{abs(pct):.0f}% so với {format_vnd(last_v)} tháng trước"
                )
            parts.append(
                "Tháng này so với tháng trước:\n" + "\n".join(lines)
            )

    if asks_subs or (not parts and subs):
        if subs:
            lines = []
            for s in subs[:5]:
                amt = format_vnd(s.get("typical_amount", 0))
                lines.append(
                    f"• {s.get('contact', '(không rõ)')}: ~{amt}/tháng "
                    f"({s.get('occurrences', 0)} lần)"
                )
            parts.append(
                "Các khoản trông giống đăng ký định kỳ:\n" + "\n".join(lines)
            )
        elif asks_subs:
            parts.append("Mình chưa thấy khoản nào đủ dữ liệu để xem là định kỳ.")

    text = "\n\n".join(parts) if parts else (
        "Mình chưa đủ dữ liệu để tổng hợp insight cho bạn lúc này."
    )
    return OmniResponse(intent="insights", text=text)
