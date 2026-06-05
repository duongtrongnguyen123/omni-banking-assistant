import { useState } from "react";
import type { TransactionDraft } from "../types";
import { formatVND } from "../format";

interface Props {
  draft: TransactionDraft;
  onConfirm: (otp: string, sourceAccountId?: string) => void;
  onCancel: () => void;
  onEdit?: () => void;
  disabled?: boolean;
  actionable?: boolean;
}

export const TransactionCard = ({
  draft,
  onConfirm,
  onCancel,
  onEdit,
  disabled,
  actionable = true,
}: Props) => {
  const [otpOpen, setOtpOpen] = useState(false);
  const [otp, setOtp] = useState("");
  const [sourceAccountId, setSourceAccountId] = useState(
    draft.source_account_id ?? draft.source_accounts[0]?.id ?? "",
  );
  const blocked = draft.flags.some((f) => f.severity === "block");
  const hardBlocked = draft.flags.some(
    (f) => f.severity === "block" && f.code !== "insufficient_balance",
  );
  const warned = draft.flags.some((f) => f.severity === "warn");
  const r = draft.recipient;
  const selectedAccount = draft.source_accounts.find((a) => a.id === sourceAccountId);
  // Only flag "không đủ số dư" when we know the amount AND it really exceeds
  // the selected account's balance. Without an amount it's a missing-info
  // issue, not a balance issue — the missing_amount safety flag below will
  // surface that separately.
  const selectedBalanceBlocks =
    !!selectedAccount && draft.amount != null && draft.amount > selectedAccount.balance;
  const canSubmit =
    actionable && !disabled && !hardBlocked && !selectedBalanceBlocks && draft.amount != null && r != null;
  const cleanOtp = otp.replace(/\D/g, "").slice(0, 6);

  const handleOtpChange = (value: string) => {
    setOtp(value.replace(/\D/g, "").slice(0, 6));
  };

  const handleConfirm = () => {
    if (!otpOpen) {
      setOtpOpen(true);
      return;
    }
    onConfirm(cleanOtp, sourceAccountId || undefined);
  };

  // Surface the step-up reason as a hero banner so the safety layer is
  // unmistakable in the demo. The same message is also in `draft.flags`
  // below, but a top banner makes the *security posture* visible at a
  // glance instead of buried under the amount.
  const stepUpReason = draft.requires_step_up
    ? draft.flags.find(
        (f) =>
          f.severity === "warn" &&
          (f.code === "new_recipient_large_amount" ||
            f.code === "amount_above_average"),
      )
    : undefined;

  return (
    <div className={`tx-card ${warned ? "tx-card--warn" : ""} ${!actionable ? "tx-card--done" : ""}`}>
      {actionable && stepUpReason && (
        <div className="tx-stepup" role="status">
          <span className="tx-stepup__icon" aria-hidden>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
              <rect x="4" y="11" width="16" height="9" rx="2" />
              <path d="M8 11V8a4 4 0 0 1 8 0v3" />
            </svg>
          </span>
          <div className="tx-stepup__body">
            <div className="tx-stepup__title">Cần xác thực OTP</div>
            <div className="tx-stepup__reason">{stepUpReason.message}</div>
          </div>
        </div>
      )}
      {draft.amount != null && (
        <div className="tx-card__amount">
          <div className="tx-card__label">SỐ TIỀN</div>
          <div className="tx-card__amount-value">{formatVND(draft.amount)}</div>
        </div>
      )}
      {r && (
        <>
          <div className="tx-row">
            <span className="tx-row__label">Người nhận</span>
            <div className="tx-row__value">
              {r.label && <span className="tx-tag">♥ {r.label}</span>}
              <div className="tx-recipient">
                <div className="tx-recipient__name">{r.display_name}</div>
                <div className="tx-recipient__meta">
                  {r.bank} · {r.account_masked}{" "}
                  {r.verified && <span className="tx-verified">· Đã xác minh</span>}
                </div>
              </div>
            </div>
          </div>
          {draft.description && (
            <div className="tx-row">
              <span className="tx-row__label">Nội dung</span>
              <span className="tx-row__value">{draft.description}</span>
            </div>
          )}
          {draft.source_accounts.length > 0 && (
            <div className="tx-row">
              <span className="tx-row__label">Tài khoản nguồn</span>
              <div className="tx-row__value">
                <select
                  className="account-select"
                  value={sourceAccountId}
                  onChange={(e) => setSourceAccountId(e.target.value)}
                  disabled={!actionable || disabled}
                >
                  {draft.source_accounts.map((account) => (
                    <option key={account.id} value={account.id}>
                      {account.primary ? "Chính" : "Phụ"} · {account.bank} · ••••
                      {account.number.slice(-4)} · {formatVND(account.balance)}
                    </option>
                  ))}
                </select>
                {selectedBalanceBlocks && (
                  <div className="account-select__hint">
                    Tài khoản này không đủ số dư, hãy chọn tài khoản khác hoặc huỷ.
                  </div>
                )}
              </div>
            </div>
          )}
        </>
      )}

      {draft.flags.length > 0 && (
        <div className="tx-flags">
          {draft.flags.map((f, i) => (
            <div key={i} className={`tx-flag tx-flag--${f.severity}`}>
              {f.severity === "block" ? "⛔" : f.severity === "warn" ? "⚠️" : "ℹ️"}{" "}
              {f.message}
            </div>
          ))}
        </div>
      )}

      {actionable ? (
        <>
          {otpOpen && (
            <div className="otp-panel">
              <div className="otp-panel__copy">
                Nhập OTP để xác minh giao dịch. Mã demo: <strong>123456</strong>
              </div>
              <input
                className="otp-input"
                value={cleanOtp}
                onChange={(e) => handleOtpChange(e.target.value)}
                inputMode="numeric"
                maxLength={6}
                placeholder="••••••"
                autoFocus
              />
            </div>
          )}
          <div className="tx-actions">
            <button
              className="btn btn--ghost"
              onClick={onCancel}
              disabled={disabled}
            >
              Huỷ
            </button>
            {onEdit && (
              <button
                className="btn btn--ghost"
                onClick={onEdit}
                disabled={disabled}
              >
                Sửa
              </button>
            )}
            <button
              className={`btn ${draft.requires_step_up || otpOpen ? "btn--warn" : "btn--primary"}`}
              onClick={handleConfirm}
              disabled={!canSubmit || (otpOpen && cleanOtp.length !== 6)}
            >
              {otpOpen ? "Xác minh & chuyển" : "Xác nhận"}
            </button>
          </div>
        </>
      ) : (
        <div className="tx-status">Giao dịch này đã được xử lý.</div>
      )}
    </div>
  );
};
