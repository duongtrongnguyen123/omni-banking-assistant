import { useCallback, useEffect, useRef, useState } from "react";

type Status = "idle" | "listening" | "error" | "unsupported";

interface SpeechRecognitionAlternative {
  transcript: string;
  confidence: number;
}
interface SpeechRecognitionResult {
  isFinal: boolean;
  0: SpeechRecognitionAlternative;
}
interface SpeechRecognitionEvent extends Event {
  resultIndex: number;
  results: ArrayLike<SpeechRecognitionResult>;
}
interface SpeechRecognitionErrorEvent extends Event {
  error: string;
}

interface BrowserSpeechRecognition extends EventTarget {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((e: SpeechRecognitionEvent) => void) | null;
  onerror: ((e: SpeechRecognitionErrorEvent) => void) | null;
  onend: (() => void) | null;
  onstart: (() => void) | null;
}

type SpeechRecognitionCtor = new () => BrowserSpeechRecognition;

const getCtor = (): SpeechRecognitionCtor | null => {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    SpeechRecognition?: SpeechRecognitionCtor;
    webkitSpeechRecognition?: SpeechRecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
};

interface Options {
  lang?: string;
  onFinalResult?: (transcript: string) => void;
}

export function useSpeechRecognition({
  lang = "vi-VN",
  onFinalResult,
}: Options = {}) {
  const [status, setStatus] = useState<Status>(() =>
    getCtor() ? "idle" : "unsupported",
  );
  const [interim, setInterim] = useState("");
  const [error, setError] = useState<string | null>(null);
  const recRef = useRef<BrowserSpeechRecognition | null>(null);
  const finalCbRef = useRef(onFinalResult);

  useEffect(() => {
    finalCbRef.current = onFinalResult;
  }, [onFinalResult]);

  const start = useCallback(() => {
    const Ctor = getCtor();
    if (!Ctor) {
      setStatus("unsupported");
      return;
    }
    if (recRef.current) recRef.current.abort();
    const rec = new Ctor();
    rec.lang = lang;
    // continuous=true keeps the mic open across pauses so the indicator
    // doesn't flicker between every utterance.
    rec.continuous = true;
    rec.interimResults = true;
    rec.onstart = () => {
      setStatus("listening");
      setInterim("");
      setError(null);
    };
    rec.onresult = (e) => {
      let interimText = "";
      let finalText = "";
      for (let i = e.resultIndex; i < e.results.length; i += 1) {
        const r = e.results[i];
        if (r.isFinal) finalText += r[0].transcript;
        else interimText += r[0].transcript;
      }
      if (interimText) setInterim(interimText);
      if (finalText) {
        setInterim("");
        finalCbRef.current?.(finalText.trim());
      }
    };
    rec.onerror = (e) => {
      setError(e.error || "speech_error");
      setStatus("error");
    };
    rec.onend = () => {
      setStatus("idle");
      setInterim("");
    };
    recRef.current = rec;
    try {
      rec.start();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setStatus("error");
    }
  }, [lang]);

  const stop = useCallback(() => {
    recRef.current?.stop();
  }, []);

  useEffect(() => () => recRef.current?.abort(), []);

  return { status, interim, error, start, stop };
}
