import { useEffect, useState } from "react";

/**
 * Footer widget that surfaces backend readiness as a colored dot.
 *
 * Polls `/health/ready` every 5s. The dot encodes one of three states:
 *
 *   * green  — backend is up AND every readiness gate passes.
 *   * yellow — backend is up (200 from /health/live) but at least one
 *              readiness gate is failing (e.g. embedder still warming
 *              up after a cold boot).
 *   * red    — backend is unreachable. The tooltip mentions the network
 *              error so the operator knows it's not a backend bug.
 *
 * Tooltip carries the full last `/health/ready` JSON so an operator
 * can hover and see *which* gate failed. We keep this lightweight on
 * purpose — no portal, no animation library; the brief asks for a
 * small footer signal, not a dashboard.
 */

type ReadyCheck = boolean | "n/a";

interface ReadyPayload {
  ready: boolean;
  checks: {
    sqlite: ReadyCheck;
    suggester: ReadyCheck;
    embedder: ReadyCheck;
    redis: ReadyCheck;
  };
}

type Status = "green" | "yellow" | "red";

const POLL_MS = 5000;

const COLORS: Record<Status, string> = {
  green: "#2bb673",
  yellow: "#f2b134",
  red: "#d94c4c",
};

const LABELS: Record<Status, string> = {
  green: "Ready",
  yellow: "Khởi động",
  red: "Mất kết nối",
};

export function HealthStatus() {
  const [status, setStatus] = useState<Status>("yellow");
  const [payload, setPayload] = useState<ReadyPayload | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    const tick = async () => {
      try {
        // /health/ready returns 200 (green) or 503 (yellow). Both
        // ship the same JSON body so we always parse before deciding.
        const res = await fetch("/health/ready");
        let body: ReadyPayload | null = null;
        try {
          body = (await res.json()) as ReadyPayload;
        } catch {
          // Body wasn't JSON — treat as yellow (alive, weird).
        }
        if (cancelled) return;
        setPayload(body);
        setError(null);
        if (res.ok && body?.ready) {
          setStatus("green");
        } else {
          // Backend responded but a gate failed → yellow. We don't
          // surface 503 as red because k8s' readinessProbe will already
          // pull the pod from the LB; a transient yellow is the right
          // UX cue.
          setStatus("yellow");
        }
      } catch (e) {
        if (cancelled) return;
        setStatus("red");
        setError(e instanceof Error ? e.message : String(e));
        setPayload(null);
      }
    };

    void tick();
    const id = window.setInterval(() => void tick(), POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  const tooltip = error
    ? `Backend unreachable: ${error}`
    : payload
      ? JSON.stringify(payload, null, 2)
      : "Đang kiểm tra…";

  return (
    <div
      className="health-status"
      title={tooltip}
      role="status"
      aria-label={`Backend status: ${LABELS[status]}`}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "4px 8px",
        fontSize: 11,
        color: "rgba(255,255,255,0.7)",
        cursor: "help",
        userSelect: "none",
      }}
    >
      <span
        aria-hidden="true"
        style={{
          display: "inline-block",
          width: 8,
          height: 8,
          borderRadius: "50%",
          backgroundColor: COLORS[status],
          boxShadow: `0 0 6px ${COLORS[status]}`,
        }}
      />
      <span>{LABELS[status]}</span>
    </div>
  );
}
