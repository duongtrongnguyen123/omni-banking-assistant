interface Scenario {
  label: string;
  text: string;
}

const SCENARIOS: Scenario[] = [
  { label: "KB1 · Chuyển thông thường", text: "Chuyển cho Minh 2 triệu tiền ăn tháng này" },
  { label: "KB2 · Ngữ cảnh cá nhân", text: "Gửi cho mẹ 5 triệu như tháng trước" },
  { label: "KB3 · Trùng tên", text: "Chuyển cho Minh 500k" },
  { label: "KB4 · Lịch sử", text: "Tháng này mình gửi mẹ bao nhiêu rồi?" },
  { label: "KB5 · Bất thường", text: "Chuyển 50 triệu cho Hùng STK 9990001234" },
  { label: "KB6 · Định kỳ", text: "Đặt lịch chuyển mẹ 2tr vào mùng 1 hàng tháng" },
  { label: "KB7 · Thêm danh bạ", text: "Lưu Lê Mai STK 0123987654 Vietcombank tên gọi tắt chị Mai" },
  { label: "KB8 · Theo chủ đề", text: "Tháng này tôi tiêu vào những chủ đề nào?" },
];

export const QuickScenarios = ({ onPick }: { onPick: (text: string) => void }) => (
  <div className="quick-scenarios">
    <div className="quick-scenarios__title">Kịch bản demo nhanh</div>
    <div className="quick-scenarios__list">
      {SCENARIOS.map((s) => {
        // Stable e2e hook — extract "KB1" / "KB2" / ... prefix so tests
        // can target a chip by scenario code regardless of label wording.
        const code = s.label.split("·")[0].trim();
        return (
          <button
            key={s.text}
            className="quick-chip"
            data-testid={`quick-chip-${code}`}
            onClick={() => onPick(s.text)}
          >
            {s.label}
          </button>
        );
      })}
    </div>
  </div>
);
