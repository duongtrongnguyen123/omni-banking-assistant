import { useRecorder } from "../hooks/useRecorder";

const MicIcon = ({ off = false }: { off?: boolean }) => (
  <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
    <path
      d="M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3Z"
      fill="currentColor"
    />
    <path
      d="M5 11a7 7 0 0 0 14 0M12 18v3"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
    />
    {off && (
      <path
        d="M3 3l18 18"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
    )}
  </svg>
);

const StopIcon = () => (
  <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
    <rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" />
  </svg>
);

const Spinner = () => (
  <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
    <circle
      cx="12"
      cy="12"
      r="9"
      fill="none"
      stroke="currentColor"
      strokeWidth="3"
      strokeLinecap="round"
      strokeDasharray="40 60"
    >
      <animateTransform
        attributeName="transform"
        type="rotate"
        from="0 12 12"
        to="360 12 12"
        dur="0.9s"
        repeatCount="indefinite"
      />
    </circle>
  </svg>
);

interface Props {
  disabled?: boolean;
  onText: (text: string) => void;
}

const fmt = (ms: number) => {
  const s = Math.floor(ms / 1000);
  const sec = (s % 60).toString().padStart(2, "0");
  const min = Math.floor(s / 60);
  return `${min}:${sec}`;
};

export const MicButton = ({ disabled, onText }: Props) => {
  const { status, error, elapsedMs, start, stop, cancel } = useRecorder({
    onText,
  });

  const unsupported = status === "unsupported";
  const recording = status === "recording";
  const processing = status === "processing" || status === "requesting";

  const click = () => {
    if (unsupported) return;
    if (recording) {
      stop();
      return;
    }
    if (processing) return;
    start();
  };

  const title = unsupported
    ? "Trình duyệt không hỗ trợ ghi âm"
    : recording
      ? `Đang ghi âm ${fmt(elapsedMs)} — bấm để dừng và gửi`
      : processing
        ? "Đang nhận diện…"
        : error
          ? `Lỗi: ${error}. Bấm để thử lại.`
          : "Bấm để ghi âm";

  let content: React.ReactNode;
  if (processing) content = <Spinner />;
  else if (recording) content = <StopIcon />;
  else content = <MicIcon off={unsupported} />;

  return (
    <div className="mic-wrap">
      {recording && (
        <div className="mic-status">
          <span className="mic-status__dot" />
          <span className="mic-status__time">{fmt(elapsedMs)}</span>
          <button
            type="button"
            className="mic-status__cancel"
            onClick={cancel}
            aria-label="Huỷ ghi âm"
          >
            Huỷ
          </button>
        </div>
      )}
      <button
        type="button"
        className={`btn btn--icon mic-btn ${recording ? "mic-btn--on" : ""} ${
          processing ? "mic-btn--busy" : ""
        }`}
        onClick={click}
        disabled={disabled || unsupported || processing}
        aria-label={title}
        title={title}
        aria-pressed={recording}
      >
        {content}
        {recording && <span className="mic-pulse" aria-hidden="true" />}
      </button>
    </div>
  );
};
