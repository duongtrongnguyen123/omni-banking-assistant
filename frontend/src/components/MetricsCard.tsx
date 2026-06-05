/**
 * Live operational metrics card — bottom-right floating widget.
 *
 * Activated by ``?metrics=1`` on the URL. Polls ``/api/metrics`` every
 * 2s, parses the Prometheus text exposition into a small in-memory
 * shape, and renders the SLO numbers an on-call would actually look at:
 *
 *   * Total chat requests + per-intent breakdown
 *   * P50 / P95 chat latency (computed across all intents from the
 *     bucket counts — close enough for a glance dashboard)
 *   * Safety flag counts split warn / block
 *   * LLM call counts split success / 429 / other
 *   * Active session gauge
 *   * Toast events published
 *
 * The card is intentionally small (200x300, opacity 0.85) so it
 * doesn't cover the chat phone frame even on a 1024-wide laptop. We
 * keep the parser tiny — it only needs to handle our own metric
 * names, not arbitrary Prometheus output.
 */
import { useEffect, useState } from "react";

const REFRESH_MS = 2000;

interface ParsedMetrics {
  chat_total: number;
  chat_by_intent: Array<{ intent: string; count: number }>;
  chat_p50_ms: number | null;
  chat_p95_ms: number | null;
  safety_warn: number;
  safety_block: number;
  llm_ok: number;
  llm_429: number;
  llm_other: number;
  session_active: number;
  toasts_total: number;
}

const isEnabled = (): boolean => {
  if (typeof window === "undefined") return false;
  try {
    return new URLSearchParams(window.location.search).get("metrics") === "1";
  } catch {
    return false;
  }
};

/** Parse one ``name{labels} value`` exposition line into the parts we need. */
function parseSample(line: string): {
  name: string;
  labels: Record<string, string>;
  value: number;
} | null {
  if (!line || line.startsWith("#")) return null;
  // ``foo_bucket{intent="balance",le="0.025"} 1`` shape. We use a
  // regex rather than a full parser — Prometheus values are always
  // the last whitespace-separated token.
  const m = line.match(/^(\w+)(\{[^}]*\})?\s+([-+0-9eE.NaN+Inf]+)\s*$/);
  if (!m) return null;
  const name = m[1];
  const value = parseFloat(m[3]);
  const labels: Record<string, string> = {};
  if (m[2]) {
    // Strip the braces and split key="value" pairs. Label values cannot
    // contain unescaped commas or quotes, so a simple split works.
    const inner = m[2].slice(1, -1);
    const re = /(\w+)="((?:[^"\\]|\\.)*)"/g;
    let match: RegExpExecArray | null;
    while ((match = re.exec(inner)) !== null) {
      labels[match[1]] = match[2]
        .replace(/\\"/g, '"')
        .replace(/\\n/g, "\n")
        .replace(/\\\\/g, "\\");
    }
  }
  return { name, labels, value };
}

/**
 * Estimate a percentile from cumulative bucket counts.
 *
 * Returns the upper bound of the first bucket whose cumulative count
 * meets ``targetCount = total * p``. The standard Prometheus
 * ``histogram_quantile`` does linear interpolation inside the bucket;
 * we deliberately don't — a glance dashboard reads cleaner with the
 * bucket edge ("≤25ms", "≤100ms") than with a synthetic 18.4ms.
 */
function percentileFromBuckets(
  buckets: Array<{ le: number; cumulative: number }>,
  total: number,
  p: number,
): number | null {
  if (total === 0) return null;
  const target = total * p;
  for (const b of buckets) {
    if (b.cumulative >= target) return b.le;
  }
  return Infinity; // fell into the +Inf bucket
}

function parseMetrics(text: string): ParsedMetrics {
  const out: ParsedMetrics = {
    chat_total: 0,
    chat_by_intent: [],
    chat_p50_ms: null,
    chat_p95_ms: null,
    safety_warn: 0,
    safety_block: 0,
    llm_ok: 0,
    llm_429: 0,
    llm_other: 0,
    session_active: 0,
    toasts_total: 0,
  };
  // Aggregate bucket counts across all intent labels — the card shows a
  // single SLO number.
  const bucketAgg: Record<string, number> = {};
  let chatHistTotal = 0;

  const byIntent: Record<string, number> = {};

  for (const raw of text.split("\n")) {
    const s = parseSample(raw);
    if (!s) continue;
    switch (s.name) {
      case "omni_chat_requests_total":
        out.chat_total += s.value;
        if (s.labels.intent) {
          byIntent[s.labels.intent] = (byIntent[s.labels.intent] || 0) + s.value;
        }
        break;
      case "omni_chat_latency_seconds_bucket":
        if (s.labels.le) {
          // ``+Inf`` rendered literally; treat as Infinity.
          const le = s.labels.le === "+Inf" ? Infinity : parseFloat(s.labels.le);
          bucketAgg[String(le)] = (bucketAgg[String(le)] || 0) + s.value;
        }
        break;
      case "omni_chat_latency_seconds_count":
        chatHistTotal += s.value;
        break;
      case "omni_safety_flag_total":
        if (s.labels.severity === "warn") out.safety_warn += s.value;
        else if (s.labels.severity === "block") out.safety_block += s.value;
        break;
      case "omni_llm_call_total":
        if (s.labels.status === "ok") out.llm_ok += s.value;
        else if (s.labels.status === "429") out.llm_429 += s.value;
        else out.llm_other += s.value;
        break;
      case "omni_session_active":
        out.session_active = s.value;
        break;
      case "omni_toast_published_total":
        out.toasts_total += s.value;
        break;
    }
  }

  out.chat_by_intent = Object.entries(byIntent)
    .map(([intent, count]) => ({ intent, count }))
    .sort((a, b) => b.count - a.count);

  // Sort buckets by le and convert to a cumulative array. Sample
  // boundaries that appear across intents already arrived as
  // cumulative-per-intent — summing keeps the cumulative property
  // because each intent's bucket counts are themselves cumulative.
  const sortedLe = Object.keys(bucketAgg)
    .map(Number)
    .sort((a, b) => a - b);
  const cumBuckets = sortedLe.map((le) => ({ le, cumulative: bucketAgg[String(le)] }));

  out.chat_p50_ms = (() => {
    const v = percentileFromBuckets(cumBuckets, chatHistTotal, 0.5);
    return v == null || v === Infinity ? v : Math.round(v * 1000);
  })() as number | null;
  out.chat_p95_ms = (() => {
    const v = percentileFromBuckets(cumBuckets, chatHistTotal, 0.95);
    return v == null || v === Infinity ? v : Math.round(v * 1000);
  })() as number | null;

  return out;
}

export function MetricsCard() {
  const [enabled] = useState<boolean>(isEnabled());
  const [data, setData] = useState<ParsedMetrics | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;

    const tick = async () => {
      try {
        const res = await fetch("/api/metrics");
        if (!res.ok) {
          if (!cancelled) setError(`HTTP ${res.status}`);
          return;
        }
        const text = await res.text();
        if (cancelled) return;
        setData(parseMetrics(text));
        setError(null);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    };

    tick();
    const id = window.setInterval(tick, REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [enabled]);

  if (!enabled) return null;

  const renderMs = (v: number | null): string => {
    if (v == null) return "—";
    if (!isFinite(v)) return "≥1s";
    return `${v}ms`;
  };

  return (
    <div className="metrics-card" role="complementary" aria-label="Live metrics">
      <div className="metrics-card__header">OMNI · METRICS</div>
      {error && <div className="metrics-card__err">{error}</div>}
      {!data && !error && <div className="metrics-card__row">Loading…</div>}
      {data && (
        <>
          <div className="metrics-card__row">
            <span>Chat requests</span>
            <span>{data.chat_total}</span>
          </div>
          {data.chat_by_intent.slice(0, 4).map((r) => (
            <div className="metrics-card__row metrics-card__row--sub" key={r.intent}>
              <span>· {r.intent}</span>
              <span>{r.count}</span>
            </div>
          ))}
          <div className="metrics-card__row">
            <span>P50 / P95</span>
            <span>
              {renderMs(data.chat_p50_ms)} / {renderMs(data.chat_p95_ms)}
            </span>
          </div>
          <div className="metrics-card__row">
            <span>Safety warn / block</span>
            <span>
              {data.safety_warn} / {data.safety_block}
            </span>
          </div>
          <div className="metrics-card__row">
            <span>LLM ok / 429 / err</span>
            <span>
              {data.llm_ok} / {data.llm_429} / {data.llm_other}
            </span>
          </div>
          <div className="metrics-card__row">
            <span>Active sessions</span>
            <span>{data.session_active}</span>
          </div>
          <div className="metrics-card__row">
            <span>Toasts</span>
            <span>{data.toasts_total}</span>
          </div>
        </>
      )}
    </div>
  );
}
