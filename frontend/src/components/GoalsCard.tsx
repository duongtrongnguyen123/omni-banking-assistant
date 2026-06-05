import { useEffect, useState } from "react";
import { api } from "../api/client";
import { formatVND } from "../format";
import type { SavingsGoal } from "../types";

/** Sidebar widget showing each savings goal with a circular progress
 * indicator. Pure SVG, no chart libraries.
 *
 * Layout: one row per goal — circle on the left, name + amount + date
 * on the right. Names with deadlines render a small "deadline" tag.
 */

function CircularProgress({
  ratio,
  size = 48,
}: {
  ratio: number;
  size?: number;
}) {
  const r = (size - 6) / 2;
  const c = 2 * Math.PI * r;
  const clamped = Math.min(1, Math.max(0, ratio));
  const dash = c * clamped;
  const colour =
    clamped >= 1.0 ? "#2e7d32" : clamped >= 0.5 ? "#0066ff" : "#7e89a0";
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        stroke="#e3e7ef"
        strokeWidth="4"
        fill="none"
      />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        stroke={colour}
        strokeWidth="4"
        fill="none"
        strokeDasharray={`${dash} ${c}`}
        strokeLinecap="round"
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
      />
      <text
        x="50%"
        y="50%"
        dominantBaseline="middle"
        textAnchor="middle"
        fontSize="11"
        fontWeight="600"
        fill="#1f2734"
      >
        {Math.round(clamped * 100)}%
      </text>
    </svg>
  );
}

interface Props {
  refreshKey?: number;
}

export const GoalsCard = ({ refreshKey = 0 }: Props) => {
  const [rows, setRows] = useState<SavingsGoal[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .goals()
      .then((d) => {
        if (!cancelled) setRows(d);
      })
      .catch((e) => {
        if (!cancelled) setError(String(e instanceof Error ? e.message : e));
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  if (error) {
    return (
      <div className="goals-card">
        <div className="goals-card__title">Mục tiêu tiết kiệm</div>
        <div className="goals-card__empty">Không tải được mục tiêu: {error}</div>
      </div>
    );
  }

  if (rows === null) {
    return (
      <div className="goals-card">
        <div className="goals-card__title">Mục tiêu tiết kiệm</div>
        <div className="goals-card__empty">Đang tải…</div>
      </div>
    );
  }

  if (rows.length === 0) {
    return (
      <div className="goals-card">
        <div className="goals-card__title">Mục tiêu tiết kiệm</div>
        <div className="goals-card__empty">
          Chưa có mục tiêu nào. Nhắn cho Omni: "mục tiêu tiết kiệm Tết 50 triệu".
        </div>
      </div>
    );
  }

  return (
    <div className="goals-card">
      <div className="goals-card__title">Mục tiêu tiết kiệm</div>
      <ul className="goals-card__list">
        {rows.map((g) => {
          const ratio = g.target_vnd > 0 ? g.current_vnd / g.target_vnd : 0;
          return (
            <li key={g.id} className="goals-card__row">
              <CircularProgress ratio={ratio} />
              <div className="goals-card__body">
                <div className="goals-card__name">{g.name}</div>
                <div className="goals-card__amounts">
                  {formatVND(g.current_vnd)} / {formatVND(g.target_vnd)}
                </div>
                {g.deadline && (
                  <div className="goals-card__deadline">Hạn: {g.deadline}</div>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
};
