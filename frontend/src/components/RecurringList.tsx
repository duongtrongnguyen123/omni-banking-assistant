import type { RecurringPattern } from "../types";
import { formatVND } from "../format";

interface Props {
  patterns: RecurringPattern[];
  onSchedule: (text: string) => void;
}

const confidenceClass = (c: number): string => {
  if (c >= 0.8) return "rec-dot rec-dot--good";
  if (c >= 0.5) return "rec-dot rec-dot--warn";
  return "rec-dot rec-dot--mute";
};

const confidenceLabel = (c: number): string => {
  if (c >= 0.8) return "Tin cậy cao";
  if (c >= 0.5) return "Tin cậy vừa";
  return "Tham khảo";
};

const firstLetter = (name: string | null | undefined): string => {
  if (!name) return "?";
  const trimmed = name.trim();
  return trimmed.length > 0 ? trimmed.charAt(0).toUpperCase() : "?";
};

export const RecurringList = ({ patterns, onSchedule }: Props) => {
  if (!patterns || patterns.length === 0) return null;

  return (
    <div className="rec-card">
      <div className="rec-card__title">Khoản định kỳ phát hiện được</div>
      <ul className="rec-list">
        {patterns.map((p, idx) => {
          const name = p.recipient_name ?? "Không rõ";
          const bank = p.recipient_bank ?? "";
          const prefill =
            `đặt lịch chuyển ${name} ${formatVND(p.typical_amount)} ` +
            `vào mùng ${p.typical_day} hàng tháng`;
          return (
            <li
              key={`${p.contact_id}-${p.description}-${idx}`}
              className="rec-list__item"
            >
              <div className="rec-list__avatar">{firstLetter(name)}</div>
              <div className="rec-list__body">
                <div className="rec-list__head">
                  <div className="rec-list__name">{name}</div>
                  <span
                    className={confidenceClass(p.confidence)}
                    title={confidenceLabel(p.confidence)}
                    aria-label={confidenceLabel(p.confidence)}
                  />
                </div>
                {bank && <div className="rec-list__bank">{bank}</div>}
                <div className="rec-list__cadence">
                  {`≈ ${formatVND(p.typical_amount)} mỗi tháng vào ~ngày ${p.typical_day}`}
                </div>
                {p.description && (
                  <div className="rec-list__desc">{p.description}</div>
                )}
                <button
                  type="button"
                  className="rec-list__action"
                  onClick={() => onSchedule(prefill)}
                >
                  Đặt lịch tự động
                </button>
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
};
