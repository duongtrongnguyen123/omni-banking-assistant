import { useState } from "react";

/**
 * Skill discovery sidebar widget — "Omni có thể làm gì?".
 *
 * Renders 5 tabs, each with a small set of example chips. Clicking a
 * chip pre-fills the chat input (via the ``onPick`` callback) instead
 * of submitting — that way the user can edit before sending, which
 * matches the existing QuickScenarios contract.
 *
 * The content here is intentionally a static mirror of the backend's
 * ``help_sections_payload`` so the sidebar works even when the user
 * hasn't typed /help yet. The /help command renders the same data
 * via ``<HelpCard />`` once the orchestrator returns it.
 */

interface SkillItem {
  label: string;
  example: string;
}

interface SkillSection {
  id: string;
  title: string;
  items: SkillItem[];
}

const SECTIONS: SkillSection[] = [
  {
    id: "transfer",
    title: "Chuyển tiền",
    items: [
      { label: "Chuyển nhanh", example: "chuyển mẹ 2tr" },
      { label: "Có nội dung", example: "gửi tiền ăn cho An 500k" },
      { label: "Slash", example: "/transfer Nam 1tr" },
    ],
  },
  {
    id: "query",
    title: "Truy vấn",
    items: [
      { label: "Tháng trước", example: "tháng trước tiêu bao nhiêu" },
      { label: "Top người nhận", example: "ai nhận nhiều nhất" },
      { label: "Số dư", example: "/balance" },
    ],
  },
  {
    id: "recurring",
    title: "Định kỳ",
    items: [
      { label: "Đặt lịch", example: "đặt lịch chuyển mẹ 2tr mùng 1" },
      { label: "Khoản đều", example: "có khoản nào trả đều" },
    ],
  },
  {
    id: "budget",
    title: "Ngân sách",
    items: [
      { label: "Đặt ngân sách", example: "đặt ngân sách ăn uống 3tr" },
      { label: "Còn lại", example: "tháng này còn bao nhiêu cho ăn uống" },
    ],
  },
  {
    id: "tools",
    title: "Công cụ",
    items: [
      { label: "Trợ giúp", example: "/help" },
      { label: "ATM gần nhất", example: "ATM gần nhất" },
      { label: "Lưu danh bạ", example: "Lưu Nam STK 0123 MB Bank" },
    ],
  },
];

export const SKILLS_SECTION_COUNT = SECTIONS.length;
export const SKILLS_CHIP_COUNTS: Record<string, number> = SECTIONS.reduce(
  (acc, s) => ({ ...acc, [s.id]: s.items.length }),
  {} as Record<string, number>,
);

interface Props {
  // Pre-fills the chat input — does NOT submit. Matches the
  // QuickScenarios onPick name for clarity but uses prefill semantics.
  onPrefill: (text: string) => void;
}

export const SkillsCard = ({ onPrefill }: Props) => {
  const [activeId, setActiveId] = useState<string>(SECTIONS[0].id);
  const active = SECTIONS.find((s) => s.id === activeId) ?? SECTIONS[0];

  return (
    <div className="skills-card" aria-label="Omni có thể làm gì?">
      <div className="skills-card__title">Omni có thể làm gì?</div>
      <div className="skills-card__tabs" role="tablist">
        {SECTIONS.map((s) => (
          <button
            key={s.id}
            type="button"
            role="tab"
            aria-selected={s.id === activeId}
            className={`skills-card__tab ${s.id === activeId ? "is-active" : ""}`}
            onClick={() => setActiveId(s.id)}
          >
            {s.title}
          </button>
        ))}
      </div>
      <div
        className="skills-card__chips"
        role="tabpanel"
        aria-label={active.title}
      >
        {active.items.map((item) => (
          <button
            key={item.example}
            type="button"
            className="skills-card__chip"
            title={item.example}
            onClick={() => onPrefill(item.example)}
          >
            <span className="skills-card__chip-label">{item.label}</span>
            <span className="skills-card__chip-example">{item.example}</span>
          </button>
        ))}
      </div>
    </div>
  );
};
