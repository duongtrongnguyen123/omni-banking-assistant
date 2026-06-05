import type { HistoryResult } from "../types";
import { formatVND, formatDateTime } from "../format";

export const HistoryCard = ({ history }: { history: HistoryResult }) => {
  const periodLabel = history.period === "this_month" ? "Tháng này" : "Tháng trước";
  return (
    <div className="hist-card" data-testid="history-card">
      <div className="hist-card__period" data-testid="history-period">{periodLabel}</div>
      <div className="hist-card__total" data-testid="history-total">{formatVND(history.total)}</div>
      <div className="hist-card__meta">
        {history.count} giao dịch · TB {formatVND(history.average)}/lần
      </div>
      {history.items.length > 0 && (
        <ul className="hist-list">
          {history.items.slice(0, 5).map((t) => (
            <li key={t.id} className="hist-list__item">
              <div className="hist-list__main">
                <div className="hist-list__name">{t.contact.display_name}</div>
                <div className="hist-list__desc">{t.description}</div>
              </div>
              <div className="hist-list__right">
                <div className="hist-list__amount">-{formatVND(t.amount)}</div>
                <div className="hist-list__date">{formatDateTime(t.created_at)}</div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
};
