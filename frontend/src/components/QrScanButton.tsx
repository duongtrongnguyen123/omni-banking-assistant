import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";

interface Props {
  onPrefill: (text: string) => void;
  disabled?: boolean;
  onToast?: (msg: string) => void;
}

/**
 * Camera-driven QR scanner.
 *
 * Tap flow:
 *   1. Probe ``navigator.mediaDevices.getUserMedia`` for a back-facing
 *      camera. No support → button stays hidden (graceful degrade).
 *   2. Open an overlay with a <video> stream.
 *   3. Tick rAF, draw the frame to an offscreen <canvas>, hand the
 *      ImageData to ``jsQR``.
 *   4. On decode → POST /api/qr/decode (server verifies CRC + TLV) →
 *      pre-fill the chat input with
 *      ``chuyển cho <STK> @ <bank> <amount>đ <message>``.
 *   5. Stop tracks + close overlay.
 *
 * Permission denied / decode timeout → toast, no fatal state.
 */
export function QrScanButton({ onPrefill, disabled, onToast }: Props) {
  const [supported, setSupported] = useState<boolean>(false);
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<string>("");
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const rafRef = useRef<number | null>(null);
  const jsQRRef = useRef<typeof import("jsqr").default | null>(null);
  const decodedRef = useRef<boolean>(false);

  // Feature-detect on mount. Hides the button if the user-agent has
  // no camera plumbing (in-app webviews, old Safari, headless tests).
  useEffect(() => {
    const has =
      typeof navigator !== "undefined" &&
      !!navigator.mediaDevices &&
      typeof navigator.mediaDevices.getUserMedia === "function";
    setSupported(has);
  }, []);

  const fireToast = useCallback(
    (msg: string) => {
      if (onToast) {
        onToast(msg);
        return;
      }
      try {
        window.dispatchEvent(
          new CustomEvent("omni:toast", { detail: { message: msg } }),
        );
      } catch {
        /* ignore */
      }
    },
    [onToast],
  );

  const stopCamera = useCallback(() => {
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    const stream = streamRef.current;
    if (stream) {
      stream.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
    }
    if (videoRef.current) {
      videoRef.current.srcObject = null;
    }
  }, []);

  const close = useCallback(() => {
    stopCamera();
    setOpen(false);
    setStatus("");
    decodedRef.current = false;
  }, [stopCamera]);

  const onDecoded = useCallback(
    async (payloadText: string) => {
      if (decodedRef.current) return;
      decodedRef.current = true;
      setStatus("Đã nhận diện QR — đang giải mã…");
      try {
        const out = await api.qrDecode(payloadText);
        // Compose a natural-language pre-fill the orchestrator can run
        // through its normal transfer pipeline (alias resolver → safety
        // → confirm). We embed the bank + masked-ish account so the
        // alias resolver can match if the recipient is already saved,
        // and we name the raw STK in case it isn't.
        const segments: string[] = ["chuyển cho"];
        segments.push(out.account_number);
        segments.push(`(${out.bank})`);
        if (out.amount !== null && out.amount !== undefined) {
          segments.push(`${out.amount.toLocaleString("vi-VN")}đ`);
        }
        if (out.message) {
          segments.push(`nội dung "${out.message}"`);
        }
        const prefill = segments.join(" ");
        onPrefill(prefill);
        fireToast("Đã quét QR — kiểm tra rồi gửi.");
        close();
      } catch (e) {
        decodedRef.current = false;
        setStatus("");
        fireToast(
          `Không giải mã được QR: ${String(
            e instanceof Error ? e.message : e,
          )}`,
        );
      }
    },
    [onPrefill, close, fireToast],
  );

  const tick = useCallback(() => {
    if (!open) return;
    const video = videoRef.current;
    const canvas = canvasRef.current;
    const jsQR = jsQRRef.current;
    if (!video || !canvas || !jsQR) {
      rafRef.current = requestAnimationFrame(tick);
      return;
    }
    if (video.readyState !== video.HAVE_ENOUGH_DATA) {
      rafRef.current = requestAnimationFrame(tick);
      return;
    }
    const w = video.videoWidth;
    const h = video.videoHeight;
    if (w === 0 || h === 0) {
      rafRef.current = requestAnimationFrame(tick);
      return;
    }
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.drawImage(video, 0, 0, w, h);
    const imageData = ctx.getImageData(0, 0, w, h);
    const found = jsQR(imageData.data, w, h, {
      inversionAttempts: "dontInvert",
    });
    if (found && found.data) {
      void onDecoded(found.data);
      return;
    }
    rafRef.current = requestAnimationFrame(tick);
  }, [open, onDecoded]);

  const start = useCallback(async () => {
    if (!supported || disabled) return;
    setOpen(true);
    setStatus("Đang mở máy ảnh…");
    decodedRef.current = false;
    try {
      // Dynamic import keeps the chat bundle slim until the user
      // actually taps the QR button. jsQR is ~45KB gzipped.
      if (!jsQRRef.current) {
        const mod = await import("jsqr");
        jsQRRef.current = mod.default;
      }
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        // Prefer the back camera on phones; falls back to whatever's
        // available on desktop.
        video: { facingMode: { ideal: "environment" } },
      });
      streamRef.current = stream;
      const video = videoRef.current;
      if (!video) {
        stream.getTracks().forEach((t) => t.stop());
        setStatus("Không khởi tạo được khung hình.");
        return;
      }
      video.srcObject = stream;
      // muted + playsInline are mandatory for iOS Safari to actually
      // start the stream inline (no fullscreen takeover).
      video.muted = true;
      video.setAttribute("playsinline", "true");
      await video.play().catch(() => {
        /* ignore — tick loop will retry once readyState is good */
      });
      setStatus("Hướng máy ảnh vào QR…");
      rafRef.current = requestAnimationFrame(tick);
    } catch (e) {
      stopCamera();
      setOpen(false);
      const name =
        e instanceof Error
          ? e.name || ""
          : typeof e === "object" && e
          ? String((e as { name?: unknown }).name ?? "")
          : "";
      if (
        name === "NotAllowedError" ||
        name === "SecurityError" ||
        name === "PermissionDeniedError"
      ) {
        fireToast("Bạn chưa cấp quyền máy ảnh.");
      } else if (name === "NotFoundError" || name === "OverconstrainedError") {
        fireToast("Không tìm thấy máy ảnh phía sau.");
      } else {
        fireToast("Không mở được máy ảnh.");
      }
    }
  }, [supported, disabled, tick, stopCamera, fireToast]);

  // Cleanup tracks when the component unmounts or the modal closes.
  useEffect(() => {
    return () => stopCamera();
  }, [stopCamera]);

  if (!supported) return null;

  return (
    <>
      <button
        type="button"
        className="phone__qr-btn"
        onClick={start}
        disabled={disabled || open}
        aria-label="Quét QR để chuyển tiền"
        title="Quét QR"
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
          <rect x="3" y="3" width="7" height="7" rx="1" />
          <rect x="14" y="3" width="7" height="7" rx="1" />
          <rect x="3" y="14" width="7" height="7" rx="1" />
          <path d="M14 14h3v3h-3z" />
          <path d="M20 14v3" />
          <path d="M14 20h3" />
          <path d="M20 20v1" />
        </svg>
      </button>
      {open && (
        <div
          className="qr-scan-modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="qr-scan-title"
        >
          <div className="qr-scan-modal__backdrop" onClick={close} />
          <div className="qr-scan-modal__card">
            <header className="qr-scan-modal__header">
              <h2 id="qr-scan-title" className="qr-scan-modal__title">
                Quét QR chuyển tiền
              </h2>
              <button
                type="button"
                className="qr-scan-modal__close"
                onClick={close}
                aria-label="Đóng"
              >
                <span aria-hidden="true">×</span>
              </button>
            </header>
            <div className="qr-scan-modal__viewport">
              <video
                ref={videoRef}
                className="qr-scan-modal__video"
                playsInline
                muted
              />
              <canvas ref={canvasRef} hidden />
              <div className="qr-scan-modal__reticle" aria-hidden="true" />
            </div>
            <p className="qr-scan-modal__status" role="status" aria-live="polite">
              {status}
            </p>
          </div>
        </div>
      )}
    </>
  );
}

export default QrScanButton;
