import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";

type Status =
  | "idle"
  | "requesting"
  | "recording"
  | "processing"
  | "error"
  | "unsupported";

interface Options {
  onText: (text: string) => void;
  maxDurationMs?: number;
}

const supported = () =>
  typeof window !== "undefined" &&
  !!navigator.mediaDevices?.getUserMedia &&
  typeof MediaRecorder !== "undefined";

const pickMimeType = (): string => {
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/mp4",
    "audio/ogg;codecs=opus",
    "audio/ogg",
  ];
  for (const t of candidates) {
    if (MediaRecorder.isTypeSupported?.(t)) return t;
  }
  return "";
};

export function useRecorder({ onText, maxDurationMs = 30_000 }: Options) {
  const [status, setStatus] = useState<Status>(() =>
    supported() ? "idle" : "unsupported",
  );
  const [error, setError] = useState<string | null>(null);
  const [elapsedMs, setElapsedMs] = useState(0);

  const recRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const startTimeRef = useRef<number>(0);
  const tickRef = useRef<number | null>(null);
  const stopTimerRef = useRef<number | null>(null);
  // Set when the user explicitly stops, so onstop knows whether to upload.
  const shouldSendRef = useRef(false);

  const cleanupStream = () => {
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    if (tickRef.current != null) {
      window.clearInterval(tickRef.current);
      tickRef.current = null;
    }
    if (stopTimerRef.current != null) {
      window.clearTimeout(stopTimerRef.current);
      stopTimerRef.current = null;
    }
  };

  const start = useCallback(async () => {
    if (!supported()) {
      setStatus("unsupported");
      return;
    }
    setError(null);
    setStatus("requesting");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      streamRef.current = stream;
      const mimeType = pickMimeType();
      const rec = mimeType
        ? new MediaRecorder(stream, { mimeType })
        : new MediaRecorder(stream);
      chunksRef.current = [];
      rec.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      rec.onstop = async () => {
        cleanupStream();
        if (!shouldSendRef.current) {
          setStatus("idle");
          chunksRef.current = [];
          return;
        }
        setStatus("processing");
        const blob = new Blob(chunksRef.current, {
          type: rec.mimeType || "audio/webm",
        });
        chunksRef.current = [];
        try {
          const text = await api.stt(blob);
          if (text) onText(text);
          setStatus("idle");
        } catch (e) {
          setError(e instanceof Error ? e.message : String(e));
          setStatus("error");
        }
      };
      rec.onerror = () => {
        setError("Lỗi ghi âm");
        setStatus("error");
        cleanupStream();
      };
      recRef.current = rec;
      shouldSendRef.current = true;
      startTimeRef.current = Date.now();
      setElapsedMs(0);
      rec.start(100); // request data every 100ms
      setStatus("recording");
      tickRef.current = window.setInterval(() => {
        setElapsedMs(Date.now() - startTimeRef.current);
      }, 100);
      // Max-duration safety stop
      stopTimerRef.current = window.setTimeout(() => {
        if (recRef.current?.state === "recording") recRef.current.stop();
      }, maxDurationMs);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      // Common: NotAllowedError when user denies mic permission
      setError(msg);
      setStatus("error");
      cleanupStream();
    }
  }, [onText, maxDurationMs]);

  const stop = useCallback(() => {
    if (recRef.current?.state === "recording") {
      shouldSendRef.current = true;
      recRef.current.stop();
    }
  }, []);

  const cancel = useCallback(() => {
    if (recRef.current?.state === "recording") {
      shouldSendRef.current = false;
      recRef.current.stop();
    } else {
      setStatus("idle");
    }
  }, []);

  useEffect(() => {
    return () => {
      shouldSendRef.current = false;
      if (recRef.current?.state === "recording") {
        try {
          recRef.current.stop();
        } catch {
          /* ignore */
        }
      }
      cleanupStream();
    };
  }, []);

  return { status, error, elapsedMs, start, stop, cancel };
}
