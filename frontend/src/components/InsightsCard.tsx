import { useEffect, useState } from "react";
import { api } from "../api/client";
import { formatVND, formatDate } from "../format";
import type { InsightsSummary, MoMEntry } from "../types";

// Vietnamese labels for category codes the backend emits.
const CATEGORY_LABEL: Record<string, string> = {
  family: "Gia đình",
  friends: "Bạn bè",
  work: "Công việc",
  bills: "Hoá đơn",
  shopping: "Mua sắm",
  food: "Ăn uống",
  transfer: "Chuyển khoản",
  omni: "Chuyển khoản",
  other: "Khác",
};

const labelOf = (cat: string): string =>
  CATEGORY_LABEL[cat] ?? cat.charAt(0).toUpperCase() + cat.slice(1);

interface MoMRow {
  category: string;
  entry: MoMEntry;
  absDelta: number;
}

const topMoM = (mom: Record<string, MoMEntry>): MoMRow[] =>
  Object.entries(mom)
    .map(([category, entry]) => ({
      category,
      entry,
      absDelta: Math.abs(entry.this - entry.last),
    }))
    .sort((a, b) => b.absDelta - a.absDelta)
    .slice(0, 3);

const ratioLabel = (z: number): string => {
  if (!isFinite(z) || z >= 50) return "cao bất thường";
  // Heuristic: z ~3 ≈ "gấp 3x", show z rounded so users see a number.
  const n = Math.max(2, Math.round(z));
  return `cao gấp ~${n}x mức thường`;
};

export const InsightsCard = () => {
  const [data, setData] = useState<InsightsSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .insights()
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e) => {
        if (!cancelled) setError(String(e instanceof Error ? e.message : e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) {
    return (
      <div className="insights-card">
        <div className="insights-card__title">Gợi ý từ Omni</div>
        <div className="insights-card__empty">Không tải được gợi ý: {error}</div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="insights-card">
        <div className="insights-card__title">Gợi ý từ Omni</div>
        <div className="insights-card__empty">Đang phân tích chi tiêu…</div>
      </div>
    );
  }

  const momRows = topMoM(data.mom);
  const hasAny =
    momRows.length > 0 ||
    data.anomalies.length > 0 ||
    data.subscriptions.length > 0;

  return (
    <div className="insights-card">
      <div className="insights-card__title">Gợi ý từ Omni</div>
      <div className="insights-card__sub">
        Omni đã rà soát lịch sử chi tiêu của bạn — đây là vài điểm đáng để mắt.
      </div>

      {!hasAny && (
        <div className="insights-card__empty">
          Chưa có đủ dữ liệu để đưa ra gợi ý.
        </div>
      )}

      {momRows.length > 0 && (
        <section className="insights-section">
          <div className="insights-section__title">
            Tháng này so với tháng trước
          </div>
          <ul className="insights-list">
            {momRows.map(({ category, entry }) => {
              const up = entry.this >= entry.last;
              const sign = up ? "+" : "";
              return (
                <li key={category} className="insights-list__row">
                  <div className="insights-list__main">
                    <div className="insights-list__name">{labelOf(category)}</div>
                    <div className="insights-list__desc">
                      Tháng trước: {formatVND(entry.last)}
                    </div>
                  </div>
                  <div className="insights-list__right">
                    <div className="insights-list__amount">
                      {formatVND(entry.this)}
                    </div>
                    <div
                      className={
                        "insights-delta " +
                        (up ? "insights-delta--up" : "insights-delta--down")
                      }
                    >
                      {sign}
                      {entry.delta_pct}%
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      {data.anomalies.length > 0 && (
        <section className="insights-section">
          <div className="insights-section__title">Bất thường</div>
          <ul className="insights-list">
            {data.anomalies.slice(0, 5).map((a) => (
              <li key={a.tx_id} className="insights-list__row">
                <div className="insights-list__main">
                  <div className="insights-list__name">{a.contact_name}</div>
                  <div className="insights-list__desc">{ratioLabel(a.z_score)}</div>
                </div>
                <div className="insights-list__right">
                  <div className="insights-list__amount insights-list__amount--warn">
                    {formatVND(a.amount)}
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      {data.subscriptions.length > 0 && (
        <section className="insights-section">
          <div className="insights-section__title">Subscription bạn có</div>
          <ul className="insights-list">
            {data.subscriptions.slice(0, 5).map((s) => (
              <li key={s.contact_id + s.typical_amount} className="insights-list__row">
                <div className="insights-list__main">
                  <div className="insights-list__name">{s.contact}</div>
                  <div className="insights-list__desc">
                    Tự động trả ~{formatVND(s.typical_amount)}/tháng · {s.occurrences} lần
                  </div>
                </div>
                <div className="insights-list__right">
                  <div className="insights-list__date">
                    Gần nhất {formatDate(s.last_seen)}
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
};
