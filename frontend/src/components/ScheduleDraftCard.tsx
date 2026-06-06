import { useEffect, useRef, useState } from "react";
import type { ScheduleDraft } from "../types";
import { formatVND, formatDate } from "../format";

interface Props {
  draft: ScheduleDraft;
  onConfirm: (otp: string, sourceAccountId?: string) => void;
  onCancel: () => void;
  disabled?: boolean;
  actionable?: boolean;
}

export const ScheduleDraftCard = ({
  draft,
  onConfirm,
  onCancel,
  disabled,
  actionable = true,
}: Props) => {
  const [otpOpen, setOtpOpen] = useState(false);
  const [otp, setOtp] = useState("");
  const [sourceAccountId, setSourceAccountId] = useState(
    draft.source_account_id ?? draft.source_accounts[0]?.id ?? "",
  );
  const cleanOtp = otp.replace(/\D/g, "").slice(0, 6);

  // Local inflight lock: the parent's `disabled` flag only flips while
  // App.tsx's main send() is busy, NOT for the schedule confirm/cancel
  // calls themselves (which go through sendDraftAction). Without this
  // guard, a double-click on "Xác minh & tạo lịch" fires two parallel
  // confirmSchedule requests and double-bumps onDraftResolved.
  const [submitting, setSubmitting] = useState(false);
  const prevDisabled = useRef<boolean | undefined>(disabled);
  useEffect(() => {
    // Reset the local lock once the parent's busy state clears, so
    // the user can retry on error.
    if (prevDisabled.current && !disabled) {
      setSubmitting(false);
    }
    prevDisabled.current = disabled;
  }, [disabled]);
  // Also release the lock when the card flips to non-actionable
  // (i.e. the action completed and a new draft / no draft replaced it).
  useEffect(() => {
    if (!actionable) setSubmitting(false);
  }, [actionable]);

  const locked = submitting || !!disabled;

  const handleConfirm = () => {
    if (!otpOpen) {
      setOtpOpen(true);
      return;
    }
    if (submitting) return;
    setSubmitting(true);
    onConfirm(cleanOtp, sourceAccountId || undefined);
  };

  const handleCancel = () => {
    if (submitting) return;
    setSubmitting(true);
    onCancel();
  };

  return (
    <div className={`tx-card ${!actionable ? "tx-card--done" : ""}`}>
      <div className="tx-card__label">LỊCH ĐỊNH KỲ MỚI</div>
      <div className="tx-card__amount">
        <div className="tx-card__amount-value">{formatVND(draft.amount)}</div>
      </div>
      <div className="tx-row">
        <span className="tx-row__label">Người nhận</span>
        <div className="tx-row__value">
          <div className="tx-recipient__name">{draft.recipient.display_name}</div>
          <div className="tx-recipient__meta">
            {draft.recipient.bank} · {draft.recipient.account_masked}
          </div>
        </div>
      </div>
      <div className="tx-row">
        <span className="tx-row__label">Tài khoản nguồn</span>
        <div className="tx-row__value">
          <select
            className="account-select"
            value={sourceAccountId}
            onChange={(e) => setSourceAccountId(e.target.value)}
            disabled={locked || !actionable}
          >
            {draft.source_accounts.map((account) => (
              <option key={account.id} value={account.id}>
                {account.primary ? "Chính" : "Phụ"} · {account.bank} · ••••
                {account.number.slice(-4)} · {formatVND(account.balance)}
              </option>
            ))}
          </select>
        </div>
      </div>
      <div className="tx-row">
        <span className="tx-row__label">Tần suất</span>
        <span className="tx-row__value">{draft.cron_label}</span>
      </div>
      <div className="tx-row">
        <span className="tx-row__label">Lần đầu</span>
        <span className="tx-row__value">{formatDate(draft.next_run)}</span>
      </div>
      {draft.description && (
        <div className="tx-row">
          <span className="tx-row__label">Nội dung</span>
          <span className="tx-row__value">{draft.description}</span>
        </div>
      )}
      <div className="tx-flags">
        <div className="tx-flag">
          ℹ️ Tạo lịch cần OTP. Trước mỗi lần đến hạn, Omni sẽ nhắc bạn xác nhận.
        </div>
      </div>
      {actionable && otpOpen && (
        <div className="otp-panel">
          <div className="otp-panel__copy">
            Nhập OTP để xác minh lịch định kỳ. Mã demo: <strong>123456</strong>
          </div>
          <input
            className="otp-input"
            value={cleanOtp}
            onChange={(e) => setOtp(e.target.value.replace(/\D/g, "").slice(0, 6))}
            inputMode="numeric"
            maxLength={6}
            placeholder="••••••"
            autoFocus
          />
        </div>
      )}
      {actionable ? (
        <div className="tx-actions">
          <button
            className="btn btn--ghost"
            onClick={handleCancel}
            disabled={locked}
            title={submitting ? "Đang xử lý — không thể huỷ" : undefined}
          >
            Huỷ
          </button>
          <button
            className={`btn ${otpOpen ? "btn--warn" : "btn--primary"}`}
            onClick={handleConfirm}
            disabled={locked || (otpOpen && cleanOtp.length !== 6)}
            aria-busy={submitting}
          >
            {submitting
              ? "Đang xử lý…"
              : otpOpen
                ? "Xác minh & tạo lịch"
                : "Tạo lịch"}
          </button>
        </div>
      ) : (
        <div className="tx-status">Lịch định kỳ này đã được xử lý.</div>
      )}
    </div>
  );
};
