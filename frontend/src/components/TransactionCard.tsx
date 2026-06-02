import type { TransactionDraft } from "../types";
import { formatVND } from "../format";

interface Props {
  draft: TransactionDraft;
  onConfirm: () => void;
  onCancel: () => void;
  onEdit?: () => void;
  disabled?: boolean;
}

export const TransactionCard = ({
  draft,
  onConfirm,
  onCancel,
  onEdit,
  disabled,
}: Props) => {
  const blocked = draft.flags.some((f) => f.severity === "block");
  const warned = draft.flags.some((f) => f.severity === "warn");
  const r = draft.recipient;

  return (
    <div className={`tx-card ${warned ? "tx-card--warn" : ""}`}>
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
          className={`btn ${draft.requires_step_up ? "btn--warn" : "btn--primary"}`}
          onClick={onConfirm}
          disabled={disabled || blocked || draft.amount == null || r == null}
        >
          {draft.requires_step_up ? "Xác minh OTP & xác nhận" : "Xác nhận"}
        </button>
      </div>
    </div>
  );
};
