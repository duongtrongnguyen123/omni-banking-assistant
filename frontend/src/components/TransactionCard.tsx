import { useEffect, useState } from "react";
import type { TransactionDraft } from "../types";
import { formatVND } from "../format";

interface ConfirmPayload {
  otp?: string;
  biometric_verified?: boolean;
  source_account_id?: string;
}

interface Props {
  draft: TransactionDraft;
  onConfirm: (payload: ConfirmPayload) => void;
  onCancel: () => void;
  onEdit?: () => void;
  disabled?: boolean;
  actionable?: boolean;
}

const FingerprintIcon = () => (
  <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
    <path
      d="M12 3a8 8 0 0 0-8 8v3M12 3a8 8 0 0 1 8 8v2M8 11a4 4 0 0 1 8 0v3a8 8 0 0 1-1.5 4.6M12 11v4a5 5 0 0 1-1 3M4 17.5C4.7 19 5.4 20 6 21M20 17c-.4.8-.9 1.6-1.5 2.3"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
    />
  </svg>
);

const CheckIcon = () => (
  <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
    <path
      d="M5 12l4.5 4.5L19 7"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  </svg>
);

export const TransactionCard = ({
  draft,
  onConfirm,
  onCancel,
  onEdit,
  disabled,
  actionable = true,
}: Props) => {
  const [authOpen, setAuthOpen] = useState(false);
  const [otp, setOtp] = useState("");
  const [bioLoading, setBioLoading] = useState(false);
  const [bioLocalDone, setBioLocalDone] = useState(false);
  const [sourceAccountId, setSourceAccountId] = useState(
    draft.source_account_id ?? draft.source_accounts[0]?.id ?? "",
  );

  const needOtp =
    draft.auth_required.includes("otp") &&
    !draft.auth_completed.includes("otp");
  const needBio =
    draft.auth_required.includes("biometric") &&
    !draft.auth_completed.includes("biometric");
  const otpDone = draft.auth_completed.includes("otp");
  const bioDone = draft.auth_completed.includes("biometric") || bioLocalDone;

  const blockFlags = draft.flags.filter((f) => f.severity === "block");
  const hardBlocked =
    draft.auth_required.length === 0 &&
    blockFlags.some((f) => f.code !== "insufficient_balance");
  const warned = draft.flags.some((f) => f.severity === "warn");
  const r = draft.recipient;
  const selectedAccount = draft.source_accounts.find(
    (a) => a.id === sourceAccountId,
  );
  const selectedBalanceBlocks =
    selectedAccount && draft.amount != null
      ? draft.amount > selectedAccount.balance
      : false;

  // Reset auth panel when server returns updated auth_completed
  useEffect(() => {
    if (draft.auth_completed.includes("biometric")) setBioLocalDone(true);
  }, [draft.auth_completed]);

  const cleanOtp = otp.replace(/\D/g, "").slice(0, 6);
  const otpReady = !needOtp || cleanOtp.length === 6;
  const bioReady = !needBio || bioDone;
  const canSubmit =
    actionable &&
    !disabled &&
    !hardBlocked &&
    !selectedBalanceBlocks &&
    draft.amount != null &&
    r != null;

  const handleOtpChange = (value: string) => {
    setOtp(value.replace(/\D/g, "").slice(0, 6));
  };

  const runBiometric = () => {
    if (bioLoading || bioDone) return;
    setBioLoading(true);
    setTimeout(() => {
      setBioLoading(false);
      setBioLocalDone(true);
    }, 1500);
  };

  const submit = () => {
    const payload: ConfirmPayload = {
      source_account_id: sourceAccountId || undefined,
    };
    if (needOtp) payload.otp = cleanOtp;
    if (needBio && bioDone) payload.biometric_verified = true;
    onConfirm(payload);
  };

  const handleConfirmClick = () => {
    const requiresAuth = needOtp || needBio;
    if (!requiresAuth) {
      submit();
      return;
    }
    if (!authOpen) {
      setAuthOpen(true);
      return;
    }
    submit();
  };

  const primaryReady = otpReady && bioReady;

  return (
    <div
      className={`tx-card ${warned ? "tx-card--warn" : ""} ${
        !actionable ? "tx-card--done" : ""
      }`}
    >
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
                  {r.verified && (
                    <span className="tx-verified">· Đã xác minh</span>
                  )}
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
                      {account.primary ? "Chính" : "Phụ"} · {account.bank} ·
                      ••••{account.number.slice(-4)} ·{" "}
                      {formatVND(account.balance)}
                    </option>
                  ))}
                </select>
                {selectedBalanceBlocks && (
                  <div className="account-select__hint">
                    Tài khoản này không đủ số dư, hãy chọn tài khoản khác hoặc
                    huỷ.
                  </div>
                )}
              </div>
            </div>
          )}
        </>
      )}

      {(() => {
        const visibleFlags = draft.flags.filter(
          (f) => f.code !== "insufficient_balance",
        );
        if (visibleFlags.length === 0) return null;
        return (
          <div className="tx-flags">
            {visibleFlags.map((f, i) => (
              <div key={i} className={`tx-flag tx-flag--${f.severity}`}>
                {f.severity === "block"
                  ? "⛔"
                  : f.severity === "warn"
                    ? "⚠️"
                    : "ℹ️"}{" "}
                {f.message}
              </div>
            ))}
          </div>
        );
      })()}

      {actionable ? (
        <>
          {authOpen && !hardBlocked && (
            <div className="auth-panel">
              <div className="auth-panel__copy">
                {needOtp && needBio
                  ? "Giao dịch cần OTP và xác minh sinh trắc học."
                  : needOtp
                    ? "Nhập OTP để xác minh giao dịch. Mã demo: 123456"
                    : "Cần xác minh sinh trắc học để tiếp tục."}
              </div>

              {(needOtp || otpDone) && (
                <div className="auth-step">
                  <div className="auth-step__head">
                    <span className="auth-step__num">1</span>
                    <span className="auth-step__title">OTP</span>
                    {otpDone && (
                      <span className="auth-step__done">
                        <CheckIcon /> Đã xác minh
                      </span>
                    )}
                  </div>
                  {!otpDone && (
                    <input
                      className="otp-input"
                      value={cleanOtp}
                      onChange={(e) => handleOtpChange(e.target.value)}
                      inputMode="numeric"
                      maxLength={6}
                      placeholder="••••••"
                      autoFocus
                    />
                  )}
                </div>
              )}

              {(needBio || draft.auth_completed.includes("biometric")) && (
                <div className="auth-step">
                  <div className="auth-step__head">
                    <span className="auth-step__num">{needOtp ? 2 : 1}</span>
                    <span className="auth-step__title">Sinh trắc học</span>
                    {bioDone && (
                      <span className="auth-step__done">
                        <CheckIcon /> Đã xác minh
                      </span>
                    )}
                  </div>
                  {!bioDone && (
                    <button
                      type="button"
                      className={`bio-btn ${bioLoading ? "bio-btn--loading" : ""}`}
                      onClick={runBiometric}
                      disabled={bioLoading || disabled}
                    >
                      <span className="bio-btn__icon">
                        {bioLoading ? (
                          <span className="bio-spinner" />
                        ) : (
                          <FingerprintIcon />
                        )}
                      </span>
                      <span>
                        {bioLoading
                          ? "Đang quét sinh trắc…"
                          : "Quét vân tay / khuôn mặt"}
                      </span>
                    </button>
                  )}
                </div>
              )}
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
            {!hardBlocked && (
              <button
                className={`btn ${
                  authOpen || draft.requires_step_up
                    ? "btn--warn"
                    : "btn--primary"
                }`}
                onClick={handleConfirmClick}
                disabled={!canSubmit || (authOpen && !primaryReady)}
              >
                {authOpen ? "Xác minh & chuyển" : "Xác nhận"}
              </button>
            )}
          </div>
        </>
      ) : (
        <div className="tx-status">Giao dịch này đã được xử lý.</div>
      )}
    </div>
  );
};
