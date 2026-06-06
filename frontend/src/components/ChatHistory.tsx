import type { ChatSession } from "../types";

interface ChatHistoryProps {
  open: boolean;
  onClose: () => void;
  sessions: ChatSession[];
  currentSessionId: string | null;
  loading: boolean;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
}

/** Vietnamese relative-time label for a conversation's last activity. */
function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const diffSec = Math.max(0, (Date.now() - then) / 1000);
  if (diffSec < 60) return "vừa xong";
  const min = Math.floor(diffSec / 60);
  if (min < 60) return `${min} phút trước`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} giờ trước`;
  const day = Math.floor(hr / 24);
  if (day < 7) return `${day} ngày trước`;
  return new Date(iso).toLocaleDateString("vi-VN");
}

/**
 * Left-hand conversation list, modelled on the history panel in other
 * AI chat UIs. Rendered as a slide-in drawer toggled from the chat
 * header so it doesn't disturb the existing two-column layout.
 */
export function ChatHistory({
  open,
  onClose,
  sessions,
  currentSessionId,
  loading,
  onSelect,
  onNew,
  onDelete,
}: ChatHistoryProps) {
  return (
    <>
      <div
        className={`chat-history__backdrop ${open ? "chat-history__backdrop--open" : ""}`}
        onClick={onClose}
        aria-hidden="true"
      />
      <aside
        className={`chat-history ${open ? "chat-history--open" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label="Lịch sử trò chuyện"
        aria-hidden={!open}
      >
        <div className="chat-history__head">
          <span className="chat-history__title">Cuộc trò chuyện</span>
          <button
            type="button"
            className="chat-history__close"
            onClick={onClose}
            aria-label="Đóng lịch sử"
          >
            <span aria-hidden="true">×</span>
          </button>
        </div>

        <button type="button" className="chat-history__new" onClick={onNew}>
          <svg
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.4"
            strokeLinecap="round"
            aria-hidden="true"
          >
            <path d="M12 5v14M5 12h14" />
          </svg>
          Cuộc trò chuyện mới
        </button>

        <div className="chat-history__list">
          {loading && (
            <div className="chat-history__empty">Đang tải…</div>
          )}
          {!loading && sessions.length === 0 && (
            <div className="chat-history__empty">
              Chưa có cuộc trò chuyện nào được lưu.
            </div>
          )}
          {sessions.map((s) => (
            <div
              key={s.id}
              className={`chat-history__item ${
                s.id === currentSessionId ? "chat-history__item--active" : ""
              }`}
            >
              <button
                type="button"
                className="chat-history__item-main"
                onClick={() => onSelect(s.id)}
                title={s.title || "Cuộc trò chuyện mới"}
              >
                <span className="chat-history__item-title">
                  {s.title || "Cuộc trò chuyện mới"}
                </span>
                <span className="chat-history__item-meta">
                  {relativeTime(s.updated_at)}
                  {s.message_count ? ` · ${s.message_count} tin nhắn` : ""}
                </span>
              </button>
              <button
                type="button"
                className="chat-history__item-del"
                onClick={() => onDelete(s.id)}
                aria-label={`Xoá cuộc trò chuyện ${s.title || ""}`}
                title="Xoá"
              >
                <svg
                  width="15"
                  height="15"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  aria-hidden="true"
                >
                  <path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14" />
                </svg>
              </button>
            </div>
          ))}
        </div>
      </aside>
    </>
  );
}
