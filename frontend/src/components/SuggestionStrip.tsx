import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { AtmHit, RecipientSuggestion } from "../types";
import { AtmFinderButton } from "./AtmFinderButton";

interface Props {
  refreshKey: number;
  busy: boolean;
  onPick: (text: string) => void;
  onAtms?: (atms: AtmHit[], note?: string) => void;
}

/**
 * Horizontal strip of ML-ranked next-recipient suggestions just above the
 * chat input. The tree + rule + frequency mix lives in
 * `backend/app/ml/suggester.py`; this surfaces it so judges see the model
 * driving the UI in a single tap.
 *
 * Re-fetched whenever `refreshKey` changes (after every executed transfer).
 */
export const SuggestionStrip = ({ refreshKey, busy, onPick, onAtms }: Props) => {
  const [items, setItems] = useState<RecipientSuggestion[]>([]);

  useEffect(() => {
    let cancelled = false;
    api
      .suggestions(4)
      .then((rows) => {
        if (cancelled) return;
        // Drop near-zero score rows; the model is honest about "I don't know"
        // for users with sparse data, and we'd rather show nothing than noise.
        setItems(rows.filter((r) => r.score > 0.02));
      })
      .catch(() => setItems([]));
    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  // The ATM pill is useful even with no recipient suggestions to show
  // (sparse data, demo first turn). So keep rendering when at least the
  // pill or 2+ recipients are available.
  if (items.length < 2 && !onAtms) return null;

  // Bar widths are normalised to the strongest signal in the visible
  // set — judges see the *relative* model confidence between chips, which
  // is more readable than raw 0..1 score values and stays meaningful
  // even when the absolute numbers drift.
  const maxScore = items.reduce((m, s) => Math.max(m, s.score), 0) || 1;

  return (
    <div className="suggest-strip" aria-label="Gợi ý người nhận">
      <div className="suggest-strip__title">
        <span className="suggest-strip__dot" /> Gợi ý cho bạn lúc này
      </div>
      <div className="suggest-strip__list">
        {items.map((s) => {
          const c = s.contact;
          const first = c.display_name.split(" ").slice(-1)[0];
          const pct = Math.max(8, Math.round((s.score / maxScore) * 100));
          return (
            <button
              key={c.id}
              className="suggest-chip"
              disabled={busy}
              onClick={() => onPick(`chuyển cho ${c.display_name} `)}
              title={`${s.reason} · score ${s.score.toFixed(3)}`}
            >
              <span className="suggest-chip__name">{first}</span>
              <span className="suggest-chip__reason">{s.reason}</span>
              <span
                className="suggest-chip__bar"
                aria-label={`Mức độ ưu tiên ${pct}%`}
              >
                <span
                  className="suggest-chip__bar-fill"
                  style={{ width: `${pct}%` }}
                />
              </span>
            </button>
          );
        })}
        {onAtms && (
          <AtmFinderButton busy={busy} onAtms={onAtms} />
        )}
      </div>
    </div>
  );
};
