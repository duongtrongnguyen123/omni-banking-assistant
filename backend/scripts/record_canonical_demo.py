"""Drive the canonical 4-knowledge-base demo and write the JSONL transcript.

Run once from the repo root::

    cd backend && .venv/bin/python scripts/record_canonical_demo.py

The output goes to ``docs/demos/canonical-demo.jsonl`` — checked in so
the live demo can be replayed by anyone via ``POST /api/demo/replay``.

Covers (from docs/demo-script.md):
  - KB1: alias resolution (mẹ → Lan)
  - KB4: history aggregation ("tháng này tiêu bao nhiêu")
  - KB5: safety wall (large amount, new recipient)
  - KB6: balance lookup

Keeps a tight, fast cadence — the whole sequence runs in <30s when
replayed at the default 800ms cadence.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make `app` importable when run from repo root or backend/.
HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))

# Belt-and-braces: keep this script offline-safe so it runs on the
# judging laptop without wifi too.
os.environ.setdefault("OMNI_SKIP_EMBED_BACKFILL", "1")
os.environ.setdefault("OMNI_DISABLE_SCHEDULE_TICK", "1")
os.environ.setdefault("OMNI_OFFLINE_DEMO", "1")

from app.config import get_settings  # noqa: E402
from app.services.orchestrator import handle_message  # noqa: E402

get_settings.cache_clear()

USER_ID = get_settings().demo_user_id


CANONICAL_SCRIPT: list[str] = [
    # KB1 — alias + amount: "chuyển cho mẹ 2 triệu"
    "Chuyển cho mẹ 2 triệu",
    "Xác nhận",
    "123456",
    # KB4 — history aggregation
    "Tháng này mình tiêu bao nhiêu rồi?",
    # KB5 — safety wall: large amount to a new recipient
    "Chuyển 50 triệu cho Hùng STK 9990001234",
    "Huỷ",
    # KB6 — balance
    "Số dư còn bao nhiêu?",
]


def main() -> None:
    out_path = BACKEND.parent / "docs" / "demos" / "canonical-demo.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    for text in CANONICAL_SCRIPT:
        resp = handle_message(USER_ID, text)
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "user": text,
            "omni": resp.model_dump(mode="json"),
        }
        lines.append(json.dumps(rec, ensure_ascii=False))

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(lines)} turns to {out_path}")


if __name__ == "__main__":
    main()
