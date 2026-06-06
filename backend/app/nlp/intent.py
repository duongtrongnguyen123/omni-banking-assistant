"""Intent classifier — keyword-priority with Vietnamese diacritic tolerance.

Uses three tiers so specific signals win deterministically over generic
ones — important because this is the fallback when the LLM rate-limits
out and we still need correct routing.

Tier 1 (HIGH, 0.85): unambiguous keywords (`so du`, `lich su`, `dat lich`).
Tier 2 (MED, 0.65):  precise but possibly overlapping (`bao nhieu`, `tieu`,
                     `lan cuoi`, transfer verbs).
Tier 3 (LOW, 0.4):   fallback heuristic (bare digit → transfer).
"""

from __future__ import annotations

import re
import unicodedata

from ..models.schemas import Intent


def _ascii_fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.replace("đ", "d").replace("Đ", "D").lower()


# ---------------------------------------------------------------------------
# Tier 1 — high precision, very unambiguous
# ---------------------------------------------------------------------------

_HIGH: list[tuple[Intent, list[str]]] = [
    # atm_finder — location-aware ATM / branch lookup. Placed at the top
    # of Tier-1 so "atm gần nhất" never gets misrouted to history's
    # "gan nhat" rule below.
    ("atm_finder", [
        "atm gan", "atm gần", "atm o gan", "atm ở gần",
        "cay atm", "cây atm", "may atm", "máy atm",
        "atm nao gan", "atm nào gần", "atm gan day", "atm gần đây",
        "tim atm", "tìm atm", "tim cay atm", "tìm cây atm",
        "chi nhanh gan", "chi nhánh gần",
        "phong giao dich gan", "phòng giao dịch gần",
        "atm gan nhat", "atm gần nhất",
        # Bank-only ATM queries — "atm vcb", "atm mb bank quanh day"
        "atm vcb", "atm vietcom", "atm tcb", "atm techcom",
        "atm bidv", "atm mb", "atm vpb", "atm acb", "atm agribank",
        "atm sacom", "atm stb",
    ]),
    # insights (proactive analytics) — before history so "tieu nhieu hon
    # thang truoc" routes here, not to plain history.
    ("insights", [
        "nhieu hon thang truoc", "it hon thang truoc",
        "tieu nhieu hon thang nay", "chi nhieu hon thang nay",
        "so voi thang truoc", "so sanh thang truoc",
        "so sanh chi tieu", "so sanh tieu",  # "so sánh chi tiêu tháng này"
        "bat thuong",  # "giao dịch nào bất thường", "có gì bất thường"
        "kha nghi",    # "có gì khả nghi", "thấy gì khả nghi không"
        "co diem la",
        "chi tieu nao la", "khoan chi nao la",  # "có chi tiêu nào lạ không"
        "giao dich nao la",
        "dang ky dich vu", "subscription", "thue bao hang thang",
        "co the cat giam", "khoan nao thua",
        "phan tich chi tieu", "phan tich tieu",
    ]),
    # recurring (read) before schedule (create): "khoan dinh ky" / "tu dong
    # hang thang" are queries about existing patterns, not commands to make
    # a new one. Schedule keeps its imperative cues.
    ("recurring", [
        "khoan dinh ky", "cac khoan dinh ky", "khoan tu dong",
        "khoan nao tu dong", "khoan nao dinh ky", "khoan nao tra deu",
        "khoan nao tra dinh ky", "khoan tra tu dong", "khoan tra dinh ky",
        "tra deu hang thang", "chi deu hang thang", "tra tu dong",
        "co khoan nao tra", "co khoan nao dinh ky",
        "liet ke lich", "xem lich tu dong", "lich tu dong",
        "liet ke khoan", "liet ke cac khoan",  # "liệt kê các khoản trả tự động"
        "khoan dinh ky cua toi", "khoan dinh ky cua minh",
    ]),
    ("schedule", [
        "dat lich", "len lich", "lap lich",
        "tu dong chuyen", "thiet lap lich",
    ]),
    ("reminder", ["nhac no", "nhac tra", "nhac thanh toan", "tao nhac"]),
    ("balance", [
        "so du", "kiem tra so du", "xem so du", "balance",
        "tai khoan con", "con bao nhieu trong tai khoan",
        # Common Vietnamese colloquialisms — "do I still have money?"
        # All of these are high-precision: as substrings they rarely
        # appear inside an unrelated history / transfer command.
        "con bao nhieu tien",      # "còn bao nhiêu tiền"
        "con nhieu tien",          # "còn nhiêu tiền" (casual)
        "het tien chua",           # "hết tiền chưa"
        "het sach tien",           # "hết sạch tiền"
        "het sach vi",             # "hết sạch ví"
        "can vi",                  # "cạn ví" — out of money slang
        "can sach vi",             # "cạn sạch ví"
        "tien nong con",           # "tiền nong còn (không)"
        "tien con khong",          # "tiền còn không"
        "tien con ko",             # casual
        "luong ve chua",           # "lương về chưa" — payday check
        "luong ve roi",            # "lương về rồi (chưa)"
    ]),
    ("history", [
        "lich su", "thong ke", "sao ke", "bao cao chi tieu",
        "bao cao thang", "bao cao chi", "bao cao tieu",  # "Báo cáo tháng / chi tiêu"
        "tong chi phi", "tong chi", "tong tieu",         # "Tổng chi phí hàng tháng"
        "ai nhan nhieu", "ai gui nhieu", "ai chuyen nhieu",
        "nhieu nhat", "lan cuoi", "lan gan nhat", "gan nhat",
        "5 giao dich", "3 giao dich", "10 giao dich",
        "giao dich gan day", "xem giao dich",
        "tu truoc den gio", "tat ca cac lan",
    ]),
    ("add_contact", [
        "luu danh ba", "them danh ba", "luu lien lac", "luu so",
        "them lien he",
    ]),
    # goal_status — progress query for an existing savings goal. Goes
    # *before* the smalltalk row in Tier-1 so "muc tieu cua toi" can't
    # accidentally fall through. Conservative anchor: needs both "muc
    # tieu" (or "tien do") AND a possessive / progress cue so a bare
    # "mục tiêu cuộc đời" doesn't get eaten.
    ("goal_status", [
        "tien do muc tieu",  # "tiến độ mục tiêu"
        "muc tieu cua toi",  # "mục tiêu của tôi"
        "muc tieu cua minh", # "mục tiêu của mình"
        "da tiet kiem duoc",  # "đã tiết kiệm được bao nhiêu"
        "tiet kiem den dau",  # "tiết kiệm đến đâu rồi"
        "con thieu bao nhieu cho muc tieu",  # explicit
        "muc tieu sap dat", "muc tieu cua minh sap",
    ]),
    ("smalltalk", [
        "xin chao", "chao omni", "hello", "cam on",
        # English thanks variants — judges who switch language mid-flow
        # shouldn't fall to the generic "chưa rõ ý" fallback.
        "thank you", "thanks",
        # Farewells + casual variants — judges who say goodbye to the
        # assistant shouldn't get the safe "unknown" fallback that
        # invites them to "thử chuyển cho mẹ 2 triệu".
        "tam biet", "bye omni", "bye bye", "goodbye", "tạm biệt",
        "good morning", "good evening", "chao buoi sang",
        # Vietnamese greetings with salutation forms — "chào em" / "chào
        # anh" / "chào chị" / "chào cô / chú / bác / mọi người / bạn".
        # The bare "chao" substring is intentionally NOT here because it
        # would false-positive inside common words. Two-token forms are
        # safe — they don't appear inside transfer or history commands.
        "chao em", "chao anh", "chao chi", "chao co ", "chao chu ",
        "chao bac", "chao moi nguoi", "chao ban",
    ]),
]

# ---------------------------------------------------------------------------
# Tier 2 — medium precision; check after high-tier short-circuit
# ---------------------------------------------------------------------------

_MED: list[tuple[Intent, list[str]]] = [
    ("schedule", [
        "hang thang", "moi thang", "hang tuan", "moi tuan",
        "moi ngay", "hang ngay",
    ]),
    # history before transfer: "bao nhieu" + verb is retrospective, not a
    # transfer command. Transfer queries have a concrete amount.
    ("history", [
        "bao nhieu", "da tieu", "da gui", "da chuyen",
        "tieu bao", "minh tieu", "toi tieu", "tieu cho",
        "chi cho", "chi gi", "chi nao", "khoan chi", "khoan nao",
        "tong cong", "tong chi", "tong gui", "tong chuyen",
        "tong tien", "tong thu chi", "tong chi tieu",  # "Tổng thu chi tháng này"
        "tat ca", "den gio", "lien quan",
        "thang nay gui", "thang nay chuyen", "thang nay tieu",
        "thang truoc gui", "thang truoc chuyen", "thang truoc tieu",
    ]),
    ("transfer", [
        "chuyen", "gui", "tra", "thanh toan", "nap",
        "transfer", "send",
        # Repeat-last-transfer phrasings — pair with a temporal entity so
        # the orchestrator pulls the previous tx as the implicit recipient.
        "lap lai", "repeat", "lai giao dich", "y nhu",
    ]),
    # NOTE: bare "hi" / "hey" are intentionally NOT here. Substring matching
    # would false-positive on Vietnamese words containing those letters
    # ("phát hiện" → "hi" inside "hiện", "khả nghi" → "hi" inside "nghi",
    # "chi tiêu" → "hi" inside "chi"). They're matched as whole words via
    # ``_SMALLTALK_HI_RE`` in classify() instead.
]


# Word-boundary smalltalk fallback — kept out of the Tier-2 substring loop
# so "hi" inside "hiện" / "nghi" / "chi" can't steal the routing from
# insights / history / transfer. Same word-boundary discipline applies
# to bare "chào" / "bye" — they would substring-match inside countless
# Vietnamese words ("chào" appears in "chào hỏi", "khẩu chào"; "bye"
# can hide in URLs).  Matched as whole words/anchored phrases here.
_SMALLTALK_HI_RE = re.compile(
    r"\b(?:hi|hey|bye)\b"
    r"|^\s*ch[àa]o\s*[!?.]?\s*$",   # bare "chào" / "chao" only
    re.IGNORECASE,
)


_LUU_STK_RE = re.compile(r"\bluu\s+[a-z][a-z\s]{0,40}?\s+stk\b", re.IGNORECASE)

# Date / temporal references that should route to history.
#
# Two flavours:
#
#  * Numeric anchors (tháng/năm/ngày/quý + digit, or bare DD/MM[/YYYY]
#    slash dates). These have to win against the Tier-3 ``\d`` fallback
#    that defaults any digit-bearing message to "transfer".
#
#  * Bare temporal words ("tuần này", "tuần trước", "hôm qua", "năm nay",
#    "năm ngoái", "đầu/cuối tháng/năm"). Without a digit they end up at
#    "unknown"; routing them to history makes the period filter Just Work.
#
# Conservative: any tier that matches first (transfer keyword, balance,
# add_contact, schedule, etc.) still wins because these checks run AFTER
# the keyword tiers and the smalltalk regex.
_HISTORY_DATE_RE = re.compile(
    r"\bth[áa]ng\s+\d{1,2}(?:[/\s]\d{2,4}|\s+n[ăa]m\s+\d{4})?\b"
    # "năm 2026"
    r"|\bn[ăa]m\s+\d{4}\b"
    # "quý 1" / "quý 2 năm 2026"
    r"|\bqu[ýy]\s+\d(?:\s+n[ăa]m\s+\d{4})?\b"
    # "ngày 15/5" / "ngày 15 tháng 5"
    r"|\bng[àa]y\s+\d{1,2}(?:[/\s]\d{1,2}(?:[/\s]\d{2,4})?|\s+th[áa]ng\s+\d{1,2})\b"
    # Bare slash-date "15/5" or "15/5/2026"
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"
    # "N tháng gần đây" / "N năm gần đây"
    r"|\b\d{1,2}\s+(?:th[áa]ng|n[ăa]m|tu[ầa]n|ng[àa]y)\s+(?:gần|gan|qua|trước|truoc)\b",
    re.IGNORECASE,
)

# Bare temporal phrases — no digit — that judges use as a standalone
# history query.
_HISTORY_TEMPORAL_RE = re.compile(
    r"\b(?:tuần|tuan)\s+(?:này|nay|trước|truoc|qua)\b"
    r"|\b(?:hôm|hom)\s+(?:nay|qua)\b"
    r"|\b(?:n[ăa]m)\s+(?:nay|ngoái|ngoai|trước|truoc)\b"
    r"|\b(?:đầu|dau|cuối|cuoi)\s+(?:tháng|thang|n[ăa]m|tu[ầa]n)\b",
    re.IGNORECASE,
)

# Branch / ATM intent — substring matchers in the Tier-1 list miss
# "chi nhánh BIDV gần nhất" / "atm acb o dau" because the bank token
# sits between the two anchor words. Regex-based pre-check handles
# arbitrary inter-token bank names and overrides the "gan nhat" /
# "lan gan nhat" history triggers below.
_ATM_FINDER_RE = re.compile(
    r"\b(?:atm|cay\s+atm|may\s+atm|chi\s+nhanh|phong\s+giao\s+dich)"
    r"\b[\w\s]{0,30}?"
    r"\b(?:gan|o\s+dau|o\s+gan|gan\s+day|gan\s+nhat|quanh\s+day|nao\s+gan)\b"
    r"|\btim\s+(?:atm|cay\s+atm|may\s+atm|chi\s+nhanh|phong\s+giao\s+dich)\b",
    re.IGNORECASE,
)


def classify(text: str) -> tuple[Intent, float]:
    folded = _ascii_fold(text)
    folded = re.sub(r"\s+", " ", folded)

    # Bare "lưu <person> STK <digits>" — informal add-contact pattern that
    # the Tier-1 keyword list can't capture without false-positiving on
    # "lưu ý" / "lưu lại". Pin it here, before any tier. CRITICAL: stops the
    # rule fallback misrouting an add-contact as a money-touching transfer
    # when both LLM providers are 429 (verifier audit 2026-06-06, H-1).
    if _LUU_STK_RE.search(folded):
        return "add_contact", 0.9

    # ATM / branch finder with a bank token in the middle — must run
    # before the Tier-1 loop so the history "gan nhat" / "lan gan nhat"
    # keywords don't steal "chi nhánh BIDV gần nhất" away.
    if _ATM_FINDER_RE.search(folded):
        return "atm_finder", 0.9

    # Tier 1 — first match wins, no scoring needed.
    for intent, kws in _HIGH:
        for kw in kws:
            if kw in folded:
                return intent, 0.85

    # Tier 2 — first match wins again, but scoring kept for telemetry.
    for intent, kws in _MED:
        for kw in kws:
            if kw in folded:
                return intent, 0.65

    # Tier 2.5 — bounded smalltalk for the English "hi"/"hey" greetings.
    # Done as a regex AFTER all substring-keyword tiers so it can't steal
    # routing from any intent. Matches whole words only.
    if _SMALLTALK_HI_RE.search(folded):
        return "smalltalk", 0.65

    # Tier 2.6 — temporal references (month/year/week/day/quarter) are
    # history queries, not transfers. Catches "tháng 5 năm 2026" /
    # "thang 5/2026" / "quý 1" / "ngày 15/5" / "tuần này" / "năm ngoái" /
    # "đầu tháng" before the Tier-3 bare-digit fallback steals them or the
    # query falls to "unknown". Runs AFTER Tier 1/2 so a real transfer or
    # schedule keyword wins first ("chuyển mẹ 2tr đầu tháng").
    if _HISTORY_DATE_RE.search(text) or _HISTORY_TEMPORAL_RE.search(text):
        return "history", 0.55

    # Tier 3 — bare digit means an unclassified transfer command.
    if re.search(r"\d", folded):
        return "transfer", 0.4

    return "unknown", 0.0
