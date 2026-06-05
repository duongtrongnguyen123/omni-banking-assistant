import { useEffect, useMemo, useState } from "react";

export interface SlashCommand {
  /** What the user types after "/" — case-insensitive prefix match. */
  key: string;
  /** Vietnamese label shown in the popover. */
  label: string;
  /** Short hint shown on the right. */
  hint: string;
  /** Usage template, e.g. "/transfer <tên> <số tiền>". */
  usage?: string;
  /**
   * Action emitted on selection. Strings are interpreted as chat
   * messages to send; the special "__action__:<name>" strings are
   * dispatched as UI actions in App.tsx.
   */
  action:
    | { kind: "send"; text: string }
    | { kind: "prefill"; text: string }
    | { kind: "ui"; name: "insights" | "clear" | "lang_en" };
}

/**
 * Returns the static slash command catalog. Vietnamese labels match the
 * rest of the UI; the actual underlying messages sent to /api/chat are
 * also Vietnamese — the slash key is just a power-user shortcut.
 */
export const SLASH_COMMANDS: SlashCommand[] = [
  {
    key: "transfer",
    label: "Chuyển tiền",
    hint: "Soạn lệnh chuyển",
    usage: "/transfer <tên> <số tiền>",
    action: { kind: "prefill", text: "chuyển cho " },
  },
  {
    key: "balance",
    label: "Số dư",
    hint: "Xem số dư các tài khoản",
    action: { kind: "send", text: "số dư" },
  },
  {
    key: "history",
    label: "Lịch sử",
    hint: "Lịch sử chi tiêu",
    usage: "/history [tháng này | tháng trước]",
    action: { kind: "send", text: "lịch sử tháng này" },
  },
  {
    key: "repeat",
    label: "Lặp lại",
    hint: "Lặp lại giao dịch trước",
    action: { kind: "send", text: "lặp lại lần trước" },
  },
  {
    key: "insights",
    label: "Insights",
    hint: "Mở thẻ gợi ý",
    action: { kind: "ui", name: "insights" },
  },
  {
    key: "help",
    label: "Trợ giúp",
    hint: "Xem danh sách tính năng",
    action: { kind: "send", text: "/help" },
  },
  {
    key: "lang en",
    label: "Switch to English",
    hint: "Đổi sang tiếng Anh",
    action: { kind: "ui", name: "lang_en" },
  },
  {
    key: "clear",
    label: "Xoá đoạn chat",
    hint: "Xoá toàn bộ tin nhắn (có xác nhận)",
    action: { kind: "ui", name: "clear" },
  },
];

interface Props {
  open: boolean;
  query: string; // text after the leading "/", e.g. "tr" for "/tr"
  onPick: (cmd: SlashCommand, raw: string) => void;
  onClose: () => void;
  /** The raw input text at trigger time — used so /transfer Nam 50k still routes. */
  rawInput: string;
}

const matches = (cmd: SlashCommand, q: string): boolean => {
  if (!q) return true;
  const key = cmd.key.toLowerCase();
  const ql = q.toLowerCase();
  // First-word match is enough; also allow full-key prefix for "lang en".
  return key.startsWith(ql) || key.split(" ")[0].startsWith(ql);
};

/**
 * Popover above the chat input that appears the moment the user types
 * "/". Arrow keys + Enter are wired here (via a global keydown listener
 * scoped to when the palette is open) so the chat <input> doesn't need
 * to know about commands directly.
 *
 * Closes when:
 *  - Esc pressed
 *  - the input no longer starts with "/" (handled by parent)
 *  - the first letter no longer matches any command (parent passes
 *    onClose when filtered list goes empty)
 */
export const SlashPalette = ({
  open,
  query,
  onPick,
  onClose,
  rawInput,
}: Props) => {
  const [active, setActive] = useState(0);
  const filtered = useMemo(
    () => SLASH_COMMANDS.filter((c) => matches(c, query)),
    [query],
  );

  useEffect(() => {
    if (active >= filtered.length) setActive(0);
  }, [filtered.length, active]);

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
      } else if (e.key === "Enter") {
        e.preventDefault();
        const pick = filtered[active];
        if (pick) onPick(pick, rawInput);
      } else if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [open, filtered, active, onPick, onClose, rawInput]);

  if (!open) return null;

  return (
    <div
      className="slash-palette"
      id="omni-slash-palette"
      role="listbox"
      aria-label="Danh sách lệnh nhanh"
    >
      <div className="slash-palette__title">Lệnh nhanh</div>
      {filtered.length === 0 && (
        <div className="slash-palette__empty">Không có lệnh phù hợp.</div>
      )}
      {filtered.map((cmd, i) => (
        <button
          type="button"
          key={cmd.key}
          role="option"
          aria-selected={i === active}
          className={
            "slash-palette__row " +
            (i === active ? "slash-palette__row--active" : "")
          }
          onMouseEnter={() => setActive(i)}
          onClick={() => onPick(cmd, rawInput)}
        >
          <div className="slash-palette__cmd">/{cmd.key}</div>
          <div className="slash-palette__body">
            <div className="slash-palette__label">{cmd.label}</div>
            <div className="slash-palette__hint">
              {cmd.usage ?? cmd.hint}
            </div>
          </div>
        </button>
      ))}
      <div className="slash-palette__footer">
        ↑↓ chọn · Enter chạy · Esc đóng
      </div>
    </div>
  );
};

/**
 * Parse a raw input like "/transfer Nam 50k" into a chat message string.
 * Used by App.tsx when the user hits Enter on a slash command that
 * accepts arguments. Returns null for commands without a parseable form.
 */
export const buildMessageFromSlash = (
  cmd: SlashCommand,
  rawInput: string,
): string | null => {
  const rest = rawInput.replace(/^\s*\/\S+\s*/, "").trim();
  if (cmd.key === "transfer") {
    // "/transfer Nam 50k" → "chuyển cho Nam 50k"
    if (!rest) return null;
    return `chuyển cho ${rest}`;
  }
  if (cmd.key === "history") {
    // "/history tháng trước" → "lịch sử tháng trước"
    if (!rest) return "lịch sử tháng này";
    return `lịch sử ${rest}`;
  }
  if (cmd.action.kind === "send") return cmd.action.text;
  return null;
};
