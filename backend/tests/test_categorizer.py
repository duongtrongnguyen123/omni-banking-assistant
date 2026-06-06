"""Categorizer precision tests.

The categorizer is a two-stage pipeline (keyword rules → TF-IDF nearest
cosine). These tests pin the top-1 precision floor at 0.80 on a 20-row
held-out test set and assert specific behaviour on adversarial inputs.

The 50-row training/validation set below is kept in this file so future
contributors can extend it without touching the seed dictionary inside
``categorizer.py`` (which is shipped to production).
"""

from __future__ import annotations

import pytest

from app.ml.categorizer import CATEGORIES, categorize


# 50 Vietnamese descriptions across all 13 categories, plus a handful of
# noise rows that should land in "other".
_VALIDATION: list[tuple[str, str]] = [
    # food (5)
    ("tiền ăn tháng này", "food"),
    ("cafe sáng cùng đồng nghiệp", "food"),
    ("trà sữa Phúc Long", "food"),
    ("nhậu tất niên", "food"),
    ("phở Lý Quốc Sư", "food"),

    # transport (4)
    ("đổ xăng buổi sáng", "transport"),
    ("Grab về nhà từ công ty", "transport"),
    ("vé máy bay Sài Gòn", "transport"),
    ("sửa xe máy thay nhớt", "transport"),

    # groceries (4)
    ("đi chợ đầu tuần mua rau", "groceries"),
    ("Bách Hoá Xanh cuối tuần", "groceries"),
    ("siêu thị Aeon Mall", "groceries"),
    ("tạp hoá đầu ngõ", "groceries"),

    # shopping (4) — quần áo / mỹ phẩm / đồ tiêu dùng
    ("mua áo Zara cuối tuần", "shopping"),
    ("son môi Maybelline", "shopping"),
    ("đặt giày Shopee", "shopping"),
    ("đồ tiêu dùng cá nhân", "shopping"),

    # entertainment (4)
    ("xem phim CGV tối qua", "entertainment"),
    ("Netflix gói gia đình", "entertainment"),
    ("karaoke với nhóm bạn", "entertainment"),
    ("concert Sơn Tùng MTP", "entertainment"),

    # health (4)
    ("mua thuốc cho mẹ", "health"),
    ("khám tổng quát bệnh viện", "health"),
    ("học phí PT phòng gym", "health"),
    ("yoga tháng này", "health"),

    # rent (3)
    ("tiền nhà tháng 6", "rent"),
    ("tiền trọ tháng này", "rent"),
    ("đặt cọc thuê phòng", "rent"),

    # utilities (3)
    ("tiền điện tháng 5", "utilities"),
    ("internet FPT tháng", "utilities"),
    ("nạp thẻ điện thoại Viettel", "utilities"),

    # gifts (4)
    ("mừng cưới em họ", "gifts"),
    ("lì xì cháu", "gifts"),
    ("phong bì đám hỏi", "gifts"),
    ("quà sinh nhật bạn thân", "gifts"),

    # savings (3)
    ("gửi tiết kiệm online", "savings"),
    ("nạp tiền chứng khoán", "savings"),
    ("mua vàng SJC", "savings"),

    # family (4)
    ("tiền sinh hoạt cho mẹ", "family"),
    ("biếu bố tháng 4", "family"),
    ("gửi bà tiền tiêu vặt", "family"),
    ("hiếu bố tháng 3", "family"),

    # friends (3)
    ("chia tiền ăn nhậu cuối tuần", "friends"),
    ("trả nợ bạn cấp 3", "friends"),
    ("góp tiền sinh nhật bestie", "friends"),

    # work (4)
    ("công tác phí Hà Nội", "work"),
    ("đóng quỹ team building", "work"),
    ("freelance code dự án A", "work"),
    ("hoa hồng tháng này", "work"),

    # other / noise (5)
    ("ok", "other"),
    ("test", "other"),
    ("asdf", "other"),
    ("", "other"),
    ("ck", "other"),
]


def test_validation_set_covers_all_categories() -> None:
    """Defensive: keep the suite honest if someone deletes a row."""
    covered = {label for _, label in _VALIDATION}
    assert covered == set(CATEGORIES) - {"other"} | {"other"}, (
        "Validation set must cover all 13 canonical categories"
    )


def test_top1_precision_on_holdout() -> None:
    """At least 80% top-1 accuracy on a 20-row deterministic holdout."""
    # Time-ordered slice — last 20 rows act as the holdout. The split is
    # fixed by index so the bar is reproducible.
    holdout = _VALIDATION[-20:]
    correct = sum(1 for desc, label in holdout if categorize(desc)[0] == label)
    precision = correct / len(holdout)
    assert precision >= 0.80, (
        f"Top-1 precision {precision:.2f} below 0.80 floor — failures:\n"
        + "\n".join(
            f"  {desc!r} -> {categorize(desc)[0]} (expected {label})"
            for desc, label in holdout
            if categorize(desc)[0] != label
        )
    )


def test_overall_precision() -> None:
    """End-to-end precision across the full validation set."""
    correct = sum(1 for desc, label in _VALIDATION if categorize(desc)[0] == label)
    precision = correct / len(_VALIDATION)
    assert precision >= 0.80, f"Overall precision {precision:.2f} below 0.80"


def test_adversarial_xam_dam_mau_is_not_food() -> None:
    """'xăm đẫm máu' (tattoo blood) substring-matches 'xăm' ≈ 'xăng'.

    A naive substring-keyword classifier would route it to food via
    weak 'ăn' substring; a naive Vietnamese spell-fold would route it
    to transport via 'xam'~'xang'. Our two-stage classifier must reject
    both — anything in {food, transport} is a regression."""
    cat, _conf = categorize("xăm đẫm máu")
    assert cat not in {"food", "transport"}, (
        f"Adversarial 'xăm đẫm máu' wrongly routed to {cat}"
    )


def test_empty_returns_other_zero_confidence() -> None:
    cat, conf = categorize("")
    assert cat == "other"
    assert conf == 0.0


def test_noise_descriptions_route_to_other() -> None:
    """Common noise like 'asdf' / single chars must not trigger a category."""
    for noise in ("asdf", "xx", "    "):
        cat, _ = categorize(noise)
        assert cat == "other", f"Noise {noise!r} got routed to {cat}"


def test_returns_valid_category() -> None:
    """All outputs must be one of the canonical CATEGORIES."""
    for desc, _ in _VALIDATION:
        cat, _ = categorize(desc)
        assert cat in CATEGORIES, f"Unknown category {cat!r} for {desc!r}"


def test_performance_under_2ms() -> None:
    """Categorizer must run in <2ms per call after warmup (P50)."""
    import time

    # Warm up the TF-IDF index.
    categorize("tiền ăn")

    samples = [desc for desc, _ in _VALIDATION if desc]
    timings: list[float] = []
    for desc in samples:
        t0 = time.perf_counter()
        categorize(desc)
        timings.append((time.perf_counter() - t0) * 1000)
    timings.sort()
    p50 = timings[len(timings) // 2]
    assert p50 < 2.0, (
        f"P50 categorize latency {p50:.2f}ms exceeds 2ms budget "
        f"(timings: {[round(t, 2) for t in timings]})"
    )


@pytest.mark.parametrize(
    "description,expected",
    [
        ("tiền ăn", "food"),
        ("trà sữa", "food"),
        ("xăng SH", "transport"),
        ("Grab về", "transport"),
        ("siêu thị WinMart", "groceries"),
        ("Netflix", "entertainment"),
        ("yoga", "health"),
        ("tiền nhà", "rent"),
        ("tiền điện", "utilities"),
        ("lì xì", "gifts"),
        ("gửi tiết kiệm", "savings"),
        ("biếu mẹ", "family"),
        ("trả nợ bạn", "friends"),
        ("quỹ team", "work"),
    ],
)
def test_canonical_examples(description: str, expected: str) -> None:
    cat, _ = categorize(description)
    assert cat == expected
