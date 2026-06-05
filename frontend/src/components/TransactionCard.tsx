import { useState } from "react";
import type { TransactionDraft } from "../types";
import { formatVND } from "../format";

interface Props {
  draft: TransactionDraft;
  onConfirm: (opts: {
    otp?: string;
    sourceAccountId?: string;
    biometricVerified?: boolean;
  }) => void;
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
  const [biometricBusy, setBiometricBusy] = useState(false);
  const [sourceAccountId, setSourceAccountId] = useState(
    draft.source_account_id ?? draft.source_accounts[0]?.id ?? "",
  );

  const hardBlocked = draft.flags.some(
    (f) => f.severity === "block" && f.code !== "insufficient_balance",
  );
  const warned = draft.flags.some((f) => f.severity === "warn");
  const r = draft.recipient;
  const selectedAccount = draft.source_accounts.find((a) => a.id === sourceAccountId);
  const selectedBalanceBlocks =
    selectedAccount && draft.amount != null ? draft.amount > selectedAccount.balance : false;
  const canSubmit =
    actionable && !disabled && !hardBlocked && !selectedBalanceBlocks && draft.amount != null && r != null;
  const cleanOtp = otp.replace(/\D/g, "").slice(0, 6);
  const needsOtp = draft.auth_required.includes("otp") && !draft.auth_completed.includes("otp");
  const needsBiometric =
    draft.auth_required.includes("biometric") && !draft.auth_completed.includes("biometric");
  const waitingForBiometric = !needsOtp && needsBiometric;

  const handleOtpChange = (value: string) => {
    setOtp(value.replace(/\D/g, "").slice(0, 6));
  };

  const handleConfirm = () => {
    if (needsOtp && !otpOpen) {
      setOtpOpen(true);
      return;
    }

    if (needsOtp) {
      onConfirm({ otp: cleanOtp, sourceAccountId: sourceAccountId || undefined });
      return;
    }

    if (needsBiometric) {
      setBiometricBusy(true);
      window.setTimeout(() => {
        setBiometricBusy(false);
        onConfirm({
          biometricVerified: true,
          sourceAccountId: sourceAccountId || undefined,
        });
      }, 900);
      return;
    }

    onConfirm({ sourceAccountId: sourceAccountId || undefined });
  };

  return (
    <div className={`tx-card ${warned ? "tx-card--warn" : ""} ${!actionable ? "tx-card--done" : ""}`}>
      {draft.amount != null && (
        <div className="tx-card__amount">
          <div className="tx-card__label">SO TIEN</div>
          <div className="tx-card__amount-value">{formatVND(draft.amount)}</div>
        </div>
      )}

      {r && (
        <>
          <div className="tx-row">
            <span className="tx-row__label">Nguoi nhan</span>
            <div className="tx-row__value">
              {r.label && <span className="tx-tag">{r.label}</span>}
              <div className="tx-recipient">
                <div className="tx-recipient__name">{r.display_name}</div>
                <div className="tx-recipient__meta">
                  {r.bank} · {r.account_masked}{" "}
                  {r.verified && <span className="tx-verified">· Da xac minh</span>}
                </div>
              </div>
            </div>
          </div>

          {draft.description && (
            <div className="tx-row">
              <span className="tx-row__label">Noi dung</span>
              <span className="tx-row__value">{draft.description}</span>
            </div>
          )}

          {draft.source_accounts.length > 0 && (
            <div className="tx-row">
              <span className="tx-row__label">Tai khoan nguon</span>
              <div className="tx-row__value">
                <select
                  className="account-select"
                  value={sourceAccountId}
                  onChange={(e) => setSourceAccountId(e.target.value)}
                  disabled={!actionable || disabled}
                >
                  {draft.source_accounts.map((account) => (
                    <option key={account.id} value={account.id}>
                      {account.primary ? "Chinh" : "Phu"} · {account.bank} · ****
                      {account.number.slice(-4)} · {formatVND(account.balance)}
                    </option>
                  ))}
                </select>
                {selectedBalanceBlocks && (
                  <div className="account-select__hint">
                    Tai khoan nay khong du so du, hay chon tai khoan khac hoac huy.
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
        <>
          {otpOpen && needsOtp && (
            <div className="otp-panel">
              <div className="otp-panel__copy">
                Nhap OTP de xac minh giao dich. Ma demo: <strong>123456</strong>
              </div>
              <input
                className="otp-input"
                value={cleanOtp}
                onChange={(e) => handleOtpChange(e.target.value)}
                inputMode="numeric"
                maxLength={6}
                placeholder="******"
                autoFocus
              />
            </div>
          )}

          {waitingForBiometric && (
            <div className="otp-panel">
              <div className="otp-panel__copy">
                OTP da xac minh. Giao dich rui ro can them sinh trac hoc.
              </div>
            </div>
          )}

          <div className="tx-actions">
            <button className="btn btn--ghost" onClick={onCancel} disabled={disabled}>
              Huy
            </button>
            {onEdit && (
              <button className="btn btn--ghost" onClick={onEdit} disabled={disabled}>
                Sua
              </button>
            )}
            <button
              className={`btn ${draft.requires_step_up || otpOpen ? "btn--warn" : "btn--primary"}`}
              onClick={handleConfirm}
              disabled={
                !canSubmit ||
                biometricBusy ||
                (needsOtp && otpOpen && cleanOtp.length !== 6)
              }
            >
              {biometricBusy
                ? "Dang quet..."
                : waitingForBiometric
                  ? "Xac minh sinh trac hoc"
                  : otpOpen
                    ? "Xac minh OTP"
                    : "Xac nhan"}
            </button>
          </div>
        </>
      ) : (
        <div className="tx-status">Giao dich nay da duoc xu ly.</div>
      )}
    </div>
  );
};
