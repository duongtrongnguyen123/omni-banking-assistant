import type { ContactDraft } from "../types";

interface Props {
  draft: ContactDraft;
  onConfirm: () => void;
  onCancel: () => void;
  disabled?: boolean;
}

export const ContactDraftCard = ({ draft, onConfirm, onCancel, disabled }: Props) => (
  <div className="tx-card" data-testid="contact-draft-card">
    <div className="tx-card__label">DANH BẠ MỚI</div>
    <div className="contact-draft__name">{draft.display_name}</div>
    <div className="tx-row">
      <span className="tx-row__label">Ngân hàng</span>
      <span className="tx-row__value">{draft.bank}</span>
    </div>
    <div className="tx-row">
      <span className="tx-row__label">Số tài khoản</span>
      <span className="tx-row__value">{draft.account_number}</span>
    </div>
    {draft.aliases.length > 0 && (
      <div className="tx-row">
        <span className="tx-row__label">Tên gọi tắt</span>
        <span className="tx-row__value">
          {draft.aliases.map((a) => (
            <span key={a} className="tx-tag tx-tag--neutral">
              {a}
            </span>
          ))}
        </span>
      </div>
    )}
    <div className="tx-flags">
      <div className="tx-flag">
        ℹ️ Mình sẽ chưa đánh dấu tài khoản là "đã xác minh". Hãy kiểm tra số tài khoản
        trước khi xác nhận.
      </div>
    </div>
    <div className="tx-actions">
      <button
        className="btn btn--ghost"
        onClick={onCancel}
        disabled={disabled}
        data-testid="contact-cancel-btn"
      >
        Huỷ
      </button>
      <button
        className="btn btn--primary"
        onClick={onConfirm}
        disabled={disabled}
        data-testid="contact-confirm-btn"
      >
        Lưu danh bạ
      </button>
    </div>
  </div>
);
