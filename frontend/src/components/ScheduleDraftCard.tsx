import type { ScheduleDraft } from "../types";
import { formatVND, formatDate } from "../format";

interface Props {
  draft: ScheduleDraft;
  onConfirm: () => void;
  onCancel: () => void;
  disabled?: boolean;
}

export const ScheduleDraftCard = ({ draft, onConfirm, onCancel, disabled }: Props) => (
  <div className="tx-card" data-testid="schedule-draft-card">
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
        ℹ️ Trước mỗi lần đến hạn, mình sẽ hỏi lại để bạn quyết định gửi hay tạm dừng.
      </div>
    </div>
    <div className="tx-actions">
      <button
        className="btn btn--ghost"
        onClick={onCancel}
        disabled={disabled}
        data-testid="schedule-cancel-btn"
      >
        Huỷ
      </button>
      <button
        className="btn btn--primary"
        onClick={onConfirm}
        disabled={disabled}
        data-testid="schedule-confirm-btn"
      >
        Tạo lịch
      </button>
    </div>
  </div>
);
