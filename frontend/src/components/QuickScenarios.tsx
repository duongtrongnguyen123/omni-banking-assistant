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
  // KB9 ("Lặp lại lần trước") is now a permanent floating CTA above the
  // input bar — see RepeatLastCTA.
];

export const QuickScenarios = ({ onPick }: { onPick: (text: string) => void }) => (
  <div className="quick-scenarios">
    <div className="quick-scenarios__title">Kịch bản demo nhanh</div>
    <div className="quick-scenarios__list">
      {SCENARIOS.map((s) => (
        <button key={s.text} className="quick-chip" onClick={() => onPick(s.text)}>
          {s.label}
        </button>
      ))}
    </div>
  </div>
);
