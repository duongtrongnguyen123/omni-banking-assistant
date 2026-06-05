import { useCallback, useEffect, useMemo, useState } from "react";

/**
 * First-time-user tutorial.
 *
 * 4 steps, slide-up modal anchored at the bottom of the phone frame:
 *   1. Welcome
 *   2. Try the chat input  (auto-advances when user sends first message)
 *   3. Confirm flow        (highlights Xác nhận when first draft appears)
 *   4. You're done
 *
 * Persistence:
 *   localStorage["omni.tutorial.completed"] = "1" on completion OR skip.
 *   Skips on every subsequent visit UNLESS ?tutorial=1 forces re-run.
 *
 * Offline-demo:
 *   OMNI_OFFLINE_DEMO=1 (Vite env) auto-skips. Judges in offline-demo
 *   mode never see the overlay surprise them mid-pitch.
 *
 * Highlight targets (CSS selectors below) match elements rendered by
 * App.tsx — keep them in sync.
 */

const STORAGE_KEY = "omni.tutorial.completed";

const SELECTOR_INPUT = "#omni-chat-input";
const SELECTOR_CONFIRM = "[data-onboarding='confirm']";
const SELECTOR_QUICK = ".quick-scenarios";

export interface TutorialOverlayProps {
  // Reported by App.tsx so the tutorial can auto-advance.
  userMessageCount: number;
  draftVisible: boolean;
  // Allow programmatic dismissal (e.g. /tutorial slash command not yet
  // wired but exposed for parity with skill discovery).
  onClose?: () => void;
}

type StepKey = "welcome" | "chat" | "confirm" | "done";

interface Step {
  key: StepKey;
  title: string;
  body: string;
  highlight?: string;
  // When true, the primary button advances; otherwise it closes.
  isFinal?: boolean;
}

const STEPS: Step[] = [
  {
    key: "welcome",
    title: "Chào! Mình là Omni, trợ lý ngân hàng của bạn.",
    body: "Để mình hướng dẫn 30 giây thôi.",
  },
  {
    key: "chat",
    title: "Thử gõ câu lệnh",
    body: "Gõ \"chuyển mẹ 2 triệu\" hoặc nhấn nút mic 🎤 để nói. Omni hiểu cả biệt danh và lịch sử.",
    highlight: SELECTOR_INPUT,
  },
  {
    key: "confirm",
    title: "Xác nhận trước khi chuyển",
    body: "Mỗi giao dịch đều cần bạn xác nhận. OTP demo: 123456.",
    highlight: SELECTOR_CONFIRM,
  },
  {
    key: "done",
    title: "Sẵn sàng!",
    body: "Bạn có thể gõ /help bất cứ lúc nào hoặc xem các kịch bản demo ở sidebar.",
    highlight: SELECTOR_QUICK,
    isFinal: true,
  },
];

const readForceFlag = (): boolean => {
  if (typeof window === "undefined") return false;
  try {
    return new URLSearchParams(window.location.search).get("tutorial") === "1";
  } catch {
    return false;
  }
};

const readOfflineDemo = (): boolean => {
  // Vite exposes import.meta.env at build time; we also check the
  // runtime window flag the offline-demo backend toggle injects.
  try {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const env = (import.meta as any)?.env ?? {};
    if (env.VITE_OMNI_OFFLINE_DEMO === "1") return true;
  } catch {
    /* ignore */
  }
  try {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    if ((window as any).OMNI_OFFLINE_DEMO === "1") return true;
  } catch {
    /* ignore */
  }
  return false;
};

const readCompleted = (): boolean => {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    return false;
  }
};

const writeCompleted = (): void => {
  try {
    window.localStorage.setItem(STORAGE_KEY, "1");
  } catch {
    /* private mode / quota — silently ignore */
  }
};

export const TutorialOverlay = ({
  userMessageCount,
  draftVisible,
  onClose,
}: TutorialOverlayProps) => {
  const forceRun = useMemo(() => readForceFlag(), []);
  const offline = useMemo(() => readOfflineDemo(), []);
  const initiallyActive = useMemo(() => {
    if (offline) return false;
    if (forceRun) return true;
    return !readCompleted();
  }, [forceRun, offline]);

  const [active, setActive] = useState<boolean>(initiallyActive);
  const [stepIdx, setStepIdx] = useState<number>(0);
  const [highlightRect, setHighlightRect] = useState<DOMRect | null>(null);

  const step = STEPS[stepIdx];

  // Auto-advance from step 2 (chat) when the user sends their first
  // message. Idempotent — only fires while we're still on the chat step.
  useEffect(() => {
    if (!active) return;
    if (step.key !== "chat") return;
    if (userMessageCount > 0) {
      setStepIdx((i) => Math.min(i + 1, STEPS.length - 1));
    }
  }, [active, step.key, userMessageCount]);

  // Auto-advance from step 3 (confirm) when a draft becomes visible.
  useEffect(() => {
    if (!active) return;
    if (step.key !== "confirm") return;
    if (draftVisible) {
      // Keep the highlight on the confirm button for a moment so the
      // user reads the OTP hint before we close.
    }
  }, [active, step.key, draftVisible]);

  // Track the highlighted element's position so we can punch a hole in
  // the dim layer. Re-measure on resize and every step change.
  useEffect(() => {
    if (!active || !step.highlight) {
      setHighlightRect(null);
      return undefined;
    }
    const update = () => {
      const el = document.querySelector<HTMLElement>(step.highlight!);
      setHighlightRect(el ? el.getBoundingClientRect() : null);
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    const id = window.setInterval(update, 400);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
      window.clearInterval(id);
    };
  }, [active, step.highlight, stepIdx]);

  const finish = useCallback(() => {
    writeCompleted();
    setActive(false);
    onClose?.();
  }, [onClose]);

  const next = useCallback(() => {
    if (step.isFinal) {
      finish();
      return;
    }
    setStepIdx((i) => Math.min(i + 1, STEPS.length - 1));
  }, [finish, step.isFinal]);

  if (!active) return null;

  return (
    <div
      className="tutorial-overlay"
      role="dialog"
      aria-modal="false"
      aria-labelledby="tutorial-title"
      aria-describedby="tutorial-body"
    >
      {/* Dim layer + spotlight ring (rendered behind the modal). */}
      <div className="tutorial-overlay__dim" aria-hidden="true" />
      {highlightRect && (
        <div
          className="tutorial-overlay__spotlight"
          aria-hidden="true"
          style={{
            top: Math.max(0, highlightRect.top - 6),
            left: Math.max(0, highlightRect.left - 6),
            width: highlightRect.width + 12,
            height: highlightRect.height + 12,
          }}
        />
      )}
      <div className="tutorial-overlay__sheet">
        <div className="tutorial-overlay__progress" aria-hidden="true">
          {STEPS.map((s, i) => (
            <span
              key={s.key}
              className={`tutorial-overlay__dot ${i === stepIdx ? "is-active" : ""} ${i < stepIdx ? "is-done" : ""}`}
            />
          ))}
        </div>
        <h2 id="tutorial-title" className="tutorial-overlay__title">
          {step.title}
        </h2>
        <p id="tutorial-body" className="tutorial-overlay__body">
          {step.body}
        </p>
        <div className="tutorial-overlay__actions">
          {!step.isFinal && (
            <button
              type="button"
              className="tutorial-overlay__skip"
              onClick={finish}
            >
              Bỏ qua
            </button>
          )}
          <button
            type="button"
            className="tutorial-overlay__primary"
            onClick={next}
            autoFocus
          >
            {step.key === "welcome"
              ? "Bắt đầu"
              : step.isFinal
                ? "Hoàn tất"
                : "Tiếp tục"}
          </button>
        </div>
      </div>
    </div>
  );
};

export const __TUTORIAL_STORAGE_KEY = STORAGE_KEY;
