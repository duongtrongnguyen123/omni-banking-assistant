import type { Schedule } from "../types";
import { formatVND, formatDate } from "../format";

export const ScheduleCard = ({ schedule }: { schedule: Schedule }) => {
  const daysAhead = Math.max(
    0,
    Math.ceil((new Date(schedule.next_run).getTime() - Date.now()) / 86400000),
  );
  return (
    <div className="sched-card" data-testid="schedule-card">
      <div className="sched-card__title">✓ Lịch định kỳ</div>
      <div className="sched-card__amount">{formatVND(schedule.amount)}</div>
      {schedule.description && (
        <div className="sched-card__desc">{schedule.description}</div>
      )}
      <div className="sched-card__meta">
        Lần kế: <strong>{formatDate(schedule.next_run)}</strong>
        {daysAhead > 0 && <span> · còn {daysAhead} ngày</span>}
      </div>
      <div className="sched-card__status">Đang chạy</div>
    </div>
  );
};
