import { useEffect, useState } from "react";
import type { Account, TransactionDraft } from "../types";
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

const FingerprintIcon = ({ size = 22 }: { size?: number }) => (
  <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true">
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

const accountKindLabel = (a: Account): string => {
  if (a.label) return a.label;
  switch (a.kind) {
    case "savings":
      return "Tiết kiệm";
    case "salary":
      return "Lương";
    case "checking":
      return "Thanh toán";
    default:
      return a.primary ? "Chính" : "Phụ";
  }
};

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

  // Keep selected source in sync if the backend updates the draft.
  useEffect(() => {
    if (draft.source_account_id && draft.source_account_id !== sourceAccountId) {
      setSourceAccountId(draft.source_account_id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft.source_account_id]);

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

  // The backend's same_bank flag is computed against draft.source_account_id;
  // also recompute locally so the pill toggles immediately when the user picks
  // a different chip before submitting.
  const sameBankLocal =
    !!r &&
    !!selectedAccount &&
    selectedAccount.bank.toLowerCase() === r.bank.toLowerCase();
  const showSameBankPill = sameBankLocal || draft.same_bank;

  // Tier-2 needs biometric BEFORE OTP per task spec. Track which step we show.
  const needsBoth =
    draft.auth_required.includes("biometric") &&
    draft.auth_required.includes("otp");
  const showOtpStep = needsBoth ? bioDone : draft.auth_required.includes("otp");

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
      // For tier-1 (biometric-only) auto-submit; tier-2 waits for OTP.
      if (needBio && !needOtp) {
        onConfirm({
          source_account_id: sourceAccountId || undefined,
          biometric_verified: true,
        });
      } else if (needsBoth) {
        // Inform backend that biometric is done so server-side
        // auth_completed reflects it before OTP is entered.
        onConfirm({
          source_account_id: sourceAccountId || undefined,
          biometric_verified: true,
        });
      }
    }, 1200);
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
      {showSameBankPill && (
        <div className="same-bank-pill" title="Giao dịch cùng ngân hàng">
          <span className="same-bank-pill__dot" />
          Cùng ngân hàng · Miễn phí
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
            <div className="tx-row tx-row--accounts">
              <span className="tx-row__label">Tài khoản nguồn</span>
              <div className="tx-row__value">
                <div className="account-chips" role="radiogroup">
                  {draft.source_accounts.map((account) => {
                    const selected = account.id === sourceAccountId;
                    const tooLow =
                      draft.amount != null && account.balance < draft.amount;
                    return (
                      <button
                        type="button"
                        role="radio"
                        aria-checked={selected}
                        key={account.id}
                        className={`account-chip ${
                          selected ? "account-chip--selected" : ""
                        } ${tooLow ? "account-chip--low" : ""}`}
                        onClick={() => setSourceAccountId(account.id)}
                        disabled={!actionable || disabled}
                        title={`${account.bank} · ${formatVND(account.balance)}`}
                      >
                        <span className="account-chip__icon" aria-hidden="true">
                          🏦
                        </span>
                        <span className="account-chip__body">
                          <span className="account-chip__bank">
                            {account.bank}
                          </span>
                          <span className="account-chip__sub">
                            {accountKindLabel(account)} · ••••
                            {account.number.slice(-4)}
                          </span>
                          <span className="account-chip__balance">
                            {formatVND(account.balance)}
                          </span>
                        </span>
                      </button>
                    );
                  })}
                </div>
                {selectedBalanceBlocks && (
                  <div className="account-select__hint">
                    Tài khoản này không đủ số dư, hãy chọn tài khoản khác hoặc
                    huỷ.
                  </div>
                )}
                {draft.auto_pick_reason === "same_bank_no_fee" && r && (
                  <div className="account-pick-reason">
                    Mình tự chọn {selectedAccount?.bank ?? "tài khoản này"} để
                    giao dịch nội mạng — phí 0đ.
                  </div>
                )}
                {draft.auto_pick_reason === "large_amount_uses_savings" && (
                  <div className="account-pick-reason">
                    Số tiền lớn — mình ưu tiên rút từ tài khoản tiết kiệm.
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
                {needBio && needOtp
                  ? "Giao dịch rủi ro cao cần sinh trắc học và OTP."
                  : needBio
                    ? "Chạm để xác thực sinh trắc học."
                    : "Nhập OTP để xác minh giao dịch. Mã demo: 123456"}
              </div>

              {/* Biometric step (always first when both are required) */}
              {(needBio || draft.auth_completed.includes("biometric")) && (
                <div className="auth-step">
                  <div className="auth-step__head">
                    <span className="auth-step__num">1</span>
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
                      className={`bio-overlay ${
                        bioLoading ? "bio-overlay--scanning" : ""
                      }`}
                      onClick={runBiometric}
                      disabled={bioLoading || disabled}
                      aria-label="Chạm để xác thực sinh trắc học"
                    >
                      <span className="bio-overlay__rings">
                        <span className="bio-overlay__ring" />
                        <span className="bio-overlay__ring" />
                        <span className="bio-overlay__ring" />
                      </span>
                      <span className="bio-overlay__icon">
                        {bioLoading ? (
                          <span className="bio-spinner" />
                        ) : (
                          <FingerprintIcon size={42} />
                        )}
                      </span>
                      <span className="bio-overlay__label">
                        {bioLoading ? "Đang quét…" : "Chạm để xác thực"}
                      </span>
                    </button>
                  )}
                </div>
              )}

              {/* OTP step — only show after biometric is done in tier-2 */}
              {(needOtp || otpDone) && showOtpStep && (
                <div className="auth-step">
                  <div className="auth-step__head">
                    <span className="auth-step__num">
                      {needsBoth ? 2 : 1}
                    </span>
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
