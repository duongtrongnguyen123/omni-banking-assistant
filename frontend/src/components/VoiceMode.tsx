import { useEffect, useRef, useState } from "react";
import type { OmniResponse } from "../types";
import { useSpeechRecognition } from "../hooks/useSpeechRecognition";
import { useTTS } from "../hooks/useTTS";

type Mode =
  | "greeting"
  | "listening"
  | "thinking"
  | "speaking"
  | "error"
  | "unsupported";

interface Props {
  open: boolean;
  onClose: () => void;
  send: (text: string) => Promise<OmniResponse | null>;
}

const GREETING =
  "Xin chào An. Mình là Omni. Bạn muốn chuyển khoản cho ai, hay xem số dư?";

const CloseIcon = () => (
  <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
    <path
      d="M6 6l12 12M18 6L6 18"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
    />
  </svg>
);

const MODE_LABEL: Record<Mode, string> = {
  greeting: "Mình đang chào bạn…",
  listening: "Đang lắng nghe…",
  thinking: "Mình đang xử lý…",
  speaking: "Mình đang trả lời…",
  error: "Có lỗi xảy ra",
  unsupported: "Trình duyệt không hỗ trợ giọng nói",
};

export const VoiceMode = ({ open, onClose, send }: Props) => {
  const [mode, setMode] = useState<Mode>("greeting");
  const [userText, setUserText] = useState("");
  const [omniText, setOmniText] = useState("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const openRef = useRef(open);
  const processingRef = useRef(false);

  useEffect(() => {
    openRef.current = open;
  }, [open]);

  const tts = useTTS();

  const handleFinal = async (transcript: string) => {
    if (!openRef.current || !transcript) return;
    // Ignore final results that arrive while we're already processing/speaking
    if (processingRef.current) return;
    processingRef.current = true;
    setUserText(transcript);
    setOmniText("");
    setMode("thinking");
    try {
      const resp = await send(transcript);
      if (!openRef.current) {
        processingRef.current = false;
        return;
      }
      if (!resp) {
        setMode("listening");
        processingRef.current = false;
        return;
      }
      setOmniText(resp.text);
      setMode("speaking");
      await tts.speakResponse(resp);
      processingRef.current = false;
      if (!openRef.current) return;
      setMode("listening");
    } catch (e) {
      processingRef.current = false;
      setErrorMsg(e instanceof Error ? e.message : String(e));
      setMode("error");
    }
  };

  const stt = useSpeechRecognition({
    lang: "vi-VN",
    onFinalResult: handleFinal,
  });

  // Open → greeting → listening (sequential)
  useEffect(() => {
    if (!open) {
      stt.stop();
      tts.stop();
      processingRef.current = false;
      return;
    }
    setUserText("");
    setOmniText("");
    setErrorMsg(null);
    if (stt.status === "unsupported") {
      setMode("unsupported");
      return;
    }
    setMode("greeting");
    let cancelled = false;
    (async () => {
      await tts.speakText(GREETING);
      if (cancelled || !openRef.current) return;
      setMode("listening");
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Drive STT off mode only (not stt.status, which would loop on onend).
  useEffect(() => {
    if (!open) return;
    if (mode === "listening") {
      stt.start();
    } else {
      stt.stop();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, mode]);

  const close = () => {
    stt.stop();
    tts.stop();
    onClose();
  };

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!open) return null;

  const interim = stt.interim;
  const liveUser = interim || userText;
  const liveOmni =
    mode === "speaking" || mode === "greeting"
      ? tts.spokenText || omniText
      : omniText;

  const retryListen = () => {
    setErrorMsg(null);
    setMode("listening");
  };

  // Visual style: greeting reuses speaking orb.
  const orbStyle = mode === "greeting" ? "speaking" : mode;

  return (
    <div className="voice" role="dialog" aria-label="Trò chuyện bằng giọng nói">
      <button
        type="button"
        className="voice__close"
        onClick={close}
        aria-label="Đóng chế độ giọng nói"
      >
        <CloseIcon />
      </button>

      <div className={`voice__orb voice__orb--${orbStyle}`}>
        <div className="voice__orb-core" />
        <div className="voice__orb-ring" />
        <div className="voice__orb-ring voice__orb-ring--2" />
      </div>

      <div className="voice__status">{MODE_LABEL[mode]}</div>

      <div className="voice__transcript">
        {liveOmni && (
          <div className="voice__bubble voice__bubble--omni">{liveOmni}</div>
        )}
        {liveUser && (
          <div className="voice__bubble voice__bubble--user">{liveUser}</div>
        )}
        {mode === "error" && errorMsg && (
          <div className="voice__err">
            {errorMsg}
            <button
              type="button"
              className="btn btn--ghost voice__retry"
              onClick={retryListen}
            >
              Thử lại
            </button>
          </div>
        )}
        {mode === "unsupported" && (
          <div className="voice__err">
            Trình duyệt này không hỗ trợ nhận diện giọng nói. Vui lòng dùng
            Chrome hoặc Edge.
          </div>
        )}
      </div>

      <div className="voice__hint">
        Bạn nói tự nhiên, ví dụ: "Chuyển cho mẹ 2 triệu" hoặc "Số dư của tôi".
      </div>
    </div>
  );
};
