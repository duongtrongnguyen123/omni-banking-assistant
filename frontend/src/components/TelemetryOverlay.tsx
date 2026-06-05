/**
 * Telemetry overlay — floating bottom-left HUD for the live pitch.
 *
 * Visible only when:
 *   1. The page URL contains `?dev=1`, OR
 *   2. The user presses Cmd/Ctrl + Shift + D to toggle it at runtime.
 *
 * Pulls data from three places:
 *   - `omni:telemetry` window event: latest NLU/safety numbers,
 *     dispatched from `App.tsx` every time `/api/chat` returns with
 *     a `telemetry` payload populated.
 *   - `omni:ws-status` window event: WS connection up/down (dispatched
 *     by `useEventStream`).
 *   - `/health` (one-shot at mount): backend version + git SHA.
 *
 * The component renders nothing if telemetry is off, so the prod path
 * pays zero DOM cost.
 */
import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { TelemetryPayload } from "../types";

export const TELEMETRY_EVENT = "omni:telemetry";
export const WS_STATUS_EVENT = "omni:ws-status";
export const TOAST_QUEUE_EVENT = "omni:toast-queue";

export interface TelemetryEventDetail {
  telemetry: TelemetryPayload;
  // Optional per-call extras the App can stash (provider name when LLM
  // succeeded, etc.). Not yet populated by the orchestrator.
  llm_provider?: string | null;
  suggester_ms?: number | null;
}

interface WsStatusDetail {
  channel: "events" | "chat";
  status: "connected" | "disconnected";
}

interface ToastQueueDetail {
  depth: number;
}

interface HealthState {
  version: string;
  git_sha: string;
  offline_demo: boolean;
}

const isDevQueryParam = (): boolean => {
  if (typeof window === "undefined") return false;
  try {
    return new URLSearchParams(window.location.search).get("dev") === "1";
  } catch {
    return false;
  }
};

export function TelemetryOverlay() {
  const [visible, setVisible] = useState<boolean>(isDevQueryParam());
  const [tel, setTel] = useState<TelemetryPayload | null>(null);
  const [wsStatus, setWsStatus] = useState<"connected" | "disconnected">("disconnected");
  const [toastDepth, setToastDepth] = useState<number>(0);
  const [suggesterMs, setSuggesterMs] = useState<number | null>(null);
  const [llmProvider, setLlmProvider] = useState<string | null>(null);
  const [health, setHealth] = useState<HealthState | null>(null);

  // Cmd/Ctrl+Shift+D toggles the overlay independently of the URL flag.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.shiftKey && (e.key === "D" || e.key === "d")) {
        e.preventDefault();
        setVisible((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Telemetry stream from /api/chat responses.
  useEffect(() => {
    const handler = (ev: Event) => {
      const detail = (ev as CustomEvent<TelemetryEventDetail>).detail;
      if (!detail) return;
      setTel(detail.telemetry);
      if (detail.suggester_ms != null) setSuggesterMs(detail.suggester_ms);
      if (detail.llm_provider !== undefined) setLlmProvider(detail.llm_provider ?? null);
    };
    window.addEventListener(TELEMETRY_EVENT, handler as EventListener);
    return () =>
      window.removeEventListener(TELEMETRY_EVENT, handler as EventListener);
  }, []);

  // WS connection status from useEventStream.
  useEffect(() => {
    const handler = (ev: Event) => {
      const detail = (ev as CustomEvent<WsStatusDetail>).detail;
      if (!detail) return;
      // We only track the events channel — that's the one judges care
      // about (it powers toasts during the demo).
      if (detail.channel === "events") setWsStatus(detail.status);
    };
    window.addEventListener(WS_STATUS_EVENT, handler as EventListener);
    return () =>
      window.removeEventListener(WS_STATUS_EVENT, handler as EventListener);
  }, []);

  // Toast queue depth — dispatched by ToastStack on each render.
  useEffect(() => {
    const handler = (ev: Event) => {
      const detail = (ev as CustomEvent<ToastQueueDetail>).detail;
      if (detail) setToastDepth(detail.depth);
    };
    window.addEventListener(TOAST_QUEUE_EVENT, handler as EventListener);
    return () =>
      window.removeEventListener(TOAST_QUEUE_EVENT, handler as EventListener);
  }, []);

  // /health one-shot at mount — gives us backend version + git SHA.
  useEffect(() => {
    if (!visible) return;
    let cancelled = false;
    api
      .health()
      .then((h) => {
        if (cancelled) return;
        setHealth({
          version: h.version,
          git_sha: h.git_sha,
          offline_demo: h.offline_demo,
        });
      })
      .catch(() => {
        /* health endpoint unreachable — leave dashes in the overlay */
      });
    return () => {
      cancelled = true;
    };
  }, [visible]);

  if (!visible) return null;

  const latencyClass = (() => {
    const ms = tel?.total_latency_ms ?? 0;
    if (!tel) return "telem__metric--idle";
    if (ms < 250) return "telem__metric--good";
    if (ms < 800) return "telem__metric--warn";
    return "telem__metric--bad";
  })();

  return (
    <div className="telem" role="complementary" aria-label="Telemetry overlay">
      <div className="telem__header">
        <span>OMNI · DEV</span>
        <button
          type="button"
          className="telem__close"
          onClick={() => setVisible(false)}
          aria-label="Đóng telemetry"
          title="Đóng (Cmd+Shift+D để bật lại)"
        >
          ×
        </button>
      </div>
      <div className="telem__rows">
        <div className={`telem__row ${latencyClass}`}>
          <span className="telem__k">NLU</span>
          <span className="telem__v">
            {tel?.nlu_latency_ms ?? "—"} ms
            <span className="telem__src">
              {tel?.nlu_source ? ` · ${tel.nlu_source}` : ""}
              {tel?.nlu_source === "llm" && llmProvider ? ` (${llmProvider})` : ""}
            </span>
          </span>
        </div>
        <div className="telem__row">
          <span className="telem__k">Total</span>
          <span className="telem__v">{tel?.total_latency_ms ?? "—"} ms</span>
        </div>
        <div className="telem__row">
          <span className="telem__k">Safety</span>
          <span className="telem__v">
            {tel?.safety_flags ?? 0} flag
            {(tel?.safety_codes ?? []).length > 0 &&
              ` · ${tel!.safety_codes!.join(", ")}`}
          </span>
        </div>
        <div className="telem__row">
          <span className="telem__k">Suggester</span>
          <span className="telem__v">
            {suggesterMs != null ? `${suggesterMs} ms` : "—"}
          </span>
        </div>
        <div className="telem__row">
          <span className="telem__k">Toasts</span>
          <span className="telem__v">{toastDepth}</span>
        </div>
        <div
          className={`telem__row telem__row--ws ${
            wsStatus === "connected" ? "telem__row--ok" : "telem__row--bad"
          }`}
        >
          <span className="telem__k">WS</span>
          <span className="telem__v">{wsStatus}</span>
        </div>
        <div className="telem__row telem__row--meta">
          <span className="telem__k">Build</span>
          <span className="telem__v">
            {health
              ? `${health.version} · ${health.git_sha}${
                  health.offline_demo ? " · OFFLINE" : ""
                }`
              : "—"}
          </span>
        </div>
      </div>
    </div>
  );
}
