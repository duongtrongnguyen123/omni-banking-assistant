import type { BalanceResult } from "../types";
import { formatVND } from "../format";

/** Tiny inline sparkline of the last 7 days of outgoing spending. The
 *  oldest day sits on the left so the trend reads chronologically; an
 *  empty series renders nothing (no chrome / no empty box). */
const SpendingSparkline = ({ values }: { values: number[] }) => {
  const max = Math.max(...values, 1);
  if (max <= 0) return null;
  const w = 80;
  const h = 22;
  const step = w / Math.max(1, values.length - 1);
  const points = values
    .map((v, i) => {
      const x = i * step;
      const y = h - (v / max) * (h - 2) - 1;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg
      className="bal-card__spark"
      width={w}
      height={h}
      viewBox={`0 0 ${w} ${h}`}
      role="img"
      aria-label="Chi tiêu 7 ngày gần nhất"
    >
      <polyline
        fill="none"
        stroke="var(--orange, #f97316)"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
        points={points}
      />
    </svg>
  );
};

export const BalanceCard = ({ balance }: { balance: BalanceResult }) => (
  <div className="bal-card">
    <div className="bal-card__label">TỔNG SỐ DƯ</div>
    <div className="bal-card__total-row">
      <div className="bal-card__total">{formatVND(balance.total)}</div>
      {balance.recent_outflow_7d && balance.recent_outflow_7d.length > 0 && (
        <SpendingSparkline values={balance.recent_outflow_7d} />
      )}
    </div>
    <ul className="bal-card__accounts">
      {balance.accounts.map((a) => (
        <li key={a.id}>
          <div>
            <div className="bal-account__bank">
              {a.bank} {a.primary && <span className="bal-tag">Chính</span>}
            </div>
            <div className="bal-account__num">•••• {a.number.slice(-4)}</div>
          </div>
          <div className="bal-account__amount">{formatVND(a.balance)}</div>
        </li>
      ))}
    </ul>
  </div>
);
