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
    # receive_qr — "tạo QR / cho tôi QR / share QR nhận tiền". Goes
    # ABOVE my_account so "QR tài khoản của tôi" routes here (QR is
    # the actionable surface; the my_account intent below is for the
    # bare "STK của tôi" lookup).
    ("receive_qr", [
        "tao qr", "tạo qr", "ma qr", "mã qr",
        "cho qr", "share qr", "gửi qr", "gui qr",
        "qr nhan tien", "qr nhận tiền",
        "qr de nhan", "qr để nhận",
        "qr cua toi", "qr của tôi",
        "qr tk", "qr tài khoản",
        "vietqr",
    ]),
    # my_account — "STK của tôi", "số tài khoản của tôi". Read-only
    # inbound info so someone else can transfer in.
    #
    # CRITICAL guard: phrasings like "tài khoản của tôi" / "thông tin
    # tài khoản" are intentionally NOT here — they're already routed
    # to ``balance`` (which surfaces STK alongside balance). Only the
    # explicit STK / số tài khoản / TK lookups belong here so judges
    # who want JUST the account number get a focused response.
    ("my_account", [
        "stk cua toi", "stk của tôi",
        "stk cua minh", "stk của mình",
        "so tai khoan cua toi", "số tài khoản của tôi",
        "so tai khoan cua minh", "số tài khoản của mình",
        "tk cua toi", "tk của tôi", "tk cua minh", "tk của mình",
        "stk toi la gi", "stk tôi là gì",
        "stk minh la gi", "stk mình là gì",
        "stk de nhan", "stk để nhận",
        "stk nhan tien", "stk nhận tiền",
    ]),
    # recap — "tôi vừa nói gì", "lúc nãy tôi bảo bao nhiêu", "đang
    # chuyển bao nhiêu cho ai", "tóm tắt", "recap". Surfaces the active
    # draft instead of falling to history (which returned past tx and
    # missed the actual question the user asked). Pinned ABOVE history
    # so "lúc nãy ... bao nhiêu" doesn't get caught by history's
    # temporal regex.
    ("recap", [
        "vua noi gi", "vừa nói gì",
        "vua bao gi", "vừa bảo gì",
        "luc nay toi noi", "lúc nãy tôi nói",
        "luc nay toi bao", "lúc nãy tôi bảo",
        "luc nay bao nhieu", "lúc nãy bao nhiêu",
        "vua roi toi noi", "vừa rồi tôi nói",
        "dang chuyen bao nhieu", "đang chuyển bao nhiêu",
        "dang chuyen cho ai", "đang chuyển cho ai",
        "minh dang chuyen", "mình đang chuyển",
        "toi vua lam gi", "tôi vừa làm gì",
        "minh vua lam gi", "mình vừa làm gì",
        "tom tat", "tóm tắt",
        "recap",
        "nhac lai", "nhắc lại",
        "giao dich hien tai", "giao dịch hiện tại",
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
        # Casual "anything weird / interesting?" — common opening
        # judge probe. Pre-fix fell to "unknown" → guess-correction
        # page. Insights handler already surfaces anomalies + MoM, so
        # routing these there gives a sensible answer.
        "co gi la",                # "có gì lạ không"
        "thay gi la",              # "thấy gì lạ không"
        "co gi dang chu y",        # "có gì đáng chú ý không"
        "dang chu y",              # standalone
        "tieu hop ly",             # "tiêu hợp lý chưa"
        "check spending",          # English code-switch
        "spending pattern",        # English
    ]),
    # recurring (read) before schedule (create): "khoan dinh ky" / "tu dong
    # hang thang" are queries about existing patterns, not commands to make
    # a new one. Schedule keeps its imperative cues.
    #
    # Also includes schedule-management verbs (tạm dừng / huỷ / dừng / xem
    # lịch). Pre-fix, "tạm dừng lịch chuyển mẹ" contained "chuyen" and the
    # Tier-1 transfer keyword later in the list would match, opening a
    # transfer draft (with predicted ~500k amount) for the user who
    # actually wanted to PAUSE a recurring schedule. Critical safety bug
    # — one click away from sending money to mẹ. Routing to the recurring
    # (read) handler shows the user their schedules so they can act
    # safely instead of getting a transfer card.
    ("recurring", [
        "khoan dinh ky", "cac khoan dinh ky", "khoan tu dong",
        "khoan nao tu dong", "khoan nao dinh ky", "khoan nao tra deu",
        "khoan nao tra dinh ky", "khoan tra tu dong", "khoan tra dinh ky",
        "tra deu hang thang", "chi deu hang thang", "tra tu dong",
        "co khoan nao tra", "co khoan nao dinh ky",
        "liet ke lich", "xem lich tu dong", "lich tu dong",
        "liet ke khoan", "liet ke cac khoan",  # "liệt kê các khoản trả tự động"
        "khoan dinh ky cua toi", "khoan dinh ky cua minh",
        # Schedule-management — see comment above.
        "tam dung lich", "tam ngung lich",
        "huy lich",      # "huỷ lịch", "huỷ lịch chuyển mẹ"
        "dung lich",     # "dừng lịch"
        "ngung lich",
        "xem lich chuyen", "xem cac lich", "danh sach lich",
        "lich chuyen cua",
        # READ-side schedule list — judges asking "show me my
        # schedules". Pre-fix, "lịch chuyển tiền của mình" matched the
        # Tier-1 transfer keyword "chuyen" and opened a transfer draft
        # → one-click money send (same risk class as "tạm dừng lịch
        # chuyển mẹ" closed by PR #19). Recurring fires before
        # transfer, so these win.
        "lich chuyen tien",        # "lịch chuyển tiền của mình"
        "cac lich",                # "các lịch của mình"
        "lich cua minh",           # "lịch của mình"
        "lich cua toi",            # "lịch của tôi"
        "co lich nao",             # "có lịch nào đang chạy"
        "lich nao dang",           # "lịch nào đang chạy"
        "lich sap toi",            # "lịch sắp tới"
        "lich sap den",            # "lịch sắp đến"
        "lich nao sap",            # "lịch nào sắp đến / sắp tới"
        "lich tu dong cua",        # "lịch tự động của mình"
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
        # Account-info queries. The balance handler already surfaces
        # per-account balances + total; these phrasings used to fall to
        # Tier-2 "bao nhieu" → history aggregate.
        "tai khoan chinh",          # "tài khoản chính"
        "tai khoan tiet kiem",      # "tài khoản tiết kiệm"
        "tai khoan dich vu",        # "tài khoản dịch vụ"
        "bao nhieu tai khoan",      # "có bao nhiêu tài khoản"
        "tai khoan cua minh",       # "tài khoản của mình"
        "tai khoan cua toi",        # "tài khoản của tôi"
        "cac tai khoan",            # "các tài khoản"
        "tong tai san",             # "tổng tài sản"
        "kiem tra tai khoan",       # "kiểm tra tài khoản (đi)"
        "thong tin tai khoan",      # "thông tin tài khoản"
        "check balance",            # English fallback judges sometimes use
        "check so du",              # "check số dư" — code-switching
        "show balance",
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
        # Transaction-search phrasings. Without these, "giao dịch nào
        # lớn nhất" falls to "unknown" and "tìm giao dịch trên 1 triệu"
        # falls to Tier-3 transfer because of the `\d`. All
        # high-precision: "giao dich" only appears in retrospective
        # queries.
        "tim giao dich",            # "tìm giao dịch"
        "giao dich lon",            # "giao dịch lớn nhất / lớn nhất"
        "giao dich nho",            # "giao dịch nhỏ nhất"
        "giao dich nao",            # "giao dịch nào ..."
        "giao dich tren",           # "giao dịch trên X triệu"
        "giao dich duoi",           # "giao dịch dưới X triệu"
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
        # "How are you?" — canonical opening smalltalk that pre-fix
        # fell to "unknown" because no keyword matched.
        "khoe khong", "khỏe không", "co khoe khong", "có khoẻ không",
        "the nao roi", "thế nào rồi",
        # Help-shaped smalltalk that's NOT a transfer / data query —
        # "Omni có thể giúp gì", "Omni làm được gì". Routed to
        # smalltalk so the static help block fires (handler picks the
        # help reply for these).
        "co the giup gi", "có thể giúp gì", "lam duoc gi", "làm được gì",
        "biet gi ve", "biết gì về",
        # Authorship / about — "Ai làm ra Omni?", "Omni của ai?"
        "ai lam ra", "ai làm ra", "ai tao ra", "ai tạo ra",
        "do ai phat trien", "do ai phát triển",
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

# Category-shaped retrospective queries. Catches "ăn uống tháng này" /
# "mua sắm tuần trước" / "cafe tháng này" / "tiêu ăn uống bao nhiêu" —
# all clear history-with-category queries where the category is the
# subject and a temporal/aggregation cue follows. Without this they
# fall to "unknown" because no Tier-1/2 keyword fires.
#
# Word list deliberately small + high-precision; categories that share
# tokens with intents ("tiền nhà" inside "tiền" etc.) are excluded
# unless paired with the time anchor.
_CATEGORY_HISTORY_RE = re.compile(
    r"\b(?:"
    r"ăn\s+uống|an\s+uong"
    r"|ăn\s+sáng|ăn\s+trưa|ăn\s+tối|an\s+sang|an\s+trua|an\s+toi"
    r"|mua\s+sắm|mua\s+sam"
    r"|giải\s+trí|giai\s+tri"
    r"|cafe|cà\s+phê|ca\s+phe|trà\s+sữa|tra\s+sua"
    r"|shopping"
    r"|xăng|xang|grab|taxi"
    r"|tiền\s+điện|tien\s+dien|tiền\s+nước|tien\s+nuoc|điện\s+nước|dien\s+nuoc"
    r"|tiền\s+nhà|tien\s+nha"
    r"|tiền\s+học|tien\s+hoc|học\s+phí|hoc\s+phi"
    r")\b"
    # Anchored to either a leading "tiêu/chi" verb OR a trailing
    # temporal / aggregation / amount-range cue. This keeps "tiền nhà
    # tôi vừa trả" (a transfer reference) out of history while still
    # catching the retrospective forms judges actually type.
    r"(?:"
    r"\s+(?:tháng|thang|tuần|tuan|năm|nam|hôm|hom|ngày|ngay|bao\s+nhi|gần\s+đây|gan\s+day)"
    r"|.*\b(?:bao\s+nhi|tổng|tong|trung\s+bình|trung\s+binh)\b"
    # Amount-range filters — "ăn uống dưới 200k" / "shopping trên 1tr" /
    # "cà phê từ 50k đến 200k". These are history filter queries, not
    # transfer commands. Pre-fix the Tier-3 bare-digit fallback ate
    # them as transfer + missing_recipient.
    r"|\s+(?:dưới|duoi|trên|tren|nhỏ\s+hơn|nho\s+hon|lớn\s+hơn|lon\s+hon|từ|tu)\b"
    r")",
    re.IGNORECASE,
)
# Also catches the leading "tiêu/chi <category>" form without time
# cue: "tiêu ăn uống", "chi giải trí".
_CATEGORY_LEAD_RE = re.compile(
    r"^(?:tiêu|chi|tieu)\s+"
    r"(?:ăn\s+uống|an\s+uong|mua\s+sắm|mua\s+sam|giải\s+trí|giai\s+tri"
    r"|cafe|cà\s+phê|ca\s+phe|shopping|xăng|xang|grab|taxi"
    r"|tiền\s+điện|tien\s+dien|tiền\s+nước|tien\s+nuoc"
    r"|tiền\s+nhà|tien\s+nha)",
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


# When a Tier-1 smalltalk keyword fires inside a longer message that ALSO
# carries an imperative ("Chào Omni, chuyển mẹ 2tr nhé" / "Hello chuyển
# bố 500k"), the greeting must not pre-empt the command — the user thinks
# they queued a transfer and walks away. This guard detects clear
# command-verb / data-query cues; when present, the Tier-1 smalltalk
# match is suppressed and the loop falls through to transfer/history/etc.
_COMMAND_VERB_RE = re.compile(
    r"\b(?:chuyen|gui|tra|nap|thanh\s+toan|"
    r"so\s+du|balance|"
    r"dat\s+lich|len\s+lich|lich\s+su|giao\s+dich|"
    r"atm|chi\s+nhanh|"
    r"ngan\s+sach|tiet\s+kiem)\b",
    re.IGNORECASE,
)


# Negation / hypothetical / modal guards. Messages like "đừng chuyển mẹ
# 2tr" / "giả sử chuyển mẹ 5tr" / "thử chuyển mẹ 1k xem được không"
# still substring-match the Tier-1 ``chuyen`` transfer keyword, open a
# one-click confirmable draft, and "thử ... 1k" becomes a real 1.000đ
# transfer (round-6 S1+S2+S3). Detect these markers BEFORE Tier-1
# dispatch and short-circuit to "unknown" so the chat asks for clarity
# instead of staging money.
_NEGATION_OR_HYPOTHETICAL_RE = re.compile(
    r"\b(?:"
    # Negation: "đừng chuyển" / "không muốn chuyển" / "không chuyển nữa"
    r"đừng\s+(?:chuyển|chuyen|gửi|gui|trả|tra|nạp|nap)"
    r"|dung\s+(?:chuyen|gui|tra|nap)"
    r"|không\s+(?:muốn|muon|nên|nen|cần|can|định|dinh)\s+(?:chuyển|chuyen|gửi|gui)"
    r"|khong\s+(?:muon|nen|can|dinh)\s+(?:chuyen|gui)"
    r"|không\s+(?:chuyển|chuyen|gửi|gui)\s+(?:nữa|nua)"
    r"|khong\s+(?:chuyen|gui)\s+nua"
    r"|hủy\s+ý\s+định|huy\s+y\s+dinh"
    # Hypothetical / irrealis: "giả sử ..." / "nếu chuyển ..." /
    # "thử chuyển ... xem"
    r"|giả\s+sử|gia\s+su"
    r"|nếu\s+(?:chuyển|chuyen|gửi|gui)"
    r"|neu\s+(?:chuyen|gui)"
    r"|thử\s+(?:chuyển|chuyen|gửi|gui)"
    r"|thu\s+(?:chuyen|gui)"
    r")\b",
    re.IGNORECASE,
)


_TERSE_SHORTCUTS: dict[str, "Intent"] = {
    # User feedback: judges type 1-2 word commands on the phone. Pin the
    # most common shortcuts so they always route to the right intent
    # regardless of Tier-1 substring noise. Must be exact-match against
    # the folded + stripped input — bare "qr" inside "QR mẹ" doesn't fire.
    "qr": "receive_qr",
    "qr code": "receive_qr",
    "stk": "my_account",
    "tk": "my_account",
    "lich su": "history",
    "lịch sử": "history",
    "so du": "balance",
    "số dư": "balance",
    "atm": "atm_finder",
    "ngan sach": "budget_status",
    "ngân sách": "budget_status",
    "muc tieu": "goal_status",
    "mục tiêu": "goal_status",
    "dinh ky": "recurring",
    "định kỳ": "recurring",
}


def classify(text: str) -> tuple[Intent, float]:
    folded = _ascii_fold(text)
    folded = re.sub(r"\s+", " ", folded)

    # Terse single-word shortcuts — judge feedback "QR" / "STK" / "lịch
    # sử" / "số dư" alone should route directly without depending on
    # Tier-1 substring rules.
    stripped = folded.strip(" ?.!,:;")
    if stripped in _TERSE_SHORTCUTS:
        return _TERSE_SHORTCUTS[stripped], 0.95
    # Also match the original (non-folded) form for VN-diacritic shortcuts.
    stripped_orig = text.strip(" ?.!,:;").lower()
    if stripped_orig in _TERSE_SHORTCUTS:
        return _TERSE_SHORTCUTS[stripped_orig], 0.95

    # Negation / hypothetical / modal guard — before any Tier-1 match.
    # Stops "đừng chuyển mẹ 2tr" / "giả sử chuyển mẹ 5tr" / "thử chuyển
    # mẹ 1k xem được không" from opening a real transfer draft. See
    # _NEGATION_OR_HYPOTHETICAL_RE above.
    if _NEGATION_OR_HYPOTHETICAL_RE.search(text):
        return "unknown", 0.85

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

    # Category-shaped retrospective queries — "ăn uống tháng này" /
    # "mua sắm tuần trước" / "tiêu giải trí bao nhiêu". Must run before
    # Tier-1 so the "tiêu" / "chi" inside transfer keywords don't
    # eat the routing, and before Tier-2 "bao nhieu" → history default
    # (which produces a generic month aggregate without the category
    # filter the user actually asked for).
    if _CATEGORY_HISTORY_RE.search(text) or _CATEGORY_LEAD_RE.search(text):
        return "history", 0.75

    # Tier 1 — first match wins, no scoring needed.
    # Exception: when smalltalk matches inside a longer message that
    # also carries a command verb ("Chào Omni, chuyển mẹ 2tr"), defer
    # to the imperative — see _COMMAND_VERB_RE above. Breaking the
    # inner kw loop lets the outer intent loop move on to the next
    # intent (transfer below smalltalk in _HIGH).
    has_command_verb = bool(_COMMAND_VERB_RE.search(folded))
    for intent, kws in _HIGH:
        for kw in kws:
            if kw in folded:
                if intent == "smalltalk" and has_command_verb:
                    break
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
