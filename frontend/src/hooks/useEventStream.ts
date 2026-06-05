/**
 * Subscribes to the backend's `/ws/events` push channel and surfaces
 * each incoming notification through `onEvent`. Fails open: if the
 * WebSocket can't connect (server down, CORS, blocked by a proxy)
 * the chat continues working, toasts just don't appear.
 *
 * Reconnect strategy: simple capped exponential back-off (1s → 30s).
 * No queue/buffering on the client — the server replays its backlog
 * for us when we reconnect.
 */
import { useEffect, useRef } from "react";

export type ToastKind =
  | "transfer_success"
  | "transfer_failed"
  | "schedule_fired"
  | "recurring_detected"
  | "balance_low"
  | "anomaly_warning";

export type ToastSeverity = "success" | "info" | "warn" | "error";

export interface ToastEvent {
  kind: ToastKind;
  title: string;
  body: string;
  severity: ToastSeverity;
  ts: number;
  actionable_text?: string | null;
}

const VALID_KINDS: Record<ToastKind, true> = {
  transfer_success: true,
  transfer_failed: true,
  schedule_fired: true,
  recurring_detected: true,
  balance_low: true,
  anomaly_warning: true,
};

const VALID_SEVERITIES: Record<ToastSeverity, true> = {
  success: true,
  info: true,
  warn: true,
  error: true,
};

const isToastEvent = (raw: unknown): raw is ToastEvent => {
  if (!raw || typeof raw !== "object") return false;
  const r = raw as Record<string, unknown>;
  return (
    typeof r.kind === "string" &&
    (r.kind as string) in VALID_KINDS &&
    typeof r.severity === "string" &&
    (r.severity as string) in VALID_SEVERITIES &&
    typeof r.title === "string"
  );
};

/**
 * Window event name used as an in-process pub/sub so the WS hook and
 * the renderer (`<ToastStack />`) can live in different components
 * without prop-drilling. Keeps `App.tsx` to a single hook call.
 */
export const TOAST_EVENT_NAME = "omni:toast";

export function useEventStream(
  userId: string,
  onEvent?: (e: ToastEvent) => void,
) {
  // Default callback re-dispatches as a window CustomEvent so
  // `<ToastStack />` (or any other listener) can pick it up without
  // needing the same hook instance.
  const defaultCb = (e: ToastEvent) => {
    window.dispatchEvent(new CustomEvent<ToastEvent>(TOAST_EVENT_NAME, { detail: e }));
  };
  // Hold the callback in a ref so we don't re-open the socket every
  // time the parent re-renders with a new closure.
  const cbRef = useRef(onEvent ?? defaultCb);
  cbRef.current = onEvent ?? defaultCb;

  useEffect(() => {
    if (typeof window === "undefined") return;

    let ws: WebSocket | null = null;
    let retryDelayMs = 1000;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;

    const connect = () => {
      if (cancelled) return;
      const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
      // Use the same origin so the Vite proxy (or the deployed nginx)
      // forwards `/ws/events` to the FastAPI backend.
      const url = `${proto}//${window.location.host}/ws/events`;
      try {
        ws = new WebSocket(url);
      } catch {
        // Browser rejected the URL outright — bail to retry loop.
        scheduleRetry();
        return;
      }

      ws.onopen = () => {
        // Reset the backoff on a successful connection.
        retryDelayMs = 1000;
      };

      ws.onmessage = (msg) => {
        try {
          const data = JSON.parse(msg.data);
          if (isToastEvent(data)) cbRef.current(data);
        } catch {
          // Malformed payload — ignore. The chat path is unaffected.
        }
      };

      ws.onclose = () => {
        ws = null;
        scheduleRetry();
      };

      ws.onerror = () => {
        // onclose fires right after — let it handle the retry.
        try {
          ws?.close();
        } catch {
          /* ignore */
        }
      };
    };

    const scheduleRetry = () => {
      if (cancelled) return;
      retryTimer = setTimeout(connect, retryDelayMs);
      retryDelayMs = Math.min(retryDelayMs * 2, 30_000);
    };

    connect();

    return () => {
      cancelled = true;
      if (retryTimer) clearTimeout(retryTimer);
      try {
        ws?.close();
      } catch {
        /* ignore */
      }
    };
    // userId is part of the dep array so that switching demo users
    // (future feature) re-opens the socket. The callback is stable
    // via cbRef.
  }, [userId]);
}
