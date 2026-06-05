/**
 * Demo recorder + replayer controls.
 *
 * Hidden by default. Surfaced only when the URL has `?demo=1`. The
 * Record button toggles a capture on the backend; Stop downloads a
 * `.omni-demo.jsonl` file. The Replay button reads a JSONL file from
 * disk and POSTs it to `/api/demo/replay`.
 *
 * Cadence is configurable via `?speed=<ms>` — defaults to 800ms so the
 * chat animations have time to land between turns.
 */
import { useEffect, useState } from "react";
import { api } from "../api/client";

const readSearchParam = (key: string): string | null => {
  if (typeof window === "undefined") return null;
  try {
    return new URLSearchParams(window.location.search).get(key);
  } catch {
    return null;
  }
};

const isDemoMode = (): boolean => readSearchParam("demo") === "1";

const cadenceFromUrl = (): number => {
  const raw = readSearchParam("speed");
  if (!raw) return 800;
  const parsed = parseInt(raw, 10);
  if (Number.isNaN(parsed) || parsed < 0 || parsed > 10_000) return 800;
  return parsed;
};

const downloadJsonl = (jsonl: string) => {
  const blob = new Blob([jsonl], { type: "application/x-ndjson" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  a.download = `${stamp}.omni-demo.jsonl`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
};

export function DemoRecorder() {
  const [enabled] = useState(isDemoMode);
  const [recording, setRecording] = useState(false);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string>("");

  // Reflect server-side state on mount so a page refresh during a
  // recording doesn't desync the button.
  useEffect(() => {
    if (!enabled) return;
    api
      .recordStatus()
      .then((s) => setRecording(s.recording))
      .catch(() => {
        /* status endpoint optional — degrade silently */
      });
  }, [enabled]);

  if (!enabled) return null;

  const toggleRecord = async () => {
    if (busy) return;
    setBusy(true);
    try {
      if (!recording) {
        await api.recordStart();
        setRecording(true);
        setStatus("Đang ghi lại — gõ kịch bản demo, bấm Stop để lưu.");
      } else {
        const r = await api.recordStop();
        setRecording(false);
        if (r.turns > 0) {
          downloadJsonl(r.jsonl);
          setStatus(`Đã lưu ${r.turns} lượt (${r.duration_ms}ms).`);
        } else {
          setStatus("Chưa có lượt nào — bỏ qua.");
        }
      }
    } catch (e) {
      setStatus(`Lỗi: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const onReplayPick = async (file: File) => {
    setBusy(true);
    setStatus("Đang phát lại…");
    try {
      const text = await file.text();
      // Each line is a JSON object {ts, user, omni}. Reuse `user` only.
      const script: Array<{ user: string }> = [];
      for (const line of text.split(/\r?\n/)) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        try {
          const obj = JSON.parse(trimmed);
          if (typeof obj.user === "string" && obj.user) {
            script.push({ user: obj.user });
          }
        } catch {
          /* ignore malformed line */
        }
      }
      if (script.length === 0) {
        setStatus("File không có lượt nào hợp lệ.");
        return;
      }
      const cadence = cadenceFromUrl();
      const r = await api.replay(script, cadence);
      setStatus(`Đã phát ${r.played} lượt trong ${r.duration_ms}ms.`);
    } catch (e) {
      setStatus(`Lỗi phát lại: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="demo-rec" role="toolbar" aria-label="Demo recorder">
      <button
        type="button"
        className={`demo-rec__btn ${recording ? "demo-rec__btn--rec" : ""}`}
        onClick={toggleRecord}
        disabled={busy}
        title={recording ? "Dừng ghi" : "Bắt đầu ghi kịch bản demo"}
      >
        {recording ? "■ Stop" : "● Record"}
      </button>
      <label className="demo-rec__btn demo-rec__btn--file">
        Replay
        <input
          type="file"
          accept=".jsonl,.ndjson,application/x-ndjson,application/json,text/plain"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) onReplayPick(f);
            e.target.value = "";
          }}
          disabled={busy}
          hidden
        />
      </label>
      {status && <div className="demo-rec__status">{status}</div>}
    </div>
  );
}
