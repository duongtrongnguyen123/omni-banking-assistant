import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { RecentRecipient } from "../types";

const PeopleIcon = () => (
  <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
    <path
      d="M9 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm7-1a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM2 20c0-3.3 3.1-6 7-6s7 2.7 7 6v1H2v-1Zm15.5-5.5c2.6.4 4.5 2.4 4.5 4.8V21h-5v-1c0-2-.9-3.8-2.3-5 .9-.2 1.8-.3 2.8 0Z"
      fill="currentColor"
    />
  </svg>
);

export const RecentRecipients = ({
  onPick,
  disabled,
}: {
  onPick: (display_name: string) => void;
  disabled?: boolean;
}) => {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<RecentRecipient[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const toggle = async () => {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    if (items === null) {
      try {
        const data = await api.recentRecipients(5);
        setItems(data);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    }
  };

  const pick = (r: RecentRecipient) => {
    setOpen(false);
    onPick(r.contact.label || r.contact.display_name);
  };

  return (
    <div className="recents" ref={wrapRef}>
      <button
        type="button"
        className="btn btn--ghost btn--icon recents__trigger"
        onClick={toggle}
        disabled={disabled}
        aria-label="Người nhận gần đây"
        aria-haspopup="listbox"
        aria-expanded={open}
        title="Người nhận gần đây"
      >
        <PeopleIcon />
      </button>
      {open && (
        <div className="recents__popover" role="listbox">
          <div className="recents__title">Người nhận gần đây</div>
          {items === null && !error && (
            <div className="recents__empty">Đang tải…</div>
          )}
          {error && <div className="recents__empty">Lỗi: {error}</div>}
          {items && items.length === 0 && (
            <div className="recents__empty">Chưa có giao dịch nào.</div>
          )}
          {items?.map((r) => {
            const initial = r.contact.display_name.trim().charAt(0).toUpperCase();
            return (
              <button
                key={r.contact.id}
                type="button"
                className="recents__row"
                onClick={() => pick(r)}
                role="option"
              >
                <span className="recents__avatar">{initial}</span>
                <span className="recents__meta">
                  <span className="recents__name">
                    {r.contact.display_name}
                    {r.contact.label && (
                      <span className="recents__label">· {r.contact.label}</span>
                    )}
                  </span>
                  <span className="recents__sub">
                    {r.contact.bank} {r.contact.account_masked}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
};
