import { useState } from "react";
import type { BalanceResult } from "../types";
import { formatVND } from "../format";

const maskedAmount = "••••••••đ";

const EyeIcon = ({ hidden }: { hidden: boolean }) => (
  <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
    {hidden ? (
      <>
        <path d="M3 3l18 18" fill="none" stroke="currentColor" strokeLinecap="round" strokeWidth="2" />
        <path d="M10.6 10.6a2 2 0 0 0 2.8 2.8" fill="none" stroke="currentColor" strokeLinecap="round" strokeWidth="2" />
        <path
          d="M9.2 5.4A9.7 9.7 0 0 1 12 5c5 0 8.4 4.4 9.5 6.3a1.4 1.4 0 0 1 0 1.4 15 15 0 0 1-2.1 2.7M6.3 6.7a15 15 0 0 0-3.8 4.6 1.4 1.4 0 0 0 0 1.4C3.6 14.6 7 19 12 19c1.2 0 2.3-.2 3.3-.6"
          fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2"
        />
      </>
    ) : (
      <>
        <path
          d="M2.5 11.3C3.6 9.4 7 5 12 5s8.4 4.4 9.5 6.3a1.4 1.4 0 0 1 0 1.4C20.4 14.6 17 19 12 19s-8.4-4.4-9.5-6.3a1.4 1.4 0 0 1 0-1.4Z"
          fill="none" stroke="currentColor" strokeLinejoin="round" strokeWidth="2"
        />
        <circle cx="12" cy="12" r="3" fill="none" stroke="currentColor" strokeWidth="2" />
      </>
    )}
  </svg>
);

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

export const BalanceCard = ({ balance }: { balance: BalanceResult }) => {
  // Privacy default: hide on first render (good banking UX). Tap eye → reveal.
  const [hidden, setHidden] = useState(true);
  const amountText = (value: number) => (hidden ? maskedAmount : formatVND(value));

  return (
    <div className="bal-card">
      <div className="bal-card__top">
        <div className="bal-card__label">TỔNG SỐ DƯ</div>
        <button
          className="bal-card__toggle"
          type="button"
          onClick={() => setHidden((v) => !v)}
          aria-label={hidden ? "Hiện số dư" : "Ẩn số dư"}
          title={hidden ? "Hiện số dư" : "Ẩn số dư"}
        >
          <EyeIcon hidden={hidden} />
        </button>
      </div>
      <div className="bal-card__total-row">
        <div className="bal-card__total">{amountText(balance.total)}</div>
        {/* Sparkline only meaningful when amounts are visible — hide it
            in privacy mode so the silhouette doesn't leak spending shape. */}
        {!hidden && balance.recent_outflow_7d && balance.recent_outflow_7d.length > 0 && (
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
            <div className="bal-account__amount">{amountText(a.balance)}</div>
          </li>
        ))}
      </ul>
    </div>
  );
};
