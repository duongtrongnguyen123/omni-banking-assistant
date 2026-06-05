import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { OmniResponse } from "../types";

type Status = "idle" | "loading" | "playing";

export function useTTS() {
  const [status, setStatus] = useState<Status>("idle");
  const [spokenText, setSpokenText] = useState<string>("");
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const urlRef = useRef<string | null>(null);
  const tokenRef = useRef(0);

  const cleanup = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.src = "";
      audioRef.current = null;
    }
    if (urlRef.current) {
      URL.revokeObjectURL(urlRef.current);
      urlRef.current = null;
    }
  }, []);

  const stop = useCallback(() => {
    tokenRef.current += 1;
    cleanup();
    setStatus("idle");
  }, [cleanup]);

  const playText = useCallback(
    async (text: string, token: number): Promise<void> => {
      setSpokenText(text);
      const blob = await api.tts(text);
      if (token !== tokenRef.current) return;
      const url = URL.createObjectURL(blob);
      urlRef.current = url;
      const audio = new Audio(url);
      audioRef.current = audio;
      await new Promise<void>((resolve) => {
        let settled = false;
        const finish = () => {
          if (settled) return;
          settled = true;
          if (token === tokenRef.current) {
            setStatus("idle");
            URL.revokeObjectURL(url);
            if (urlRef.current === url) urlRef.current = null;
          }
          resolve();
        };
        audio.onplay = () => {
          if (token === tokenRef.current) setStatus("playing");
        };
        audio.onended = finish;
        audio.onerror = finish;
        audio.play().catch(finish);
      });
    },
    [],
  );

  /** Speak the response. Resolves when audio finishes or is interrupted. */
  const speakResponse = useCallback(
    async (response: OmniResponse): Promise<void> => {
      const token = ++tokenRef.current;
      cleanup();
      setStatus("loading");
      try {
        const { text } = await api.voiceText(response);
        if (token !== tokenRef.current) return;
        if (!text) {
          setSpokenText("");
          setStatus("idle");
          return;
        }
        await playText(text, token);
      } catch {
        if (token === tokenRef.current) setStatus("idle");
      }
    },
    [cleanup, playText],
  );

  /** Speak arbitrary text (skip voice-text redaction). */
  const speakText = useCallback(
    async (text: string): Promise<void> => {
      if (!text) return;
      const token = ++tokenRef.current;
      cleanup();
      setStatus("loading");
      try {
        await playText(text, token);
      } catch {
        if (token === tokenRef.current) setStatus("idle");
      }
    },
    [cleanup, playText],
  );

  useEffect(() => () => stop(), [stop]);

  return { status, spokenText, speakResponse, speakText, stop };
}
