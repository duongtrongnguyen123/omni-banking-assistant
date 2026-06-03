#!/usr/bin/env python3
"""Transform the supplied banking workbook into Omni-friendly synthetic data."""

from __future__ import annotations

import argparse
import csv
import hashlib
import heapq
import json
import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree as ET
from zipfile import ZipFile

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
TZ_VIETNAM = timezone(timedelta(hours=7))
SOURCE_COLUMNS = ("CIF_NO", "NOTE", "TRAN_DATE", "AMOUNT")
DEMO_USER_ID = "u_an"
DEMO_USER_NAME = "Nguyễn Hoàng An"

BANKS = (
    "Vietcombank",
    "MB Bank",
    "Techcombank",
    "ACB",
    "BIDV",
    "VietinBank",
    "VPBank",
    "TPBank",
    "Sacombank",
    "HDBank",
)

FEATURED_CONTACTS = (
    ("Nguyễn Thị Lan", ("mẹ", "me", "má", "mom"), "Mẹ", "Vietcombank", True, True),
    ("Nguyễn Văn Minh", ("minh", "anh minh"), "Bạn", "MB Bank", True, True),
    ("Trần Hoàng Minh", ("minh", "hoàng minh"), "Đồng nghiệp", "Techcombank", True, False),
    ("Trần Quốc Hùng", ("hùng",), None, "ACB", False, False),
    ("Lê Thị Thảo", ("thảo", "em thảo"), "Bạn", "Techcombank", True, False),
)

FAMILY_NAMES = (
    "Nguyễn", "Trần", "Lê", "Phạm", "Hoàng", "Huỳnh", "Phan", "Vũ",
    "Võ", "Đặng", "Bùi", "Đỗ", "Hồ", "Ngô", "Dương", "Lý",
)
MIDDLE_NAMES = (
    "Văn", "Thị", "Minh", "Hoàng", "Ngọc", "Thanh", "Quang", "Gia",
    "Đức", "Hải", "Thu", "Anh", "Khánh", "Phương", "Tuấn", "Mai",
)
GIVEN_NAMES = (
    "An", "Anh", "Bảo", "Bình", "Châu", "Chi", "Cường", "Dũng",
    "Duy", "Giang", "Hà", "Hạnh", "Hiếu", "Hoa", "Huy", "Hương",
    "Khang", "Khôi", "Lan", "Linh", "Long", "Mai", "Minh", "My",
    "Nam", "Nga", "Ngân", "Nhung", "Phúc", "Phương", "Quân", "Quang",
    "Sơn", "Tâm", "Thảo", "Trang", "Trinh", "Trung", "Tú", "Tuấn",
    "Vy", "Yến",
)

NOTE_RULES = (
    ("debt", "Trả nợ thấu chi", ("tra no thau chi",)),
    ("debt", "Trả nợ thẻ", ("tra no the", "tt the", "thanh toan the tin dung")),
    ("debt", "Trả góp", ("dang ky tra gop", "tra gop")),
    ("bills", "Tiền điện", ("tien dien",)),
    ("bills", "Phí quản lý", ("phi quan ly",)),
    ("bills", "Tiền gửi xe", ("tien gui xe",)),
    ("family", "Tiền sinh hoạt", ("tien sinh hoat",)),
    ("family", "Gia đình gửi tiền", ("gia dinh gui tien",)),
    ("family", "Báo nuôi", ("bao nuoi",)),
    ("income", "Ứng lương", ("ung luong",)),
    ("income", "Phụ cấp", ("phu cap",)),
    ("income", "Trợ cấp", ("tro cap",)),
    ("shopping", "Thanh toán đơn hàng", ("ck don hang",)),
    ("shopping", "Mua sắm", ("mua ", "tra tien ao")),
    ("charity", "Ủng hộ", ("donate",)),
    ("transfer", "Chuyển tiền", ("chuyen tien", "ck", "t ck")),
)


@dataclass(frozen=True)
class Counterparty:
    source_cif_no: str
    contact_id: str
    counterparty_name: str
    bank: str
    account_number: str
    account_masked: str
    aliases: tuple[str, ...]
    label: str | None
    verified: bool
    frequent: bool


@dataclass(frozen=True)
class SourceTransaction:
    source_row: int
    source_cif_no: str
    note: str
    transaction_at: datetime
    signed_amount_vnd: int


def fold_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    folded = "".join(char for char in normalized if not unicodedata.combining(char))
    return " ".join(folded.lower().replace("đ", "d").strip().split())


def classify_note(note: str) -> tuple[str, str]:
    folded = fold_text(note)
    for category, description, fragments in NOTE_RULES:
        if any(fragment in folded for fragment in fragments):
            return category, description
    return "other", note.strip() or "Khác"


def load_shared_strings(workbook: ZipFile) -> list[str]:
    strings: list[str] = []
    with workbook.open("xl/sharedStrings.xml") as source:
        for _, elem in ET.iterparse(source, events=("end",)):
            if elem.tag == f"{{{MAIN_NS}}}si":
                strings.append(
                    "".join(node.text or "" for node in elem.iter(f"{{{MAIN_NS}}}t"))
                )
                elem.clear()
    return strings


def iter_source_transactions(
    workbook: ZipFile, shared_strings: list[str]
) -> Iterator[SourceTransaction]:
    with workbook.open("xl/worksheets/sheet1.xml") as source:
        for _, elem in ET.iterparse(source, events=("end",)):
            if elem.tag != f"{{{MAIN_NS}}}row":
                continue
            row_number = int(elem.attrib.get("r", "0"))
            cell = elem.find(f"{{{MAIN_NS}}}c")
            value = cell.find(f"{{{MAIN_NS}}}v") if cell is not None else None
            raw = ""
            if cell is not None and value is not None and value.text is not None:
                raw = (
                    shared_strings[int(value.text)]
                    if cell.attrib.get("t") == "s"
                    else value.text
                )
            elem.clear()
            columns = next(csv.reader([raw]))
            if row_number == 1:
                if tuple(columns) != SOURCE_COLUMNS:
                    raise ValueError(f"Unexpected source columns: {columns!r}")
                continue
            if len(columns) != len(SOURCE_COLUMNS):
                raise ValueError(f"Malformed CSV row {row_number}: {columns!r}")
            cif_no, note, transaction_at, signed_amount = columns
            yield SourceTransaction(
                source_row=row_number,
                source_cif_no=cif_no,
                note=note,
                transaction_at=datetime.strptime(transaction_at, "%m/%d/%Y %H:%M"),
                signed_amount_vnd=int(signed_amount),
            )


def stable_number(seed: str) -> int:
    return int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16)


def synthetic_names() -> Iterator[str]:
    for family_name in FAMILY_NAMES:
        for middle_name in MIDDLE_NAMES:
            for given_name in GIVEN_NAMES:
                yield f"{family_name} {middle_name} {given_name}"


def create_counterparties(
    cif_counts: Counter[str],
) -> dict[str, Counterparty]:
    sorted_cifs = sorted(cif_counts)
    reserved_names = {item[0] for item in FEATURED_CONTACTS}
    names = (name for name in synthetic_names() if name not in reserved_names)
    frequent_cutoff = sorted(cif_counts.values(), reverse=True)[max(len(cif_counts) // 5 - 1, 0)]
    counterparties: dict[str, Counterparty] = {}

    for index, cif_no in enumerate(sorted_cifs):
        stable = stable_number(cif_no)
        if index < len(FEATURED_CONTACTS):
            name, aliases, label, bank, verified, frequent = FEATURED_CONTACTS[index]
        else:
            name = next(names)
            aliases = ()
            label = None
            bank = BANKS[stable % len(BANKS)]
            verified = stable % 5 != 0
            frequent = cif_counts[cif_no] >= frequent_cutoff
        account_number = "9704" + f"{stable % 100_000_000:08d}"
        counterparties[cif_no] = Counterparty(
            source_cif_no=cif_no,
            contact_id=f"c_{cif_no}",
            counterparty_name=name,
            bank=bank,
            account_number=account_number,
            account_masked="*" + account_number[-3:],
            aliases=aliases,
            label=label,
            verified=verified,
            frequent=frequent,
        )
    return counterparties


def direction_for(signed_amount: int) -> str:
    if signed_amount < 0:
        return "outgoing"
    if signed_amount > 0:
        return "incoming"
    return "neutral"


def enriched_row(
    transaction: SourceTransaction, counterparty: Counterparty
) -> dict[str, str | int]:
    direction = direction_for(transaction.signed_amount_vnd)
    category, normalized_note = classify_note(transaction.note)
    if direction == "incoming":
        sender_id, sender_name = counterparty.source_cif_no, counterparty.counterparty_name
        receiver_id, receiver_name = DEMO_USER_ID, DEMO_USER_NAME
    else:
        sender_id, sender_name = DEMO_USER_ID, DEMO_USER_NAME
        receiver_id, receiver_name = counterparty.source_cif_no, counterparty.counterparty_name
    return {
        "transaction_id": f"sim_{transaction.source_row - 1:06d}",
        "source_cif_no": counterparty.source_cif_no,
        "counterparty_name": counterparty.counterparty_name,
        "counterparty_bank": counterparty.bank,
        "counterparty_account_number": counterparty.account_number,
        "direction": direction,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "receiver_id": receiver_id,
        "receiver_name": receiver_name,
        "signed_amount_vnd": transaction.signed_amount_vnd,
        "amount_vnd": abs(transaction.signed_amount_vnd),
        "note_raw": transaction.note,
        "note_normalized": normalized_note,
        "category": category,
        "transaction_at": transaction.transaction_at.replace(tzinfo=TZ_VIETNAM).isoformat(),
        "status": "completed",
    }


def push_latest(
    heap: list[tuple[datetime, int, SourceTransaction]],
    item: SourceTransaction,
    limit: int,
) -> None:
    candidate = (item.transaction_at, item.source_row, item)
    if len(heap) < limit:
        heapq.heappush(heap, candidate)
    elif candidate[:2] > heap[0][:2]:
        heapq.heapreplace(heap, candidate)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def profile_source(
    workbook: ZipFile, shared_strings: list[str]
) -> tuple[dict[str, object], Counter[str], datetime]:
    cif_counts: Counter[str] = Counter()
    note_counts: Counter[str] = Counter()
    direction_counts: Counter[str] = Counter()
    month_counts: Counter[str] = Counter()
    minimum_at: datetime | None = None
    maximum_at: datetime | None = None
    minimum_amount: int | None = None
    maximum_amount: int | None = None
    records = 0

    for transaction in iter_source_transactions(workbook, shared_strings):
        records += 1
        cif_counts[transaction.source_cif_no] += 1
        note_counts[fold_text(transaction.note)] += 1
        direction_counts[direction_for(transaction.signed_amount_vnd)] += 1
        month_counts[transaction.transaction_at.strftime("%Y-%m")] += 1
        minimum_at = min(minimum_at, transaction.transaction_at) if minimum_at else transaction.transaction_at
        maximum_at = max(maximum_at, transaction.transaction_at) if maximum_at else transaction.transaction_at
        minimum_amount = min(minimum_amount, transaction.signed_amount_vnd) if minimum_amount is not None else transaction.signed_amount_vnd
        maximum_amount = max(maximum_amount, transaction.signed_amount_vnd) if maximum_amount is not None else transaction.signed_amount_vnd

    if maximum_at is None:
        raise ValueError("The source workbook has no transactions")
    profile = {
        "source_records": records,
        "unique_counterparties": len(cif_counts),
        "date_range": {
            "from": minimum_at.isoformat(),
            "to": maximum_at.isoformat(),
        },
        "signed_amount_range_vnd": {
            "minimum": minimum_amount,
            "maximum": maximum_amount,
        },
        "direction_counts": dict(sorted(direction_counts.items())),
        "month_counts": dict(sorted(month_counts.items())),
        "top_notes": [
            {"note": note, "count": count}
            for note, count in note_counts.most_common(30)
        ],
    }
    return profile, cif_counts, maximum_at


def write_counterparties(path: Path, counterparties: dict[str, Counterparty]) -> None:
    fieldnames = (
        "source_cif_no", "contact_id", "counterparty_name", "bank",
        "account_number", "account_masked", "aliases", "label", "verified", "frequent",
    )
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for counterparty in counterparties.values():
            row = asdict(counterparty)
            row["aliases"] = "|".join(counterparty.aliases)
            writer.writerow(row)


def write_enriched_and_collect_demo(
    workbook: ZipFile,
    shared_strings: list[str],
    counterparties: dict[str, Counterparty],
    output_path: Path,
    demo_limit: int,
    featured_limit: int,
) -> list[SourceTransaction]:
    fieldnames = tuple(enriched_row(
        SourceTransaction(1, "sample", "", datetime(2000, 1, 1), 0),
        Counterparty("sample", "sample", "", "", "", "", (), None, False, False),
    ))
    latest: list[tuple[datetime, int, SourceTransaction]] = []
    featured_cifs = set(sorted(counterparties)[:len(FEATURED_CONTACTS)])
    featured: dict[str, list[tuple[datetime, int, SourceTransaction]]] = {
        cif_no: [] for cif_no in featured_cifs
    }

    with output_path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for transaction in iter_source_transactions(workbook, shared_strings):
            counterparty = counterparties[transaction.source_cif_no]
            writer.writerow(enriched_row(transaction, counterparty))
            if transaction.signed_amount_vnd >= 0:
                continue
            push_latest(latest, transaction, demo_limit)
            if transaction.source_cif_no in featured:
                push_latest(featured[transaction.source_cif_no], transaction, featured_limit)

    selected = {item.source_row: item for _, _, item in latest}
    for heap in featured.values():
        selected.update({item.source_row: item for _, _, item in heap})
    return sorted(selected.values(), key=lambda item: (item.transaction_at, item.source_row))


def write_demo(
    output_dir: Path,
    counterparties: dict[str, Counterparty],
    transactions: list[SourceTransaction],
    source_maximum_at: datetime,
    demo_end_date: date,
) -> None:
    demo_dir = output_dir / "demo"
    demo_dir.mkdir(parents=True, exist_ok=True)
    shift = timedelta(days=(demo_end_date - source_maximum_at.date()).days)
    contacts = [
        {
            "id": item.contact_id,
            "owner_id": DEMO_USER_ID,
            "display_name": item.counterparty_name,
            "bank": item.bank,
            "account_number": item.account_number,
            "account_masked": item.account_masked,
            "aliases": list(item.aliases),
            "label": item.label,
            "verified": item.verified,
            "frequent": item.frequent,
        }
        for item in counterparties.values()
    ]
    demo_transactions = []
    for item in transactions:
        category, normalized_note = classify_note(item.note)
        demo_transactions.append(
            {
                "id": f"sim_{item.source_row - 1:06d}",
                "owner_id": DEMO_USER_ID,
                "contact_id": counterparties[item.source_cif_no].contact_id,
                "amount": abs(item.signed_amount_vnd),
                "description": normalized_note,
                "category": category,
                "status": "completed",
                "created_at": (item.transaction_at + shift).replace(tzinfo=TZ_VIETNAM).isoformat(),
            }
        )

    write_json(demo_dir / "metadata.json", {
        "description": "Backend-compatible outgoing sample derived from banking_simulation_6M",
        "demo_end_date": demo_end_date.isoformat(),
        "source_maximum_at": source_maximum_at.isoformat(),
        "shift_days": shift.days,
        "contacts": len(contacts),
        "outgoing_transactions": len(demo_transactions),
    })
    write_json(demo_dir / "users.json", [{
        "id": DEMO_USER_ID,
        "display_name": DEMO_USER_NAME,
        "phone": "0912345678",
        "accounts": [
            {
                "id": "acc_an_main",
                "bank": "Omni Bank",
                "number": "1234567890",
                "balance": 24_350_000,
                "currency": "VND",
                "primary": True,
            },
            {
                "id": "acc_an_savings",
                "bank": "Omni Bank",
                "number": "1234567891",
                "balance": 50_000_000,
                "currency": "VND",
                "primary": False,
            },
        ],
    }])
    write_json(demo_dir / "contacts.json", contacts)
    write_json(demo_dir / "transactions.json", demo_transactions)
    write_json(demo_dir / "schedules.json", [])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path, help="Source XLSX workbook")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--demo-limit", type=int, default=1_500)
    parser.add_argument("--featured-limit", type=int, default=80)
    parser.add_argument(
        "--demo-end-date",
        type=date.fromisoformat,
        default=date.today() - timedelta(days=1),
        help="Shift the demo subset so its latest transaction falls on this date",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    generated_dir = output_dir / "generated"
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_dir.mkdir(parents=True, exist_ok=True)

    with ZipFile(args.input) as workbook:
        shared_strings = load_shared_strings(workbook)
        profile, cif_counts, source_maximum_at = profile_source(workbook, shared_strings)
        counterparties = create_counterparties(cif_counts)
        write_counterparties(output_dir / "counterparties.csv", counterparties)
        demo_transactions = write_enriched_and_collect_demo(
            workbook=workbook,
            shared_strings=shared_strings,
            counterparties=counterparties,
            output_path=generated_dir / "transactions_enriched_6m.csv",
            demo_limit=args.demo_limit,
            featured_limit=args.featured_limit,
        )
        profile["generated_at"] = datetime.now(tz=TZ_VIETNAM).isoformat(timespec="seconds")
        profile["demo"] = {
            "end_date": args.demo_end_date.isoformat(),
            "outgoing_transactions": len(demo_transactions),
        }
        write_json(output_dir / "source_profile.json", profile)
        write_demo(
            output_dir=output_dir,
            counterparties=counterparties,
            transactions=demo_transactions,
            source_maximum_at=source_maximum_at,
            demo_end_date=args.demo_end_date,
        )

    print(f"Wrote {profile['source_records']:,} enriched transactions")
    print(f"Wrote {len(counterparties):,} counterparties")
    print(f"Wrote {len(demo_transactions):,} backend demo transactions")
    print(f"Output: {output_dir}")


if __name__ == "__main__":
    main()
