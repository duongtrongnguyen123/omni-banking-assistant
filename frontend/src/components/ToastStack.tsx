/**
 * Push-notification toast stack.
 *
 * Listens on the `omni:toast` window event (fed by `useEventStream`)
 * and renders up to 3 toasts simultaneously near the top of the
 * phone frame. Each toast auto-dismisses after 4s. Clicking one with
 * `actionable_text` pre-fills that string into the chat input.
 *
 * The component is intentionally decoupled from `<App />` — App.tsx
 * just mounts this and calls `useEventStream(userId)` once. All
 * coordination happens via the window CustomEvent channel.
 */
import { useEffect, useRef, useState } from "react";
import {
  TOAST_EVENT_NAME,
  type ToastEvent,
  type ToastSeverity,
} from "../hooks/useEventStream";

const MAX_VISIBLE = 3;
const AUTO_DISMISS_MS = 4000;
const CHAT_INPUT_DOM_ID = "omni-chat-input";

interface RenderedToast extends ToastEvent {
  uid: string;
  dismissing?: boolean;
}

const SEVERITY_GLYPH: Record<ToastSeverity, string> = {
  success: "✓",
  info: "i",
  warn: "!",
  error: "×",
};

/**
 * Push `text` into the chat input. We bypass React state because the
 * chat input lives in App.tsx and we're contractually limited to a
 * 1-line mount there. Setting `value` via the native setter +
 * dispatching `input` is what React's synthetic event system listens
 * for — this works regardless of controlled-vs-uncontrolled wiring.
 */
function prefillChatInput(text: string) {
  const el = document.getElementById(CHAT_INPUT_DOM_ID) as HTMLInputElement | null;
  if (!el) return;
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype,
    "value",
  )?.set;
  if (setter) {
    setter.call(el, text);
  } else {
    el.value = text;
  }
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.focus();
}

export function ToastStack() {
  const [toasts, setToasts] = useState<RenderedToast[]>([]);
  // Per-toast timer handles so we can cancel on manual dismiss.
  const timersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  // Clean up all timers on unmount — prevents setState-on-unmounted
  // React dev warnings during fast HMR cycles.
  useEffect(() => {
    return () => {
      timersRef.current.forEach((t) => clearTimeout(t));
      timersRef.current.clear();
    };
  }, []);

  useEffect(() => {
    const dismiss = (uid: string) => {
      const timer = timersRef.current.get(uid);
      if (timer) {
        clearTimeout(timer);
        timersRef.current.delete(uid);
      }
      setToasts((prev) =>
        prev.map((t) => (t.uid === uid ? { ...t, dismissing: true } : t)),
      );
      setTimeout(() => {
        setToasts((prev) => prev.filter((t) => t.uid !== uid));
      }, 200);
    };

    const handler = (e: Event) => {
      const ce = e as CustomEvent<ToastEvent>;
      const detail = ce.detail;
      if (!detail) return;
      const uid = `${detail.ts}-${Math.random().toString(36).slice(2, 8)}`;
      setToasts((prev) => {
        // Cap at MAX_VISIBLE — drop the oldest if we're at the limit.
        const next = [...prev, { ...detail, uid }];
        if (next.length <= MAX_VISIBLE) return next;
        const removed = next.slice(0, next.length - MAX_VISIBLE);
        removed.forEach((r) => {
          const t = timersRef.current.get(r.uid);
          if (t) clearTimeout(t);
          timersRef.current.delete(r.uid);
        });
        return next.slice(-MAX_VISIBLE);
      });
      const timer = setTimeout(() => dismiss(uid), AUTO_DISMISS_MS);
      timersRef.current.set(uid, timer);
    };
    window.addEventListener(TOAST_EVENT_NAME, handler);
    return () => window.removeEventListener(TOAST_EVENT_NAME, handler);
  }, []);

  const dismiss = (uid: string) => {
    const timer = timersRef.current.get(uid);
    if (timer) {
      clearTimeout(timer);
      timersRef.current.delete(uid);
    }
    setToasts((prev) =>
      prev.map((t) => (t.uid === uid ? { ...t, dismissing: true } : t)),
    );
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.uid !== uid));
    }, 200);
  };

  const onToastClick = (toast: RenderedToast) => {
    if (toast.actionable_text) {
      prefillChatInput(toast.actionable_text);
    }
    dismiss(toast.uid);
  };

  if (toasts.length === 0) return null;

  return (
    <div className="toast-stack" aria-live="polite" aria-atomic="false">
      {toasts.map((t) => (
        <div
          key={t.uid}
          className={`toast toast--${t.severity}${t.dismissing ? " toast--dismissing" : ""}${t.actionable_text ? " toast--clickable" : ""}`}
          role={t.severity === "error" || t.severity === "warn" ? "alert" : "status"}
          onClick={() => onToastClick(t)}
        >
          <div className={`toast__icon toast__icon--${t.severity}`}>
            {SEVERITY_GLYPH[t.severity]}
          </div>
          <div className="toast__body">
            <div className="toast__title">{t.title}</div>
            {t.body && <div className="toast__text">{t.body}</div>}
          </div>
          <button
            type="button"
            className="toast__close"
            aria-label="Đóng thông báo"
            onClick={(e) => {
              // Prevent the parent click → prefill from firing.
              e.stopPropagation();
              dismiss(t.uid);
            }}
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
