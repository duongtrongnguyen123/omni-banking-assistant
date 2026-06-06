/**
 * Subscribes to the backend's `/ws/events` push channel and surfaces
 * each incoming notification through `onEvent`. Fails open: if the
 * WebSocket can't connect (server down, CORS, blocked by a proxy)
 * the chat continues working, toasts just don't appear.
 *
 * Reconnect strategy: capped exponential back-off (1s → 30s) with
 * ±15% jitter so N tabs reconnecting after a redeploy don't all fire
 * in the same tick. After ~1h at the cap the hook pauses reconnects
 * for hidden tabs until they're foregrounded again.
 *
 * Liveness: a `{type:"ping"}` is sent every 25s to keep NAT mappings
 * warm, and any inbound frame refreshes `lastMessageAt`. If 60s pass
 * with no inbound frame the socket is force-closed to trigger the
 * reconnect path — without this a silent mid-flow NAT drop leaves the
 * browser holding a dead WebSocket and `onclose` never fires. The
 * backend's `_drain_client` task discards client messages, so unknown
 * `{type:"ping"}` payloads are harmless. The server keeps a 64-entry
 * backlog so reconnects replay missed events.
 */
import { useEffect, useRef } from "react";

export type ToastKind =
  | "transfer_success"
  | "transfer_failed"
  | "schedule_fired"
  | "recurring_detected"
  | "recurring_suggest"
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
  recurring_suggest: true,
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
// Fired with `{channel: "events", status: "connected"|"disconnected"}` so
// the telemetry overlay can render a live WS-status pill.
export const WS_STATUS_EVENT_NAME = "omni:ws-status";

const dispatchWsStatus = (status: "connected" | "disconnected") => {
  if (typeof window === "undefined") return;
  window.dispatchEvent(
    new CustomEvent(WS_STATUS_EVENT_NAME, {
      detail: { channel: "events", status },
    }),
  );
};

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
    let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
    let lastMessageAt = Date.now();
    let totalRetries = 0;
    let visibilityListener: (() => void) | null = null;
    let cancelled = false;

    // Heartbeat tuning. NAT boxes (mobile carriers, corporate proxies)
    // typically time out idle TCP flows around 60-120s; pinging every
    // 25s keeps the connection warm AND gives us a reliable cadence to
    // gate liveness on. Without this a silent NAT drop leaves the
    // browser holding a dead WebSocket forever — onclose never fires.
    const PING_INTERVAL_MS = 25_000;
    const LIVENESS_TIMEOUT_MS = 60_000;
    // After ~1h of failed attempts at the 30s cap, stop hammering the
    // server until the tab is foregrounded again. A backend redeploy
    // that boots N hidden tabs into the cap would otherwise produce a
    // thundering herd; deferring hidden tabs spreads the load.
    const MAX_RETRIES_BEFORE_PAUSE = 120;

    const clearHeartbeat = () => {
      if (heartbeatTimer !== null) {
        clearInterval(heartbeatTimer);
        heartbeatTimer = null;
      }
    };

    const startHeartbeat = () => {
      clearHeartbeat();
      lastMessageAt = Date.now();
      heartbeatTimer = setInterval(() => {
        // Liveness check: if we haven't heard anything from the server
        // in 60s, the TCP is probably half-open from a silent NAT drop.
        // Force-close to trigger the reconnect loop; the browser would
        // otherwise hold the dead socket indefinitely with no onclose.
        if (Date.now() - lastMessageAt > LIVENESS_TIMEOUT_MS) {
          try {
            ws?.close();
          } catch {
            /* ignore — onclose will scheduleRetry */
          }
          return;
        }
        if (ws && ws.readyState === WebSocket.OPEN) {
          try {
            ws.send(JSON.stringify({ type: "ping" }));
          } catch {
            // send failure is itself a hint the socket is dead.
            try {
              ws.close();
            } catch {
              /* ignore */
            }
          }
        }
      }, PING_INTERVAL_MS);
    };

    const removeVisibilityListener = () => {
      if (visibilityListener && typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", visibilityListener);
        visibilityListener = null;
      }
    };

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
        totalRetries = 0;
        dispatchWsStatus("connected");
        startHeartbeat();
      };

      ws.onmessage = (msg) => {
        // Track liveness on *every* inbound frame — server pushes count
        // just as much as pong replies. The backend currently has no
        // explicit pong, but normal event traffic suffices to keep
        // lastMessageAt fresh.
        lastMessageAt = Date.now();
        try {
          const data = JSON.parse(msg.data);
          if (isToastEvent(data)) cbRef.current(data);
        } catch {
          // Malformed payload — ignore. The chat path is unaffected.
        }
      };

      ws.onclose = () => {
        ws = null;
        clearHeartbeat();
        dispatchWsStatus("disconnected");
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
      // Clear any pending retry first — if `connect()` failed and called
      // `scheduleRetry()` synchronously, or if `onclose` and `onerror`
      // both fire a retry in the same tick, overwriting the handle
      // without clearing leaks the previous timer (Bug B). Under fast
      // disconnect/reconnect cycles this grows unbounded.
      if (retryTimer !== null) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
      totalRetries += 1;
      // After ~1h of failures, pause reconnecting until the tab is
      // visible again. Prevents hidden tabs from contributing to a
      // thundering-herd reconnect storm after a long backend outage.
      if (
        totalRetries > MAX_RETRIES_BEFORE_PAUSE &&
        typeof document !== "undefined" &&
        document.visibilityState !== "visible"
      ) {
        removeVisibilityListener();
        visibilityListener = () => {
          if (document.visibilityState === "visible") {
            removeVisibilityListener();
            totalRetries = 0;
            retryDelayMs = 1000;
            connect();
          }
        };
        document.addEventListener("visibilitychange", visibilityListener);
        return;
      }
      // Add ±15% jitter so N tabs reconnecting after a redeploy don't
      // all fire in the same tick. Without jitter the 30s cap acts as
      // a synchronisation point that hammers the server.
      const jitter = 0.85 + Math.random() * 0.3;
      const delayWithJitter = Math.round(retryDelayMs * jitter);
      retryTimer = setTimeout(() => {
        retryTimer = null;
        connect();
      }, delayWithJitter);
      retryDelayMs = Math.min(retryDelayMs * 2, 30_000);
    };

    connect();

    return () => {
      cancelled = true;
      if (retryTimer !== null) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
      clearHeartbeat();
      removeVisibilityListener();
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
