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
  onConfirm: (sourceAccountId?: string) => void;
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
    selectedAccount && draft.amount != null ? draft.amount > selectedAccount.balance : false;
  const canSubmit =
    actionable && !disabled && !hardBlocked && !selectedBalanceBlocks && draft.amount != null && r != null;

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
                      {account.primary ? "Chính" : "Phụ"} · {account.bank} · ****
                      {account.number.slice(-4)} · {formatVND(account.balance)}
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

      {draft.flags.length > 0 && (
        <div className="tx-flags">
          {draft.flags.map((f, i) => (
            <div key={i} className={`tx-flag tx-flag--${f.severity}`}>
              {f.severity === "block" ? "!" : f.severity === "warn" ? "!" : "i"} {f.message}
            </div>
          ))}
        </div>
      )}

      {actionable ? (
        <div className="tx-actions">
          <button className="btn btn--ghost" onClick={onCancel} disabled={disabled}>
            Huỷ
          </button>
          {onEdit && (
            <button className="btn btn--ghost" onClick={onEdit} disabled={disabled}>
              Sửa
            </button>
          )}
          <button
            className={`btn ${draft.requires_step_up ? "btn--warn" : "btn--primary"}`}
            onClick={() => onConfirm(sourceAccountId || undefined)}
            disabled={!canSubmit}
          >
            Xác nhận
          </button>
        </div>
      ) : (
        <div className="tx-status">Giao dịch này đã được xử lý.</div>
      )}
    </div>
  );
};
