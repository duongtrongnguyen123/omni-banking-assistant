"""File-based audit log for safety decisions.

SBV (State Bank of Vietnam) requires banks to retain a 5-year immutable
audit trail of compliance-relevant decisions. Real production needs
write-once storage (S3 Object Lock, ledger DB); this module is the
hackathon-scope minimum that gives the team something to extend:

* JSONL append, rotated daily at midnight local.
* `audit.log` in ``OMNI_AUDIT_DIR`` (defaults to ``backend/logs/``).
* Fail-open: any disk error logs to stderr and continues — never blocks
  the live transfer path.

Records every:

* Safety flag emission (code, severity, user_id, draft_id, message)
* Transfer confirmation (executed amount, recipient, source account)
* OTP request / verify (mock OTP only; never log the OTP value itself)
* Cancel events

Each record is a single JSON line with ISO-8601 UTC timestamp and a
``kind`` discriminator so a downstream pipeline can split by type.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_FH = None  # type: ignore[var-annotated]
_FH_DAY: Optional[str] = None


def _audit_dir() -> Path:
    return Path(os.environ.get(
        "OMNI_AUDIT_DIR",
        str(Path(__file__).resolve().parent.parent.parent / "logs"),
    ))


def _ensure_writer() -> Optional[Any]:
    """Open / rotate the file handle. Returns None on disk error so the
    caller can fail-open. Daily rotation by UTC date."""
    global _FH, _FH_DAY
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _FH is not None and _FH_DAY == today:
        return _FH
    try:
        d = _audit_dir()
        d.mkdir(parents=True, exist_ok=True)
        if _FH is not None:
            try:
                _FH.close()
            except Exception:
                pass
        _FH = (d / f"audit-{today}.log").open("a", encoding="utf-8")
        _FH_DAY = today
        return _FH
    except Exception as e:
        print(f"[audit_log] writer init failed: {e}", file=sys.stderr)
        _FH = None
        return None


def record(kind: str, **fields: Any) -> None:
    """Append one audit row. Synchronous + locked but cheap (<1ms typical)."""
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        **fields,
    }
    line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
    with _LOCK:
        fh = _ensure_writer()
        if fh is None:
            return
        try:
            fh.write(line)
            fh.flush()
        except Exception as e:  # pragma: no cover — defensive
            print(f"[audit_log] write failed: {e}", file=sys.stderr)


def record_safety_decision(
    user_id: str,
    draft_id: Optional[str],
    flags: list,
) -> None:
    """Audit every safety evaluation. ``flags`` is list[SafetyFlag] but
    we avoid the import to keep this module dep-free."""
    if not flags:
        return
    record(
        "safety_decision",
        user_id=user_id,
        draft_id=draft_id,
        flags=[
            {"code": f.code, "severity": f.severity, "message": f.message[:200]}
            for f in flags
        ],
    )


def record_transfer_executed(
    user_id: str,
    draft_id: str,
    amount: int,
    recipient_name: str,
    source_account_id: str,
    category: Optional[str] = None,
) -> None:
    record(
        "transfer_executed",
        user_id=user_id,
        draft_id=draft_id,
        amount=amount,
        recipient_name=recipient_name,
        source_account_id=source_account_id,
        category=category,
    )


def record_otp(
    user_id: str,
    draft_id: str,
    action: str,  # "requested" | "verified" | "failed"
) -> None:
    record("otp", user_id=user_id, draft_id=draft_id, action=action)


def record_cancel(user_id: str, draft_id: str) -> None:
    record("cancel", user_id=user_id, draft_id=draft_id)
