import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { RecipientSuggestion } from "../types";

interface Props {
  open: boolean;
  onClose: () => void;
  onPick: (text: string) => void;
}

/**
 * Full-height contact picker overlaying the phone frame.
 * - Shows every contact in the user's book
 * - Ordered by the tree + frequency model (see backend/app/ml/suggester.py)
 * - One tap pre-fills the chat input with "chuyển cho <Tên> " — the user
 *   types the amount and hits send. Faster than typing a name for users
 *   with a small contact book.
 */
export const ContactPicker = ({ open, onClose, onPick }: Props) => {
  const [items, setItems] = useState<RecipientSuggestion[]>([]);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");
  // Hold the element that was focused before the dialog opened so we
  // can restore focus on close — best practice for modal dialogs
  // (WCAG 2.4.3 Focus Order + APG dialog pattern).
  const previouslyFocused = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    previouslyFocused.current = (document.activeElement as HTMLElement) ?? null;
    setLoading(true);
    api
      .rankedContacts()
      .then(setItems)
      .catch(() => setItems([]))
      .finally(() => setLoading(false));
    return () => {
      // Restore focus on close.
      previouslyFocused.current?.focus?.();
    };
  }, [open]);

  // Esc closes the dialog (WCAG dialog pattern).
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const filtered = query.trim()
    ? items.filter((s) => {
        const q = query.toLowerCase();
        const c = s.contact;
        return (
          c.display_name.toLowerCase().includes(q) ||
          c.bank.toLowerCase().includes(q) ||
          (c.aliases || []).some((a) => a.toLowerCase().includes(q))
        );
      })
    : items;

  return (
    <div
      className="picker"
      role="dialog"
      aria-modal="true"
      aria-labelledby="picker-title"
    >
      <header className="picker__header">
        <button
          className="picker__close"
          onClick={onClose}
          aria-label="Đóng danh bạ"
        >
          <span aria-hidden="true">←</span>
        </button>
        <div className="picker__title" id="picker-title">Danh bạ</div>
        <div className="picker__hint">Xếp theo gợi ý hôm nay</div>
      </header>
      <div className="picker__search">
        <label htmlFor="picker-search" className="sr-only">
          Tìm danh bạ
        </label>
        <input
          id="picker-search"
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Tìm tên / ngân hàng / biệt danh"
          aria-label="Tìm danh bạ"
        />
      </div>
      <div className="picker__list">
        {loading && <div className="picker__empty">Đang xếp hạng…</div>}
        {!loading && filtered.length === 0 && (
          <div className="picker__empty">Không tìm thấy ai.</div>
        )}
        {!loading &&
          filtered.map((s) => {
            const c = s.contact;
            const initial =
              c.display_name.split(" ").slice(-1)[0][0] || "?";
            return (
              <button
                key={c.id}
                className="picker__row"
                onClick={() => {
                  onPick(`chuyển cho ${c.display_name} `);
                  onClose();
                }}
              >
                <div className="picker__avatar">{initial}</div>
                <div className="picker__main">
                  <div className="picker__name">
                    {c.display_name}
                    {c.label && (
                      <span className="picker__label"> · {c.label}</span>
                    )}
                  </div>
                  <div className="picker__meta">
                    {c.bank} · {c.account_masked}
                  </div>
                </div>
                {s.score > 0 && (
                  <span
                    className="picker__chip"
                    title={`Gợi ý hôm nay · ${s.score.toFixed(3)}`}
                    aria-hidden="true"
                  />
                )}
              </button>
            );
          })}
      </div>
    </div>
  );
};
