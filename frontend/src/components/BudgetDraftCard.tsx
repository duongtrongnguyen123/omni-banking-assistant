import { api } from "../api/client";
import { formatVND } from "../format";
import type { BudgetDraft, GoalDraft, OmniResponse } from "../types";

/** Confirm card for a staged budget OR savings goal. The same shape
 * works for both because the action set is identical (xác nhận / huỷ),
 * and both arrive on the same OmniResponse payload — we pick which
 * draft to render from the parent's data. */

interface BudgetProps {
  draft: BudgetDraft;
  onResolve: (resp: OmniResponse) => void;
  busy?: boolean;
}

export const BudgetDraftCard = ({ draft, onResolve, busy }: BudgetProps) => {
  const onConfirm = async () => {
    try {
      const resp = await api.confirmBudget(draft.id);
      onResolve(resp);
    } catch (e) {
      // Surface the error as a no-op response so the chat thread
      // doesn't silently drop the click.
      onResolve({
        intent: "set_budget",
        text: `Lỗi xác nhận ngân sách: ${String(e instanceof Error ? e.message : e)}`,
        draft: null,
        contact_draft: null,
        schedule_draft: null,
        history: null,
        balance: null,
        schedule: null,
        recurring_patterns: null,
        needs_disambiguation: false,
      });
    }
  };

  const onCancel = async () => {
    try {
      const resp = await api.cancelBudget(draft.id);
      onResolve(resp);
    } catch {
      /* silent — cancel is best-effort */
    }
  };

  return (
    <div className="budget-draft-card">
      <div className="budget-draft-card__title">
        {draft.replaces_existing ? "Cập nhật ngân sách" : "Ngân sách mới"}
      </div>
      <div className="budget-draft-card__body">
        <div className="budget-draft-card__cat">{draft.category_label}</div>
        <div className="budget-draft-card__amount">
          {formatVND(draft.monthly_limit_vnd)}{" "}
          <span className="budget-draft-card__period">/ tháng</span>
        </div>
      </div>
      <div className="budget-draft-card__actions">
        <button
          className="btn btn--ghost"
          onClick={onCancel}
          disabled={busy}
          aria-label="Huỷ đặt ngân sách"
        >
          Huỷ
        </button>
        <button
          className="btn btn--primary"
          onClick={onConfirm}
          disabled={busy}
          aria-label="Xác nhận đặt ngân sách"
        >
          Xác nhận
        </button>
      </div>
    </div>
  );
};


/** Identical confirm card shape for savings goals. */
interface GoalProps {
  draft: GoalDraft;
  onResolve: (resp: OmniResponse) => void;
  busy?: boolean;
}

export const GoalDraftCard = ({ draft, onResolve, busy }: GoalProps) => {
  const onConfirm = async () => {
    try {
      const resp = await api.confirmGoal(draft.id);
      onResolve(resp);
    } catch (e) {
      onResolve({
        intent: "set_goal",
        text: `Lỗi xác nhận mục tiêu: ${String(e instanceof Error ? e.message : e)}`,
        draft: null,
        contact_draft: null,
        schedule_draft: null,
        history: null,
        balance: null,
        schedule: null,
        recurring_patterns: null,
        needs_disambiguation: false,
      });
    }
  };

  const onCancel = async () => {
    try {
      const resp = await api.cancelGoal(draft.id);
      onResolve(resp);
    } catch {
      /* silent */
    }
  };

  return (
    <div className="budget-draft-card">
      <div className="budget-draft-card__title">Mục tiêu tiết kiệm mới</div>
      <div className="budget-draft-card__body">
        <div className="budget-draft-card__cat">{draft.name}</div>
        <div className="budget-draft-card__amount">
          {formatVND(draft.target_vnd)}
        </div>
        {draft.deadline && (
          <div className="budget-draft-card__deadline">Hạn: {draft.deadline}</div>
        )}
      </div>
      <div className="budget-draft-card__actions">
        <button
          className="btn btn--ghost"
          onClick={onCancel}
          disabled={busy}
          aria-label="Huỷ mục tiêu"
        >
          Huỷ
        </button>
        <button
          className="btn btn--primary"
          onClick={onConfirm}
          disabled={busy}
          aria-label="Xác nhận mục tiêu"
        >
          Xác nhận
        </button>
      </div>
    </div>
  );
};
