import { useEffect, useState } from "react";
import { formatVND } from "../format";

interface TopRecipient {
  contact_id: string;
  display_name: string;
  bank?: string;
  total: number;
  count: number;
}

interface TaxYearResponse {
  year: number;
  total_outgoing: number;
  count: number;
  by_category: Record<string, number>;
  by_month: Record<string, number>;
  by_recipient_top10: TopRecipient[];
}

interface Props {
  year: number;
  onClose: () => void;
}

/**
 * Sidebar / modal widget that surfaces the /api/export/tax-year.json
 * rollup. Mounted from `ExportMenu` and auto-shown when month=12 or
 * `?taxview=1` is present in the URL.
 */
export const TaxYearCard = ({ year, onClose }: Props) => {
  const [data, setData] = useState<TaxYearResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetch(`/api/export/tax-year.json?year=${year}`, {
      headers: { "x-user-id": "u_an", "Accept-Language": "vi" },
    })
      .then(async (res) => {
        if (!res.ok) throw new Error(`${res.status}`);
        return res.json();
      })
      .then((j: TaxYearResponse) => {
        if (alive) setData(j);
      })
      .catch((e) => {
        if (alive) setErr(String(e));
      });
    return () => {
      alive = false;
    };
  }, [year]);

  return (
    <div
      className="taxyear-backdrop"
      role="dialog"
      aria-label="Tổng kết năm"
      onClick={onClose}
    >
      <div className="taxyear-card" onClick={(e) => e.stopPropagation()}>
        <div className="taxyear-card__head">
          <div>
            <div className="taxyear-card__kicker">Tổng kết</div>
            <div className="taxyear-card__title">Năm {year}</div>
          </div>
          <button className="taxyear-card__close" onClick={onClose} aria-label="Đóng">
            ×
          </button>
        </div>

        {err && <div className="taxyear-card__err">Không tải được: {err}</div>}
        {!data && !err && <div className="taxyear-card__loading">Đang tải…</div>}

        {data && (
          <>
            <div className="taxyear-card__big">
              <div className="taxyear-card__big-label">Tổng chi cả năm</div>
              <div className="taxyear-card__big-value">
                {formatVND(data.total_outgoing)}
              </div>
              <div className="taxyear-card__big-sub">
                {data.count} giao dịch
              </div>
            </div>

            <div className="taxyear-card__section">
              <div className="taxyear-card__section-title">Theo chủ đề</div>
              {Object.entries(data.by_category).length === 0 ? (
                <div className="taxyear-card__empty">Chưa có dữ liệu.</div>
              ) : (
                <ul className="taxyear-card__list">
                  {Object.entries(data.by_category).map(([cat, total]) => {
                    const pct = data.total_outgoing
                      ? Math.round((total / data.total_outgoing) * 100)
                      : 0;
                    return (
                      <li key={cat}>
                        <div className="taxyear-card__row">
                          <span>{cat}</span>
                          <span>{formatVND(total)}</span>
                        </div>
                        <div className="taxyear-card__bar">
                          <div
                            className="taxyear-card__bar-fill"
                            style={{ width: `${pct}%` }}
                          />
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>

            <div className="taxyear-card__section">
              <div className="taxyear-card__section-title">
                Top người nhận
              </div>
              {data.by_recipient_top10.length === 0 ? (
                <div className="taxyear-card__empty">Chưa có dữ liệu.</div>
              ) : (
                <ol className="taxyear-card__top">
                  {data.by_recipient_top10.slice(0, 10).map((r, idx) => (
                    <li key={r.contact_id}>
                      <span className="taxyear-card__rank">{idx + 1}</span>
                      <div className="taxyear-card__top-meta">
                        <strong>{r.display_name || r.contact_id}</strong>
                        <small>
                          {r.bank ? `${r.bank} · ` : ""}
                          {r.count} lần
                        </small>
                      </div>
                      <span className="taxyear-card__top-total">
                        {formatVND(r.total)}
                      </span>
                    </li>
                  ))}
                </ol>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
};
