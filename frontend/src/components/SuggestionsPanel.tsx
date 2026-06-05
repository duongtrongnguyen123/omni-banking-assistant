import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { RecipientSuggestion } from "../types";

interface Props {
  onPick: (text: string) => void;
  refreshKey?: number;
}

/**
 * Sidebar widget powered by the tree-based suggester
 * (backend: app/ml/suggester.py). Re-fetches every time
 * ``refreshKey`` changes — App bumps it after each transfer.
 */
export const SuggestionsPanel = ({ onPick, refreshKey = 0 }: Props) => {
  const [items, setItems] = useState<RecipientSuggestion[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api
      .suggestions(5)
      .then(setItems)
      .catch(() => setItems([]))
      .finally(() => setLoading(false));
  }, [refreshKey]);

  const subtitle = new Date().toLocaleDateString("vi-VN", {
    weekday: "long",
    day: "numeric",
    month: "numeric",
  });

  return (
    <div className="suggest-panel">
      <div className="suggest-panel__title">Có thể bạn muốn chuyển cho…</div>
      <div className="suggest-panel__date">{subtitle} · mô hình tree-based</div>
      {loading && <div className="suggest-panel__empty">Đang xếp hạng…</div>}
      {!loading && items.length === 0 && (
        <div className="suggest-panel__empty">
          Chưa đủ lịch sử để gợi ý — thử chuyển vài lần để mô hình học.
        </div>
      )}
      <ul className="suggest-list">
        {items.map((s, i) => {
          const c = s.contact;
          const initial = c.display_name.split(" ").slice(-1)[0][0] || "?";
          return (
            <li key={c.id}>
              <button
                className="suggest-row"
                onClick={() => onPick(`chuyển cho ${c.display_name.split(" ").slice(-1)[0]} `)}
                title={`Gợi ý #${i + 1} · score ${s.score.toFixed(3)}`}
              >
                <div className="suggest-row__avatar">{initial}</div>
                <div className="suggest-row__main">
                  <div className="suggest-row__name">
                    {c.display_name}
                    {c.label && <span className="suggest-row__label"> · {c.label}</span>}
                  </div>
                  <div className="suggest-row__meta">
                    {c.bank} · {c.account_masked}
                  </div>
                  <div className="suggest-row__reason">{s.reason}</div>
                </div>
                <div className="suggest-row__score" aria-hidden>
                  <span style={{ height: `${Math.min(100, s.score * 100 * 3)}%` }} />
                </div>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
};
