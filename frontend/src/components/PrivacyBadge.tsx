import { useEffect, useState } from "react";

/**
 * Small header badge that surfaces the active privacy mode.
 *
 * Hidden when mode is "off" (default — current production behaviour).
 * When mode is "redact" or "local-only" we render a lock icon plus a
 * short label, and a tooltip that explains what gets redacted /
 * suppressed. Pure display — no controls live here; toggling happens
 * via the admin endpoint, which is out of scope for the demo UI.
 */

type Mode = "off" | "redact" | "local-only";

interface HealthResponse {
  privacy_mode?: Mode;
}

const LABELS: Record<Exclude<Mode, "off">, { label: string; tooltip: string }> = {
  redact: {
    label: "chế độ bảo mật cao",
    tooltip:
      "Số tài khoản, số tiền, số điện thoại, email và tên cá nhân được lọc trên máy trước khi gửi tới LLM.",
  },
  "local-only": {
    label: "chế độ riêng tư",
    tooltip:
      "Tắt hoàn toàn LLM bên thứ ba. Mọi câu hỏi chỉ chạy qua rule extractor cục bộ — không có dữ liệu rời máy chủ.",
  },
};

export function PrivacyBadge() {
  const [mode, setMode] = useState<Mode>("off");

  useEffect(() => {
    let active = true;
    const tick = async () => {
      try {
        const res = await fetch("/health");
        if (!res.ok) return;
        const j = (await res.json()) as HealthResponse;
        if (active && j.privacy_mode) setMode(j.privacy_mode);
      } catch {
        /* ignore — badge just won't show until the next poll succeeds */
      }
    };
    tick();
    const id = window.setInterval(tick, 15000);
    return () => {
      active = false;
      window.clearInterval(id);
    };
  }, []);

  if (mode === "off") return null;
  const info = LABELS[mode];

  return (
    <div
      className={`privacy-badge privacy-badge--${mode}`}
      role="status"
      aria-label={`Chế độ bảo mật: ${info.label}`}
      title={info.tooltip}
    >
      <span className="privacy-badge__lock" aria-hidden="true">
        {/* Pure SVG lock — no emoji baseline gymnastics */}
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
          <rect x="4" y="11" width="16" height="10" rx="2" />
          <path d="M8 11V8a4 4 0 0 1 8 0v3" />
        </svg>
      </span>
      <span className="privacy-badge__label">{info.label}</span>
    </div>
  );
}
