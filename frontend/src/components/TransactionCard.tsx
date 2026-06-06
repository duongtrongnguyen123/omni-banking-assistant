import { useEffect, useRef, useState } from "react";
import type { TransactionDraft } from "../types";
import { formatVND } from "../format";

// Visual metadata for the auto-categoriser chip. Keys must match the
// categories emitted by `backend/app/ml/categorizer.py:CATEGORIES`.
// Unknown categories fall back to a neutral grey chip with a generic icon.
const CATEGORY_META: Record<
  string,
  { label: string; emoji: string; bg: string; fg: string }
> = {
  food:          { label: "Ăn uống",       emoji: "🍜", bg: "#fff1e6", fg: "#b25a13" },
  transport:     { label: "Di chuyển",     emoji: "🚗", bg: "#e6f0ff", fg: "#1a4fb0" },
  groceries:     { label: "Đi chợ",        emoji: "🛒", bg: "#e9f7e6", fg: "#2f7a25" },
  entertainment: { label: "Giải trí",      emoji: "🎬", bg: "#f3e6ff", fg: "#6b1fb0" },
  health:        { label: "Sức khoẻ",      emoji: "🩺", bg: "#ffe6ea", fg: "#b01a3a" },
  rent:          { label: "Nhà cửa",       emoji: "🏠", bg: "#fff4e0", fg: "#a55a00" },
  utilities:     { label: "Hoá đơn",       emoji: "💡", bg: "#fff8d6", fg: "#7a6800" },
  gifts:         { label: "Quà / Mừng",    emoji: "🎁", bg: "#ffe0ec", fg: "#b01a6a" },
  savings:       { label: "Tiết kiệm",     emoji: "💰", bg: "#e0f5ee", fg: "#0f7a55" },
  family:        { label: "Gia đình",      emoji: "👨‍👩‍👧", bg: "#fde6f4", fg: "#a01a78" },
  friends:       { label: "Bạn bè",        emoji: "🧋", bg: "#e6f0f7", fg: "#1a5a78" },
  work:          { label: "Công việc",     emoji: "💼", bg: "#eef0f4", fg: "#3a4255" },
  other:         { label: "Khác",          emoji: "•",  bg: "#f1f2f4", fg: "#5a5f6a" },
};

const CategoryChip = ({ category }: { category: string }) => {
  const meta = CATEGORY_META[category] ?? CATEGORY_META.other;
  return (
    <span
      className="tx-card__category-chip"
      title={`Tự nhận diện danh mục: ${meta.label}`}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        marginTop: 6,
        padding: "2px 8px",
        borderRadius: 999,
        fontSize: 11,
        fontWeight: 600,
        background: meta.bg,
        color: meta.fg,
        lineHeight: 1.5,
      }}
    >
      <span aria-hidden>{meta.emoji}</span>
      {meta.label}
    </span>
  );
};

/** Tiny bar chart contrasting the last 3 amounts to this recipient
 *  against the current draft amount. The current bar gets a warn fill
 *  when the safety layer flagged the draft so the divergence is
 *  visually unmistakable even before the user reads the prose flag. */
const AmountVsHistoryBars = ({
  history,
  current,
  flagged,
}: {
  history: number[];
  current: number;
  flagged: boolean;
}) => {
  const values = [...history, current];
  const max = Math.max(...values, 1);
  const w = 14;
  const gap = 4;
  const h = 22;
  return (
    <svg
      className="tx-amount-bars"
      width={values.length * (w + gap) - gap}
      height={h}
      role="img"
      aria-label={
        flagged
          ? "Số tiền hiện tại cao bất thường so với các lần trước"
          : "Số tiền hiện tại so với các lần trước"
      }
    >
      {values.map((v, i) => {
        const isCurrent = i === values.length - 1;
        const bh = Math.max(2, (v / max) * h);
        const fill = isCurrent
          ? flagged
            ? "#dc2626"
            : "var(--orange, #f97316)"
          : "#cbd5e1";
        return (
          <rect
            key={i}
            x={i * (w + gap)}
            y={h - bh}
            width={w}
            height={bh}
            rx={2}
            fill={fill}
          />
        );
      })}
    </svg>
  );
};

interface Props {
  draft: TransactionDraft;
  /** Trigger confirmation. OTP + biometric are collected by the App-level
   *  auth overlay (full phone-frame), not inside this card. */
  onConfirm: (sourceAccountId?: string) => void;
  onCancel: () => void;
  onEdit?: () => void;
  /** Submit a new amount for this draft — wired to the orchestrator's
   *  modify-draft path (sends "đổi sang <amount>" so the existing NLU
   *  + draft patch flow handles it without a new endpoint). */
  onModifyAmount?: (amount: number) => void;
  /** Open the split-bill flow from a just-confirmed receipt. App.tsx
   *  resolves it to a contact-picker modal then POST /transactions/split. */
  onSplitBill?: (amount: number, description: string) => void;
  disabled?: boolean;
  /** True while a confirm/cancel HTTP request for THIS draft is still
   *  travelling. Locks both Huỷ and Xác nhận so the user can't fire a
   *  cancel that races a confirm. Closes the SAFETY bug from user
   *  feedback "nhập opt rồi nhấn huỷ nhưng mà sao vẫn chuyển?". */
  inFlight?: boolean;
  actionable?: boolean;
}

export const TransactionCard = ({
  draft,
  onConfirm,
  onCancel,
  onEdit,
  onModifyAmount,
  onSplitBill,
  disabled,
  inFlight = false,
  actionable = true,
}: Props) => {
  const [editingAmount, setEditingAmount] = useState(false);
  const [pendingAmount, setPendingAmount] = useState("");
  const [sourceAccountId, setSourceAccountId] = useState(
    draft.source_account_id ?? draft.source_accounts[0]?.id ?? "",
  );
  // Tracks the actionable → done transition so we can play the success
  // animation exactly once, only when the user just confirmed (vs. an
  // already-completed card rendered from history).
  const wasActionable = useRef(actionable);
  const [justConfirmed, setJustConfirmed] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [confirmedAt] = useState(() => new Date());

  useEffect(() => {
    if (wasActionable.current && !actionable) {
      // Fire celebratory state, then auto-collapse to a compact receipt.
      setJustConfirmed(true);
      const t = window.setTimeout(() => setCollapsed(true), 4000);
      return () => window.clearTimeout(t);
    }
    wasActionable.current = actionable;
  }, [actionable]);
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
    actionable && !disabled && !inFlight && !hardBlocked && !selectedBalanceBlocks && draft.amount != null && r != null;

  const handleConfirm = () => {
    // Defend at the handler — the button's ``disabled`` attr should
    // already block this, but a fast double-click can land both events
    // in the same React batch before re-render. Drop the second one.
    if (inFlight) return;
    // Auth (OTP + optional 8D face scan) is now handled by the App-level
    // overlay that fills the phone frame — the card just kicks it off.
    onConfirm(sourceAccountId || undefined);
  };

  const handleCancel = () => {
    // Once a confirm is travelling we refuse to fire a cancel that
    // would arrive AFTER the transfer is written. Backend has the
    // matching guard in routes/chat.py.
    if (inFlight) return;
    onCancel();
  };

  // Surface the step-up reason as a hero banner so the safety layer is
  // unmistakable in the demo. The same message is also in `draft.flags`
  // below, but a top banner makes the *security posture* visible at a
  // glance instead of buried under the amount.
  //
  // ``fraud_risk_high`` is the Isolation Forest signal — the slide deck's
  // headline fraud metric. Must surface alongside the rule-based warns
  // so judges see the IF model actually drive the OTP step-up.
  const stepUpReason = draft.requires_step_up
    ? draft.flags.find(
        (f) =>
          f.severity === "warn" &&
          (f.code === "new_recipient_large_amount" ||
            f.code === "large_amount" ||
            f.code === "amount_above_average" ||
            f.code === "fraud_risk_high" ||
            f.code === "transfer_velocity_high"),
      )
    : undefined;

  // Compact receipt rendered after the 4s celebration auto-collapses.
  if (collapsed && r && draft.amount != null) {
    const time = confirmedAt.toLocaleTimeString("vi-VN", {
      hour: "2-digit",
      minute: "2-digit",
    });
    return (
      <div className="tx-receipt" role="status" data-testid="tx-receipt">
        <div className="tx-receipt__check" aria-hidden>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
            <path d="M5 12l5 5L20 7" />
          </svg>
        </div>
        <div className="tx-receipt__body">
          <div className="tx-receipt__line">
            Đã chuyển <strong>{formatVND(draft.amount)}</strong> · {r.display_name}
          </div>
          <div className="tx-receipt__time">{time}</div>
        </div>
        {onSplitBill && draft.amount && (
          <button
            type="button"
            className="tx-receipt__split"
            onClick={() => onSplitBill(draft.amount!, draft.description || "")}
            title="Chia tiền với người khác"
          >
            Chia tiền
          </button>
        )}
      </div>
    );
  }

  // Animated celebratory state: shown for ~4s right after confirmation.
  if (justConfirmed && r && draft.amount != null) {
    const time = confirmedAt.toLocaleTimeString("vi-VN", {
      hour: "2-digit",
      minute: "2-digit",
    });
    return (
      <div className="tx-success" role="status" aria-live="polite" data-testid="tx-success">
        <div className="tx-success__confetti" aria-hidden>
          {Array.from({ length: 14 }).map((_, i) => (
            <span key={i} className={`tx-confetti tx-confetti--${i % 7}`} />
          ))}
        </div>
        <div className="tx-success__hero">
          <div className="tx-success__check" aria-hidden>
            <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
              <path d="M5 12l5 5L20 7" />
            </svg>
          </div>
          <div className="tx-success__title">Đã chuyển thành công</div>
          <div className="tx-success__amount">{formatVND(draft.amount)}</div>
          <div className="tx-success__meta">
            cho {r.display_name} · {r.bank} · {time}
          </div>
        </div>
        <div className="tx-success__actions">
          <button
            type="button"
            className="tx-success__action"
            onClick={() => setCollapsed(true)}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d="M5 12h14" />
              <path d="M12 5l7 7-7 7" />
            </svg>
            Chuyển tiếp
          </button>
          <button
            type="button"
            className="tx-success__action"
            onClick={() => setCollapsed(true)}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <circle cx="18" cy="5" r="3" />
              <circle cx="6" cy="12" r="3" />
              <circle cx="18" cy="19" r="3" />
              <path d="M8.6 13.5l6.9 4M15.4 6.5l-6.8 4" />
            </svg>
            Chia sẻ biên lai
          </button>
        </div>
      </div>
    );
  }

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
          {editingAmount && onModifyAmount ? (
            <form
              className="tx-card__amount-edit"
              onSubmit={(e) => {
                e.preventDefault();
                const cleaned = pendingAmount.replace(/[^\d]/g, "");
                const n = parseInt(cleaned, 10);
                if (!isFinite(n) || n <= 0) {
                  setEditingAmount(false);
                  return;
                }
                onModifyAmount(n);
                setEditingAmount(false);
              }}
            >
              <input
                className="tx-card__amount-input"
                type="text"
                inputMode="numeric"
                autoFocus
                value={pendingAmount}
                onChange={(e) =>
                  setPendingAmount(e.target.value.replace(/[^\d.,\s]/g, ""))
                }
                onBlur={() => setEditingAmount(false)}
                aria-label="Số tiền mới (VND)"
                placeholder="Số tiền VND"
              />
              <button
                type="submit"
                className="btn btn--primary tx-card__amount-save"
                disabled={!pendingAmount.replace(/[^\d]/g, "")}
                onMouseDown={(e) => e.preventDefault()}
              >
                Lưu
              </button>
            </form>
          ) : (
            <div className="tx-card__amount-value">
              {formatVND(draft.amount)}
              {draft.predicted_amount && (
                <>
                  <span
                    className="tx-card__predicted-chip"
                    title={
                      draft.amount_prediction_reason ??
                      "Số tiền được đề xuất từ giao dịch trước đây với người này"
                    }
                  >
                    đề xuất từ lịch sử
                  </span>
                  {typeof draft.amount_prediction_confidence === "number" && (
                    <span
                      className="tx-card__confidence-badge"
                      title="Độ tin cậy của dự đoán (từ amount_predictor)"
                    >
                      {Math.round(draft.amount_prediction_confidence * 100)}%
                    </span>
                  )}
                </>
              )}
              {onModifyAmount && actionable && (
                <button
                  type="button"
                  className="tx-card__amount-edit-trigger"
                  onClick={() => {
                    setPendingAmount(String(draft.amount ?? ""));
                    setEditingAmount(true);
                  }}
                  aria-label="Sửa số tiền"
                  title="Sửa số tiền"
                >
                  ✎
                </button>
              )}
            </div>
          )}
          {draft.category && <CategoryChip category={draft.category} />}
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
                {draft.recent_to_recipient && draft.recent_to_recipient.length > 0 && (
                  <ul
                    className="tx-recent-mini"
                    aria-label={`${draft.recent_to_recipient.length} giao dịch gần đây với ${r.display_name}`}
                  >
                    {draft.recent_to_recipient.slice(0, 3).map((tx, i) => {
                      const date = new Date(tx.created_at);
                      const dm =
                        isNaN(date.getTime())
                          ? ""
                          : `${String(date.getDate()).padStart(2, "0")}/${String(
                              date.getMonth() + 1,
                            ).padStart(2, "0")}`;
                      return (
                        <li key={i} className="tx-recent-mini__row">
                          <span className="tx-recent-mini__amount">
                            {formatVND(tx.amount)}
                          </span>
                          {dm && (
                            <span className="tx-recent-mini__date">· {dm}</span>
                          )}
                          {tx.description && (
                            <span
                              className="tx-recent-mini__desc"
                              title={tx.description}
                            >
                              · {tx.description}
                            </span>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                )}
                {draft.recent_to_recipient &&
                  draft.recent_to_recipient.length > 0 &&
                  draft.amount != null && (
                    <AmountVsHistoryBars
                      history={draft.recent_to_recipient
                        .slice(0, 3)
                        .map((t) => t.amount)
                        .reverse()}
                      current={draft.amount}
                      flagged={draft.flags.some(
                        (f) =>
                          f.code === "amount_above_average" ||
                          f.code === "fraud_risk_high" ||
                          f.code === "large_amount" ||
                          f.code === "new_recipient_large_amount",
                      )}
                    />
                  )}
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
          {draft.flags.map((f, i) => {
            const d = f.details as
              | {
                  kind?: string;
                  median?: number;
                  p90?: number;
                  n_samples?: number;
                  ratio?: number;
                  current_amount?: number;
                  score?: number;
                  threshold?: number;
                  n_train?: number;
                  category_label?: string;
                  monthly_limit_vnd?: number;
                  spent_vnd?: number;
                  projected_vnd?: number;
                  overshoot_vnd?: number;
                  recent_count?: number;
                  window_sec?: number;
                }
              | null
              | undefined;
            const showWhy =
              d?.kind === "per_recipient" &&
              typeof d.median === "number" &&
              typeof d.p90 === "number" &&
              typeof d.n_samples === "number";
            // Isolation Forest detail block — mirrors the "why" panel for
            // amount_above_average but for the per-user fraud model.
            const showFraudWhy =
              d?.kind === "fraud_model" &&
              typeof d.score === "number" &&
              typeof d.threshold === "number";
            // Budget overshoot detail block — judges who set a budget then
            // breach it shouldn't have to do mental arithmetic; render the
            // limit / spent / projected / overshoot triangle.
            const showBudgetWhy =
              d?.kind === "budget_overshoot" &&
              typeof d.monthly_limit_vnd === "number" &&
              typeof d.spent_vnd === "number" &&
              typeof d.projected_vnd === "number";
            // Velocity detail block — N transfers in W seconds.
            // Surfaces the exact count + window so judges see what
            // tripped the velocity guard instead of having to infer it
            // from the prose message.
            const showVelocityWhy =
              d?.kind === "velocity" &&
              typeof d.recent_count === "number" &&
              typeof d.window_sec === "number";
            return (
              <div key={i} className={`tx-flag tx-flag--${f.severity}`}>
                <div>
                  {f.severity === "block"
                    ? "⛔"
                    : f.severity === "warn"
                    ? "⚠️"
                    : "ℹ️"}{" "}
                  {f.message}
                </div>
                {showWhy && (
                  <div className="tx-flag__why">
                    <div>
                      Trung vị: <strong>{formatVND(d!.median!)}</strong> · p90:{" "}
                      <strong>{formatVND(d!.p90!)}</strong>
                    </div>
                    <div>
                      Mẫu: {d!.n_samples} giao dịch · Lần này gấp{" "}
                      <strong>{d!.ratio?.toFixed(1)}×</strong> trung vị
                    </div>
                  </div>
                )}
                {showFraudWhy && (
                  <div className="tx-flag__why">
                    <div>
                      Điểm rủi ro:{" "}
                      <strong>{Math.round((d!.score ?? 0) * 100)}%</strong>{" "}
                      · Ngưỡng cảnh báo:{" "}
                      <strong>{Math.round((d!.threshold ?? 0) * 100)}%</strong>
                    </div>
                    {typeof d!.n_train === "number" && (
                      <div>
                        Mô hình Isolation Forest huấn luyện trên{" "}
                        <strong>{d!.n_train}</strong> giao dịch của bạn.
                      </div>
                    )}
                  </div>
                )}
                {showBudgetWhy && (
                  <div className="tx-flag__why">
                    <div>
                      Đã chi tháng này:{" "}
                      <strong>{formatVND(d!.spent_vnd!)}</strong> / Hạn mức{" "}
                      <strong>{formatVND(d!.monthly_limit_vnd!)}</strong>
                      {d!.category_label ? ` · ${d!.category_label}` : ""}
                    </div>
                    <div>
                      Sau giao dịch:{" "}
                      <strong>{formatVND(d!.projected_vnd!)}</strong>
                      {typeof d!.overshoot_vnd === "number" && (
                        <>
                          {" · "}Vượt{" "}
                          <strong>{formatVND(d!.overshoot_vnd!)}</strong>
                        </>
                      )}
                    </div>
                  </div>
                )}
                {showVelocityWhy && (
                  <div className="tx-flag__why">
                    <div>
                      Đã có{" "}
                      <strong>{d!.recent_count}</strong> giao dịch trong{" "}
                      <strong>{d!.window_sec}</strong> giây
                      {typeof d!.threshold === "number" && (
                        <>
                          {" · "}Ngưỡng:{" "}
                          <strong>{d!.threshold}</strong> giao dịch
                        </>
                      )}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {actionable ? (
        <>
          <div className="tx-actions">
            <button
              className="btn btn--ghost"
              onClick={handleCancel}
              disabled={disabled || inFlight}
              title={inFlight ? "Đang xử lý — không thể huỷ" : undefined}
            >
              Huỷ
            </button>
            {onEdit && (
              <button
                className="btn btn--ghost"
                onClick={onEdit}
                disabled={disabled || inFlight}
              >
                Sửa
              </button>
            )}
            <button
              className={`btn ${draft.requires_step_up ? "btn--warn" : "btn--primary"}`}
              onClick={handleConfirm}
              disabled={!canSubmit}
              aria-busy={inFlight}
              data-onboarding="confirm"
            >
              {inFlight ? "Đang xử lý…" : "Xác nhận"}
            </button>
          </div>
        </>
      ) : (
        <div className="tx-status">Giao dịch này đã được xử lý.</div>
      )}
    </div>
  );
};
