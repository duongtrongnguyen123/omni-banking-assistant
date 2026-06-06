"""Mock banking operations. Stands in for a real core-banking integration."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean
from typing import Optional
from zoneinfo import ZoneInfo

# Default timezone used to anchor naive datetimes passed into
# ``next_run_for``. ``_safe_day_in_month`` returns a tz-aware datetime
# (via ``.astimezone()``), so comparing it against a naive ``ref`` raises
# ``TypeError: can't compare offset-naive and offset-aware datetimes``.
_DEFAULT_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

from ..config import get_settings
from ..models.schemas import Contact, Schedule, Transaction
from ..services import events
from ..store import get_store, new_id, now

# Threshold below which we surface a "balance low" push notification
# after every transfer. Keeps the toast non-spammy by only firing once
# the user's primary account drops under 100k VND — at that point a
# heads-up is genuinely useful (typical Omni demo balance is 24M).
_BALANCE_LOW_THRESHOLD = 100_000


def _cached(key: str, producer):
    """Cache-aside cho kết quả truy vấn (dict nhỏ, đắt để tính trên 591k dòng).

    Tắt cache => gọi thẳng producer. Redis sập => producer (fail-open).
    """
    settings = get_settings()
    if not settings.cache_enabled:
        return producer()
    from .. import redis_client

    cached = redis_client.get_cache(key)
    if cached is not None:
        return cached
    value = producer()
    redis_client.set_cache(key, value, settings.cache_ttl_seconds)
    return value


def execute_transfer(
    *,
    user_id: str,
    recipient: Contact,
    amount: int,
    description: str = "",
    source_account_id: str | None = None,
    category: str | None = None,
    auth_methods: list[str] | None = None,
) -> Transaction:
    store = get_store()
    user = store.get_user_or_none(user_id)
    kyc_level = user.kyc_level if user is not None else "normal"
    try:
        from ..safety.rules import DAILY_TRANSFER_LIMITS

        daily_limit = DAILY_TRANSFER_LIMITS.get(kyc_level, DAILY_TRANSFER_LIMITS["normal"])
    except Exception:  # pragma: no cover - compliance metadata is best-effort
        daily_limit = None
    daily_total_before = store.completed_transfer_total_today(user_id)
    acc = (
        store.account_by_id(user_id, source_account_id)
        if source_account_id
        else store.primary_account(user_id)
    )
    if acc is None:
        raise ValueError("no_source_account")
    if amount > acc.balance:
        # Push notification so the user sees the failure even if they've
        # navigated away from the chat. Mirrors the chat error string.
        events.publish_transfer_failed(
            user_id, reason=f"Số dư không đủ để chuyển {amount:,}đ.".replace(",", ".")
        )
        raise ValueError("insufficient_balance")
    store.update_balance(user_id, acc.id, -amount)
    # Auto-categorise from description when the caller didn't already
    # decide. Falls back to the legacy "omni" placeholder when the
    # classifier abstains so existing analytics queries keep working.
    if category is None:
        from ..ml.categorizer import categorize as _categorize

        cat, conf = _categorize(description or "Chuyển khoản")
        category = cat if (cat != "other" and conf >= 0.5) else "omni"
    tx = Transaction(
        id=new_id("t"),
        owner_id=user_id,
        contact_id=recipient.id,
        amount=amount,
        description=description or "Chuyển khoản",
        category=category,
        status="completed",
        created_at=now(),
        auth_methods=[m for m in (auth_methods or []) if m in {"otp", "biometric"}],
        kyc_level=kyc_level,
        daily_limit_vnd=daily_limit,
        daily_total_before_vnd=daily_total_before,
    )
    saved = store.add_transaction(tx)

    # Fire-and-forget toasts. Both are non-blocking and fail-open inside
    # the event bus, so they can't break the transfer happy path.
    events.publish_transfer_success(
        user_id, recipient_name=recipient.display_name, amount_vnd=amount
    )
    remaining = acc.balance - amount
    if remaining < _BALANCE_LOW_THRESHOLD:
        events.publish_balance_low(user_id, balance_vnd=remaining)

    # Auto-promote-to-schedule suggest: after this transfer lands, check
    # if the (recipient, amount) pair now forms a recurring pattern of
    # ≥3 occurrences AND no active schedule already covers it. Fail-open
    # — detection error never breaks the transfer happy path.
    try:
        from .recurring import detect_recurring

        all_txs = store.transactions_of(user_id, status="completed")
        patterns = detect_recurring(all_txs)
        for p in patterns:
            if p.contact_id != recipient.id:
                continue
            # Amount within 10% of typical → same pattern.
            if abs(p.typical_amount - amount) > max(p.typical_amount * 0.10, 10_000):
                continue
            if p.occurrence_count < 3:
                continue
            # Skip if a matching schedule is already active.
            already_scheduled = any(
                s.contact_id == recipient.id
                and abs(s.amount - amount) <= max(amount * 0.10, 10_000)
                and s.active
                for s in store.schedules_of(user_id)
            )
            if already_scheduled:
                continue
            events.publish_recurring_suggest(
                user_id,
                recipient_name=recipient.display_name,
                amount_vnd=int(p.typical_amount),
                occurrence_count=p.occurrence_count,
                typical_day=p.typical_day,
            )
            break  # one nudge per transfer
    except Exception:  # pragma: no cover — defensive
        pass

    return saved


def get_balance(user_id: str) -> dict:
    """Return current balance + 7-day outflow sparkline.

    Wrapped in the hien-branch ``_cached`` layer so identical reads
    inside the cache TTL window skip the SQL. Sparkline is part of the
    cached payload — judges see the same 7-day shape until a new
    transfer invalidates (next invocation re-computes after TTL).
    """
    def _compute() -> dict:
        store = get_store()
        user = store.get_user(user_id)
        # 7-day rolling outflow series — one cell per day, oldest → newest.
        # Powers the sparkline on BalanceCard so the user sees their recent
        # spending shape at a glance instead of opening the history view.
        ref = now()
        today = ref.replace(hour=0, minute=0, second=0, microsecond=0)
        start = today - timedelta(days=6)
        daily = [0] * 7
        txs = store.transactions_of(
            user_id, since=start, status="completed",
        )
        for t in txs:
            if t.amount <= 0:
                continue
            bucket = (t.created_at - start).days
            if 0 <= bucket < 7:
                daily[bucket] += t.amount
        return {
            "display_name": user.display_name,
            "accounts": [a.model_dump() for a in user.accounts],
            "total": sum(a.balance for a in user.accounts),
            "recent_outflow_7d": daily,
        }

    try:
        from ..redis_client import user_balance_key

        return _cached(user_balance_key(user_id), _compute)
    except Exception:  # pragma: no cover — redis layer optional
        return _compute()


def _month_window(ref: datetime) -> tuple[datetime, datetime]:
    start = ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def get_history(
    *,
    user_id: str,
    contact_id: Optional[str] = None,
    period: str = "this_month",
    specific_month: Optional[int] = None,
    specific_year: Optional[int] = None,
    all_time: bool = False,
    limit: Optional[int] = None,
    semantic_filter: Optional[str] = None,
) -> dict:
    """Aggregate transaction history with flexible filters.

    Precedence rules:
      * ``all_time`` overrides every period filter — returns the full history.
      * ``specific_month`` (+ optional ``specific_year``) wins over ``period``.
      * ``semantic_filter`` runs *after* the time/contact filters: lexical
        token overlap against ``description`` and ``category``.
      * ``limit`` truncates the items list (kept sorted by created_at desc)
        and rolls aggregates from the truncated set.

    Not cached: the cache key on origin/main only covered (user, contact,
    period) and would return stale results for ``specific_month`` /
    ``all_time`` / ``limit`` / ``semantic_filter`` variants. Until the
    cache key is extended to cover all kwargs, the compute path runs
    every call.
    """
    store = get_store()
    txs = store.transactions_of(user_id)
    if not txs:
        return {"period": period, "count": 0, "total": 0, "items": []}

    ref_now = now()
    if all_time:
        # Effectively no time bound — pick a window wide enough to cover all
        # seeded data plus future-proofing.
        start = ref_now.replace(year=ref_now.year - 50)
        end = ref_now + timedelta(days=365)
        period = "all_time"
    elif specific_month is not None:
        year = specific_year or ref_now.year
        start = ref_now.replace(year=year, month=specific_month, day=1,
                                hour=0, minute=0, second=0, microsecond=0)
        if specific_month == 12:
            end = start.replace(year=year + 1, month=1)
        else:
            end = start.replace(month=specific_month + 1)
        period = f"{year:04d}-{specific_month:02d}"
    elif period == "this_month":
        start, end = _month_window(ref_now)
    elif period == "last_month":
        prev = ref_now.replace(day=1) - timedelta(days=1)
        start, end = _month_window(prev)
    elif period == "today":
        start = ref_now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif period == "yesterday":
        today_start = ref_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = today_start - timedelta(days=1)
        end = today_start
    elif period == "this_week":
        # ISO Monday-start week. ``weekday()`` is 0 = Monday.
        today_start = ref_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = today_start - timedelta(days=today_start.weekday())
        end = start + timedelta(days=7)
    elif period == "last_week":
        today_start = ref_now.replace(hour=0, minute=0, second=0, microsecond=0)
        this_week_start = today_start - timedelta(days=today_start.weekday())
        start = this_week_start - timedelta(days=7)
        end = this_week_start
    elif period == "this_year":
        start = ref_now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1)
    elif period == "last_year":
        this_year_start = ref_now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        start = this_year_start.replace(year=this_year_start.year - 1)
        end = this_year_start
    else:
        start = ref_now - timedelta(days=30)
        end = ref_now + timedelta(days=1)

    items = [t for t in txs if start <= t.created_at < end]
    if contact_id:
        items = [t for t in items if t.contact_id == contact_id]

    if semantic_filter:
        items = _lexical_filter_transactions(items, semantic_filter)

    items.sort(key=lambda t: t.created_at, reverse=True)
    if limit is not None and limit > 0:
        items = items[:limit]

    categories: dict[str, int] = defaultdict(int)
    by_recipient: dict[str, int] = defaultdict(int)
    for t in items:
        categories[t.category] += t.amount
        c = _contact_summary(t.contact_id)
        if c.get("display_name"):
            by_recipient[c["display_name"]] += t.amount

    return {
        "period": period,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "count": len(items),
        "total": sum(t.amount for t in items),
        "items": [
            {
                **t.model_dump(mode="json"),
                "contact": _contact_summary(t.contact_id),
            }
            for t in items
        ],
        "average": int(mean([t.amount for t in items])) if items else 0,
        "by_category": dict(categories),
        "by_recipient": dict(by_recipient),
        "semantic_filter": semantic_filter,
        "limit": limit,
    }


def _lexical_filter_transactions(
    items: list[Transaction], query: str, cutoff: float = 0.34
) -> list[Transaction]:
    """Two-stage filter:

    1. **Vector** (primary) — embed the query, cosine against stored
       transaction embeddings, keep rows whose similarity ≥ 0.40.
    2. **Lexical** (fallback) — token-overlap on description + category +
       a small keyword expansion table. Only runs when no embeddings are
       available (e.g. local model missing, or rows haven't been embedded
       yet).

    The vector stage handles meaningful queries like "ăn uống" → "Cafe +
    ăn trưa", "sức khoẻ" → "Mua thuốc cho mẹ" — things token overlap
    can't reach.
    """
    vec_filtered = _vector_filter_transactions(items, query)
    if vec_filtered is not None:
        return vec_filtered

    from ..context.alias import _STOP_TOKENS, _fold

    CATEGORY_KEYWORDS = {
        "family": ["gia đình", "mẹ", "bố", "ba", "ông", "bà", "anh", "chị", "em"],
        "friends": ["bạn", "ăn", "uống", "cafe", "trà", "nhậu"],
        "work": ["lương", "công việc", "sếp", "đồng nghiệp", "thưởng"],
        "omni": ["chuyển khoản"],
        "other": [],
    }

    q_tokens = {t for t in _fold(query).split() if t and t not in _STOP_TOKENS}
    if not q_tokens:
        return items

    scored: list[tuple[float, Transaction]] = []
    for t in items:
        bits = [t.description, t.category]
        bits.extend(CATEGORY_KEYWORDS.get(t.category, []))
        doc_tokens = {
            tok for b in bits for tok in _fold(b).split()
            if tok and tok not in _STOP_TOKENS
        }
        if not doc_tokens:
            continue
        overlap = q_tokens & doc_tokens
        if not overlap:
            continue
        score = len(overlap) / max(len(q_tokens), 1)
        if score >= cutoff:
            scored.append((score, t))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [t for _, t in scored]


def _vector_filter_transactions(
    items: list[Transaction], query: str, cutoff: float = 0.40
) -> Optional[list[Transaction]]:
    """Embedding-based filter. Returns ``None`` when embeddings aren't
    available (caller falls back to lexical). Returns a list (possibly
    empty) otherwise — empty means "we tried and nothing scored above the
    cutoff", which is honest."""
    from ..db.connection import get_connection
    from ..nlp.embeddings import cosine, embed, unpack

    qv = embed(query, task_type="RETRIEVAL_QUERY")
    if qv is None or not items:
        return None

    ids = [t.id for t in items]
    placeholders = ",".join("?" * len(ids))
    rows = get_connection().execute(
        f"SELECT id, embedding FROM transactions "
        f"WHERE id IN ({placeholders}) AND embedding IS NOT NULL",
        ids,
    ).fetchall()
    if not rows:
        # No transaction in our window has been embedded yet → defer to
        # the lexical fallback so the user still gets *some* answer.
        return None

    by_id = {t.id: t for t in items}
    scored: list[tuple[float, Transaction]] = []
    for row in rows:
        score = cosine(qv, unpack(row["embedding"]))
        if score >= cutoff:
            scored.append((score, by_id[row["id"]]))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [t for _, t in scored]


def _contact_summary(contact_id: str) -> dict:
    store = get_store()
    c = store.get_contact(contact_id)
    if not c:
        return {}
    return {
        "id": c.id,
        "display_name": c.display_name,
        "bank": c.bank,
        "account_masked": c.account_masked,
        "label": c.label,
    }


def create_schedule(
    *,
    user_id: str,
    recipient: Contact,
    amount: int,
    cron: str,
    description: str = "",
    source_account_id: str | None = None,
) -> Schedule:
    store = get_store()
    sched = Schedule(
        id=new_id("s"),
        owner_id=user_id,
        contact_id=recipient.id,
        source_account_id=source_account_id,
        amount=amount,
        description=description,
        cron=cron,
        next_run=_next_run_for(cron, now()),
        active=True,
    )
    saved = store.add_schedule(sched)
    events.publish_schedule_created(
        user_id,
        recipient_name=recipient.display_name,
        amount_vnd=amount,
        cron=cron,
    )
    return saved


def next_run_for(cron: str, ref: datetime) -> datetime:
    """Compute next run for the cron subset we generate in entities.py.

    Supports:
      "0 9 D * *"   -> hour H on day-D of each month
      "0 9 * * w"   -> hour H every weekday w in STANDARD CRON DOW
                      (0=Sun, 1=Mon, ..., 6=Sat)
      "0 9 * * *"   -> hour H every day
    Falls back to ref+30d if the expression doesn't match.
    """
    # Coerce naive ``ref`` to tz-aware Asia/Ho_Chi_Minh — the helper
    # ``_safe_day_in_month`` returns a tz-aware datetime via
    # ``.astimezone()``, so a naive ``ref`` from a caller using
    # ``datetime.now()`` would crash the ``candidate <= ref`` comparison.
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=_DEFAULT_TZ)

    parts = cron.split()
    if len(parts) != 5:
        return ref + timedelta(days=30)
    _, hour, dom, _, dow = parts
    h = int(hour) if hour.isdigit() else 9

    if dom.isdigit():
        day = int(dom)
        candidate = _safe_day_in_month(ref.year, ref.month, day, h)
        if candidate <= ref:
            year, month = (ref.year + 1, 1) if ref.month == 12 else (ref.year, ref.month + 1)
            candidate = _safe_day_in_month(year, month, day, h)
        return candidate

    if dow == "*":
        # Daily: next occurrence at H today (if still future) or tomorrow.
        candidate = ref.replace(hour=h, minute=0, second=0, microsecond=0)
        if candidate <= ref:
            candidate = candidate + timedelta(days=1)
        return candidate

    if dow.isdigit():
        # Convert standard cron DOW (0=Sun, 1=Mon, ..., 6=Sat) to Python's
        # datetime.weekday() convention (0=Mon, 1=Tue, ..., 6=Sun).
        # ``(cron_dow - 1) % 7`` does the rotation. The pre-fix code used
        # ``int(dow) % 7`` which silently aliased every day off by one —
        # "Monday" cron landed on Tuesday, "Sunday" landed on Monday.
        cron_dow = int(dow) % 7
        target = (cron_dow - 1) % 7  # → 0=Mon..6=Sun in datetime convention
        days_ahead = (target - ref.weekday()) % 7
        days_ahead = days_ahead or 7
        return (ref + timedelta(days=days_ahead)).replace(
            hour=h, minute=0, second=0, microsecond=0
        )

    return ref + timedelta(days=30)


def _safe_day_in_month(year: int, month: int, day: int, hour: int) -> datetime:
    """B1: clamp `day` to the month's max so February etc. don't ValueError."""
    import calendar

    last_day = calendar.monthrange(year, month)[1]
    return datetime(year, month, min(day, last_day), hour, 0, 0).astimezone()


# Keep the old name as a private alias for any callers that still reference it
# in the banking module.
_next_run_for = next_run_for
