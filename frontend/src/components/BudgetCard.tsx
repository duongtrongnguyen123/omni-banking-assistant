import { useEffect, useState } from "react";
import { api } from "../api/client";
import { formatVND } from "../format";
import type { BudgetRow } from "../types";

/** Sidebar widget showing each monthly budget vs this month's spend.
 *
 * Colour rule:
 *   ratio < 0.8  → green (healthy)
 *   0.8 ≤ r < 1  → orange (warning)
 *   ratio ≥ 1.0  → red (over budget)
 */

function barColour(ratio: number): string {
  if (ratio >= 1.0) return "budget-card__bar--red";
  if (ratio >= 0.8) return "budget-card__bar--orange";
  return "budget-card__bar--green";
}

interface Props {
  refreshKey?: number;
}

export const BudgetCard = ({ refreshKey = 0 }: Props) => {
  const [rows, setRows] = useState<BudgetRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .budgets()
      .then((d) => {
        if (!cancelled) setRows(d);
      })
      .catch((e) => {
        if (!cancelled) setError(String(e instanceof Error ? e.message : e));
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  if (error) {
    return (
      <div className="budget-card">
        <div className="budget-card__title">Ngân sách tháng này</div>
        <div className="budget-card__empty">Không tải được ngân sách: {error}</div>
      </div>
    );
  }

  if (rows === null) {
    return (
      <div className="budget-card">
        <div className="budget-card__title">Ngân sách tháng này</div>
        <div className="budget-card__empty">Đang tải…</div>
      </div>
    );
  }

  if (rows.length === 0) {
    return (
      <div className="budget-card">
        <div className="budget-card__title">Ngân sách tháng này</div>
        <div className="budget-card__empty">
          Bạn chưa đặt ngân sách nào. Nhắn cho Omni: "đặt ngân sách ăn uống 3 triệu".
        </div>
      </div>
    );
  }

  return (
    <div className="budget-card">
      <div className="budget-card__title">Ngân sách tháng này</div>
      <ul className="budget-card__list">
        {rows.map((b) => {
          const pct = Math.min(100, Math.round(b.ratio * 100));
          const remaining = Math.max(b.remaining_vnd, 0);
          return (
            <li key={b.id} className="budget-card__row">
              <div className="budget-card__row-head">
                <span className="budget-card__cat">{b.category_label}</span>
                <span className="budget-card__amounts">
                  {formatVND(b.spent_vnd)} / {formatVND(b.monthly_limit_vnd)}
                </span>
              </div>
              <div
                className="budget-card__bar-track"
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={pct}
                aria-label={`Đã tiêu ${pct}% ngân sách ${b.category_label}`}
              >
                <div
                  className={`budget-card__bar ${barColour(b.ratio)}`}
                  style={{ width: `${Math.min(100, Math.max(2, pct))}%` }}
                />
              </div>
              <div className="budget-card__row-foot">
                {b.ratio >= 1.0 ? (
                  <span className="budget-card__over">
                    Vượt {formatVND(b.spent_vnd - b.monthly_limit_vnd)}
                  </span>
                ) : (
                  <span>Còn {formatVND(remaining)}</span>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
};
