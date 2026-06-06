import type { HistoryResult } from "../types";
import { formatVND, formatDateTime } from "../format";

// Compact category metadata for history rows — keys mirror the
// categoriser keys used in TransactionCard. Kept small (no emoji) so
// the row stays single-line on the phone frame.
const CATEGORY_META: Record<string, { label: string; bg: string; fg: string }> = {
  food:          { label: "Ăn uống",      bg: "#fff1e6", fg: "#b25a13" },
  transport:     { label: "Di chuyển",    bg: "#e6f0ff", fg: "#1a4fb0" },
  groceries:     { label: "Đi chợ",       bg: "#e9f7e6", fg: "#2f7a25" },
  entertainment: { label: "Giải trí",     bg: "#f3e6ff", fg: "#6b1fb0" },
  health:        { label: "Sức khoẻ",     bg: "#ffe6ea", fg: "#b01a3a" },
  rent:          { label: "Nhà cửa",      bg: "#fff4e0", fg: "#a55a00" },
  utilities:     { label: "Hoá đơn",      bg: "#fff8d6", fg: "#7a6800" },
  gifts:         { label: "Quà / Mừng",   bg: "#ffe0ec", fg: "#b01a6a" },
  savings:       { label: "Tiết kiệm",    bg: "#e0f5ee", fg: "#0f7a55" },
  family:        { label: "Gia đình",     bg: "#fde6f4", fg: "#a01a78" },
  friends:       { label: "Bạn bè",       bg: "#e6f0f7", fg: "#1a5a78" },
  work:          { label: "Công việc",    bg: "#eef0f4", fg: "#3a4255" },
};

const CategoryTag = ({ category }: { category: string }) => {
  const meta = CATEGORY_META[category];
  if (!meta) return null;
  return (
    <span
      className="hist-list__cat"
      style={{
        display: "inline-block",
        marginLeft: 6,
        padding: "1px 6px",
        borderRadius: 6,
        fontSize: 10,
        fontWeight: 600,
        background: meta.bg,
        color: meta.fg,
        verticalAlign: "middle",
      }}
      title={`Danh mục tự nhận diện: ${meta.label}`}
    >
      {meta.label}
    </span>
  );
};

export const HistoryCard = ({ history }: { history: HistoryResult }) => {
  const periodLabel = history.period === "this_month" ? "Tháng này" : "Tháng trước";
  return (
    <div className="hist-card">
      <div className="hist-card__period">{periodLabel}</div>
      <div className="hist-card__total">{formatVND(history.total)}</div>
      <div className="hist-card__meta">
        {history.count} giao dịch · TB {formatVND(history.average)}/lần
      </div>
      {history.items.length > 0 && (
        <ul className="hist-list">
          {history.items.slice(0, 5).map((t) => (
            <li key={t.id} className="hist-list__item">
              <div className="hist-list__main">
                <div className="hist-list__name">{t.contact.display_name}</div>
                <div className="hist-list__desc">
                  {t.description}
                  {t.category && <CategoryTag category={t.category} />}
                </div>
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
