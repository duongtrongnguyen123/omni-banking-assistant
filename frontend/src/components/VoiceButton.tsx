import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";

type VoiceButtonProps = {
  onTranscript: (text: string) => void;
  disabled?: boolean;
};

/**
 * Imperative handle exposed by `<VoiceButton>` via `ref`.
 *
 * Lets the parent stop a listening session it didn't start — e.g. when
 * the user submits the chat form, the mic should release immediately
 * instead of continuing to capture audio and overwrite the cleared
 * input on the next `onresult` event.
 */
export interface VoiceButtonHandle {
  /** Stop recognition if currently listening. Safe to call when idle. */
  stop: () => void;
  /** True while the browser is actively listening. */
  isListening: () => boolean;
}

/**
 * Resolves the browser's Speech Recognition constructor.
 * Prefers the standard `SpeechRecognition`, falls back to the
 * webkit-prefixed variant (Chrome, Edge, Safari iOS 14.5+).
 */
function getRecognitionCtor(): SpeechRecognitionConstructor | undefined {
  if (typeof window === "undefined") return undefined;
  return window.SpeechRecognition ?? window.webkitSpeechRecognition;
}

export const VoiceButton = forwardRef<VoiceButtonHandle, VoiceButtonProps>(
  function VoiceButton({ onTranscript, disabled }, ref) {
  const RecognitionCtor = useMemo(() => getRecognitionCtor(), []);
  const recognitionRef = useRef<SpeechRecognition | null>(null);
  const [listening, setListening] = useState(false);
  const listeningRef = useRef(false);
  const [supported, setSupported] = useState<boolean>(!!RecognitionCtor);

  // If the API isn't available, log once and hide the button entirely.
  useEffect(() => {
    if (!RecognitionCtor) {
      // eslint-disable-next-line no-console
      console.info(
        "[VoiceButton] webkitSpeechRecognition not available in this browser — voice input disabled.",
      );
      setSupported(false);
    }
  }, [RecognitionCtor]);

  const stop = useCallback(() => {
    const rec = recognitionRef.current;
    if (rec) {
      try {
        // `abort()` ends recognition immediately and suppresses any
        // pending final-result event, so a stop triggered by Send
        // can't race in and overwrite the freshly cleared input.
        rec.abort();
      } catch {
        // already stopped — safe to ignore
      }
    }
    listeningRef.current = false;
    setListening(false);
  }, []);

  // Imperative handle so the parent (App.tsx) can stop listening when
  // the user submits the form, clears the input, etc.
  useImperativeHandle(
    ref,
    () => ({
      stop,
      isListening: () => listeningRef.current,
    }),
    [stop],
  );

  // Stop recognition when the tab loses focus (saves battery, avoids
  // capturing audio while user is elsewhere).
  useEffect(() => {
    const onVisibility = () => {
      if (document.hidden && recognitionRef.current) {
        stop();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("blur", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("blur", onVisibility);
    };
  }, [stop]);

  // Cleanup on unmount.
  useEffect(() => {
    return () => {
      recognitionRef.current?.abort();
      recognitionRef.current = null;
    };
  }, []);

  const start = useCallback(() => {
    if (!RecognitionCtor) return;
    // Re-instantiate every time: many implementations only allow a single
    // .start() cycle per instance.
    const rec = new RecognitionCtor();
    rec.lang = "vi-VN";
    rec.interimResults = true;
    rec.continuous = false;
    rec.maxAlternatives = 1;

    rec.onresult = (event: SpeechRecognitionEvent) => {
      let buffer = "";
      for (let i = 0; i < event.results.length; i += 1) {
        const result = event.results[i];
        buffer += result[0]?.transcript ?? "";
      }
      onTranscript(buffer);
    };

    rec.onerror = (event: SpeechRecognitionErrorEvent) => {
      // Silently ignore the expected end-of-utterance / no-speech signals.
      if (event.error === "no-speech" || event.error === "aborted") {
        return;
      }
      // eslint-disable-next-line no-console
      console.warn("[VoiceButton] recognition error:", event.error);
    };

    rec.onend = () => {
      listeningRef.current = false;
      setListening(false);
      recognitionRef.current = null;
    };

    rec.onstart = () => {
      listeningRef.current = true;
      setListening(true);
    };

    recognitionRef.current = rec;
    // IMPORTANT: must be called synchronously from the click handler so
    // mobile Safari treats this as a user-initiated gesture.
    try {
      rec.start();
    } catch (err) {
      // Some browsers throw InvalidStateError if start() is called twice.
      // eslint-disable-next-line no-console
      console.warn("[VoiceButton] failed to start recognition:", err);
      recognitionRef.current = null;
      listeningRef.current = false;
      setListening(false);
    }
  }, [RecognitionCtor, onTranscript]);

  const toggle = useCallback(() => {
    if (listening) {
      stop();
    } else {
      start();
    }
  }, [listening, start, stop]);

  if (!supported) {
    return null;
  }

  const label = listening ? "Đang ghi âm — nhấn để dừng" : "Bật ghi âm";
  const tooltip = listening ? "Đang nghe…" : "Nhấn để nói";

  return (
    <button
      type="button"
      className={
        "phone__voice-btn" +
        (listening ? " phone__voice-btn--listening" : "")
      }
      onClick={toggle}
      disabled={disabled}
      aria-label={label}
      aria-pressed={listening}
      title={tooltip}
    >
      <svg
        width="20"
        height="20"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <rect x="9" y="3" width="6" height="12" rx="3" />
        <path d="M5 11a7 7 0 0 0 14 0" />
        <line x1="12" y1="18" x2="12" y2="22" />
        <line x1="8" y1="22" x2="16" y2="22" />
      </svg>
    </button>
  );
  },
);

export default VoiceButton;
