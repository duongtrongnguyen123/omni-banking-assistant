import { useEffect, useRef, useState } from "react";
import { TaxYearCard } from "./TaxYearCard";

const pad = (n: number) => n.toString().padStart(2, "0");

const todayParts = () => {
  const d = new Date();
  return {
    year: d.getFullYear(),
    month: d.getMonth() + 1,
    monthKey: `${d.getFullYear()}-${pad(d.getMonth() + 1)}`,
    firstDay: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-01`,
    today: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`,
  };
};

/**
 * Header dropdown that exposes the three /api/export endpoints to the user.
 *
 *  - "Tải CSV tháng này"  → downloads /api/export/transactions.csv
 *  - "Tải sao kê HTML"    → opens /api/export/sao-ke.html in a new tab
 *  - "Tổng kết năm"       → opens the TaxYearCard modal
 *
 * Auto-opens the tax modal when `?taxview=1` is in the URL or when the
 * current month is December, matching the prompt brief.
 */
export const ExportMenu = () => {
  const [open, setOpen] = useState(false);
  const [showTax, setShowTax] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const parts = todayParts();

  useEffect(() => {
    const url = new URL(window.location.href);
    if (url.searchParams.get("taxview") === "1" || parts.month === 12) {
      // delay so the chat mounts first; non-blocking
      const id = window.setTimeout(() => setShowTax(true), 400);
      return () => window.clearTimeout(id);
    }
  }, [parts.month]);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("mousedown", onClick);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onClick);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const downloadCsv = () => {
    const url =
      `/api/export/transactions.csv?from=${parts.firstDay}&to=${parts.today}`;
    // Anchor with download attribute — preserves the BOM and lets the
    // browser pick up the Content-Disposition filename from the response.
    const a = document.createElement("a");
    a.href = url;
    a.rel = "noopener";
    a.click();
    setOpen(false);
  };

  const openSaoKe = () => {
    const url = `/api/export/sao-ke.html?month=${parts.monthKey}`;
    window.open(url, "_blank", "noopener,noreferrer");
    setOpen(false);
  };

  const openTaxView = () => {
    setShowTax(true);
    setOpen(false);
  };

  return (
    <>
      <div className="export-menu" ref={wrapRef}>
        <button
          type="button"
          className="export-menu__trigger"
          onClick={() => setOpen((v) => !v)}
          aria-haspopup="menu"
          aria-expanded={open}
        >
          Tải sao kê
        </button>
        {open && (
          <div className="export-menu__panel" role="menu">
            <button
              type="button"
              role="menuitem"
              className="export-menu__item"
              onClick={downloadCsv}
            >
              Tải CSV tháng này
              <small>Mở được bằng Excel / Google Sheets</small>
            </button>
            <button
              type="button"
              role="menuitem"
              className="export-menu__item"
              onClick={openSaoKe}
            >
              Tải sao kê HTML
              <small>In sang PDF bằng Cmd/Ctrl + P</small>
            </button>
            <button
              type="button"
              role="menuitem"
              className="export-menu__item"
              onClick={openTaxView}
            >
              Tổng kết năm
              <small>Theo chủ đề · top người nhận</small>
            </button>
          </div>
        )}
      </div>
      {showTax && (
        <TaxYearCard year={parts.year} onClose={() => setShowTax(false)} />
      )}
    </>
  );
};
