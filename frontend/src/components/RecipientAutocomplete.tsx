import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { Contact, RecipientSuggestion } from "../types";

interface Props {
  open: boolean;
  query: string; // text after the last "@", before the cursor
  onPick: (contact: Contact) => void;
  onClose: () => void;
}

/**
 * "@-mention" style recipient autocomplete.
 *
 * Triggered from App.tsx when the user types "@" in the chat input. We
 * pull the ranked-contacts feed (already sorted by recent usage + ML
 * suggester score) and filter locally as the user types. Arrow keys are
 * handled by the parent so they integrate with the regular input.
 *
 * Visible labels stay Vietnamese to match the rest of the UI.
 */
export const RecipientAutocomplete = ({ open, query, onPick, onClose }: Props) => {
  const [all, setAll] = useState<RecipientSuggestion[]>([]);
  const [active, setActive] = useState(0);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (!open || loaded) return;
    api
      .rankedContacts()
      .then((items) => {
        setAll(items);
        setLoaded(true);
      })
      .catch(() => setAll([]));
  }, [open, loaded]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const items = !q
      ? all
      : all.filter((s) => {
          const c = s.contact;
          return (
            c.display_name.toLowerCase().includes(q) ||
            c.bank.toLowerCase().includes(q) ||
            (c.aliases || []).some((a) => a.toLowerCase().includes(q))
          );
        });
    return items.slice(0, 6);
  }, [all, query]);

  // Keep active index in range when filtered shrinks.
  useEffect(() => {
    if (active >= filtered.length) setActive(0);
  }, [filtered.length, active]);

  // Expose a tiny imperative-ish API via window so the input keydown
  // handler (in App.tsx) can drive selection without prop drilling refs.
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (!open || filtered.length === 0) return;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActive((i) => (i + 1) % filtered.length);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setActive((i) => (i - 1 + filtered.length) % filtered.length);
      } else if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        const pick = filtered[active];
        if (pick) onPick(pick.contact);
      } else if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [open, filtered, active, onPick, onClose]);

  if (!open) return null;

  return (
    <div className="autocomplete" role="listbox" aria-label="Gợi ý danh bạ">
      {filtered.length === 0 && (
        <div className="autocomplete__empty">Không có danh bạ phù hợp.</div>
      )}
      {filtered.map((s, i) => {
        const c = s.contact;
        const initial = c.display_name.split(" ").slice(-1)[0][0] || "?";
        return (
          <button
            key={c.id}
            type="button"
            role="option"
            aria-selected={i === active}
            className={
              "autocomplete__row " +
              (i === active ? "autocomplete__row--active" : "")
            }
            onMouseEnter={() => setActive(i)}
            onClick={() => onPick(c)}
          >
            <div className="autocomplete__avatar">{initial}</div>
            <div className="autocomplete__main">
              <div className="autocomplete__name">{c.display_name}</div>
              <div className="autocomplete__meta">
                {c.bank} · {c.account_masked}
              </div>
            </div>
          </button>
        );
      })}
      <div className="autocomplete__footer">
        ↑↓ chọn · Enter chèn · Esc đóng
      </div>
    </div>
  );
};
