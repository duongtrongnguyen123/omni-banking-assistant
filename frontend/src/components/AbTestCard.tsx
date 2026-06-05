/**
 * A/B test dashboard card — bottom-right floating widget for the demo.
 *
 * Activated by ``?abtest=1`` on the URL. Polls
 * ``/api/admin/abtest/report`` every 5 seconds and renders the per-arm
 * trial/hit/hit_rate table. Includes a small "reset" button that posts
 * to ``/api/admin/abtest/reset`` — handy when judges want to watch the
 * bandit converge from scratch.
 *
 * Lightweight by design: the production path pays zero DOM cost when
 * ``?abtest=1`` isn't set.
 */
import { useCallback, useEffect, useState } from "react";

const REFRESH_MS = 5000;

interface ArmRow {
  trials: number;
  hits: number;
  hit_rate: number;
  ci: [number, number];
  weights: [number, number, number];
}

interface ReportPayload {
  enabled: boolean;
  min_trials_per_arm: number;
  bandit_active: boolean;
  arms: Record<string, ArmRow>;
}

const isEnabled = (): boolean => {
  if (typeof window === "undefined") return false;
  try {
    return new URLSearchParams(window.location.search).get("abtest") === "1";
  } catch {
    return false;
  }
};

async function fetchReport(): Promise<ReportPayload | null> {
  try {
    const res = await fetch("/api/admin/abtest/report");
    if (!res.ok) return null;
    return (await res.json()) as ReportPayload;
  } catch {
    return null;
  }
}

async function resetReport(): Promise<void> {
  try {
    await fetch("/api/admin/abtest/reset", { method: "POST" });
  } catch {
    // ignore — best-effort
  }
}

export function AbTestCard() {
  const [visible] = useState<boolean>(isEnabled());
  const [data, setData] = useState<ReportPayload | null>(null);
  const [busy, setBusy] = useState<boolean>(false);

  const refresh = useCallback(async () => {
    const r = await fetchReport();
    if (r) setData(r);
  }, []);

  useEffect(() => {
    if (!visible) return;
    void refresh();
    const id = window.setInterval(() => {
      void refresh();
    }, REFRESH_MS);
    return () => window.clearInterval(id);
  }, [visible, refresh]);

  if (!visible) return null;

  const handleReset = async () => {
    setBusy(true);
    await resetReport();
    await refresh();
    setBusy(false);
  };

  const rows = data
    ? Object.entries(data.arms).sort((a, b) => b[1].hit_rate - a[1].hit_rate)
    : [];
  const totalTrials = rows.reduce((acc, [, a]) => acc + a.trials, 0);

  return (
    <div
      style={{
        position: "fixed",
        right: 12,
        bottom: 12,
        width: 360,
        background: "rgba(15, 23, 42, 0.92)",
        color: "#e2e8f0",
        border: "1px solid rgba(148, 163, 184, 0.3)",
        borderRadius: 8,
        padding: "10px 12px",
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
        fontSize: 11,
        lineHeight: 1.4,
        zIndex: 9998,
        boxShadow: "0 8px 24px rgba(0,0,0,0.35)",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 6,
          borderBottom: "1px solid rgba(148, 163, 184, 0.2)",
          paddingBottom: 4,
        }}
      >
        <strong style={{ fontSize: 12, letterSpacing: 0.4 }}>
          A/B suggester
        </strong>
        <span style={{ opacity: 0.7 }}>
          {data
            ? `${totalTrials} trials · ${
                data.bandit_active ? "bandit on" : "hash routing"
              }`
            : "loading…"}
        </span>
      </div>

      {data && !data.enabled && (
        <div style={{ color: "#fca5a5" }}>
          Disabled (OMNI_DISABLE_ABTEST=1)
        </div>
      )}

      {rows.length === 0 && data && data.enabled && (
        <div style={{ opacity: 0.8 }}>No arms registered.</div>
      )}

      {rows.length > 0 && (
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ textAlign: "left", opacity: 0.7 }}>
              <th style={{ padding: "2px 4px" }}>arm</th>
              <th style={{ padding: "2px 4px" }}>n</th>
              <th style={{ padding: "2px 4px" }}>hit</th>
              <th style={{ padding: "2px 4px" }}>rate</th>
              <th style={{ padding: "2px 4px" }}>95% CI</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([name, a], idx) => (
              <tr
                key={name}
                style={{
                  background: idx === 0 ? "rgba(34, 197, 94, 0.15)" : "transparent",
                }}
              >
                <td style={{ padding: "2px 4px" }} title={`weights: ${a.weights.join(", ")}`}>
                  {idx === 0 ? "★ " : "  "}
                  {name}
                </td>
                <td style={{ padding: "2px 4px" }}>{a.trials}</td>
                <td style={{ padding: "2px 4px" }}>{a.hits}</td>
                <td style={{ padding: "2px 4px" }}>
                  {(a.hit_rate * 100).toFixed(1)}%
                </td>
                <td style={{ padding: "2px 4px", opacity: 0.85 }}>
                  {(a.ci[0] * 100).toFixed(0)}–{(a.ci[1] * 100).toFixed(0)}%
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginTop: 6,
          paddingTop: 4,
          borderTop: "1px solid rgba(148, 163, 184, 0.2)",
        }}
      >
        <span style={{ opacity: 0.6 }}>
          {data
            ? `Thompson at ≥${data.min_trials_per_arm} trials/arm`
            : ""}
        </span>
        <button
          onClick={handleReset}
          disabled={busy}
          style={{
            background: "transparent",
            border: "1px solid rgba(148, 163, 184, 0.4)",
            color: "#e2e8f0",
            padding: "2px 8px",
            borderRadius: 4,
            cursor: busy ? "wait" : "pointer",
            fontSize: 10,
          }}
        >
          {busy ? "…" : "reset"}
        </button>
      </div>
    </div>
  );
}
