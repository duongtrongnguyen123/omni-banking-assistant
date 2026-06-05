/**
 * Forensic "why did Omni do that?" drawer.
 *
 * Listens for two events on window:
 *   - "omni-audit-open"          → open the drawer
 *   - "omni-audit-focus" (detail.id) → open and scroll to that audit event
 *
 * That keeps coupling with App.tsx minimal — TransactionCard etc can dispatch
 * the focus event without prop-drilling.
 */
import { useCallback, useEffect, useRef, useState } from "react";

interface ExplainStep {
  layer: "nlu" | "context" | "safety" | "banking" | string;
  decision: string;
  rationale: string;
  rationale_en?: string;
  source?: "llm" | "rule" | "unknown";
}

interface ExplainPayload {
  audit_id: string;
  summary: string;
  steps: ExplainStep[];
  raw_audit_event: Record<string, unknown>;
}

interface AuditRow {
  id: string;
  created_at: string;
  intent: string;
  decision: string;
  nlu_source: "llm" | "rule" | "unknown";
  entities: Record<string, unknown>;
  resolved_recipient: string | null;
  selected_account: string | null;
  safety_flags: string[];
  auth_required: string[];
  auth_completed: string[];
  message: string;
  summary?: string;
}

const HEADERS = { "x-user-id": "u_an" };

async function fetchLast(limit = 20): Promise<AuditRow[]> {
  const res = await fetch(`/api/audit/last?limit=${limit}`, { headers: HEADERS });
  if (!res.ok) throw new Error(`audit/last failed: ${res.status}`);
  return res.json();
}

async function fetchExplain(id: string): Promise<ExplainPayload> {
  const res = await fetch(`/api/audit/${id}/explain`, { headers: HEADERS });
  if (!res.ok) throw new Error(`audit/explain failed: ${res.status}`);
  return res.json();
}

function intentBadgeColor(intent: string): string {
  switch (intent) {
    case "transfer":
      return "#2563eb";
    case "balance":
      return "#16a34a";
    case "history":
      return "#a855f7";
    case "schedule":
      return "#f59e0b";
    case "add_contact":
      return "#0ea5e9";
    case "smalltalk":
      return "#64748b";
    default:
      return "#64748b";
  }
}

function layerColor(layer: string, hasFlag: boolean): string {
  if (layer === "nlu") return "#2563eb";
  if (layer === "context") return "#a855f7";
  if (layer === "safety") return hasFlag ? "#dc2626" : "#16a34a";
  if (layer === "banking") return "#16a34a";
  return "#64748b";
}

function formatTimestamp(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return iso;
  }
}

interface CardProps {
  row: AuditRow;
  expanded: boolean;
  onToggle: () => void;
  cardRef?: (el: HTMLDivElement | null) => void;
}

const AuditCard = ({ row, expanded, onToggle, cardRef }: CardProps) => {
  const [explain, setExplain] = useState<ExplainPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!expanded || explain || loading) return;
    setLoading(true);
    fetchExplain(row.id)
      .then((p) => setExplain(p))
      .catch((e) => setErr(String(e instanceof Error ? e.message : e)))
      .finally(() => setLoading(false));
  }, [expanded, row.id, explain, loading]);

  const hasFlag = (row.safety_flags?.length ?? 0) > 0;

  return (
    <div className="audit-card" ref={cardRef}>
      <button className="audit-card__head" onClick={onToggle} aria-expanded={expanded}>
        <span className="audit-card__ts">{formatTimestamp(row.created_at)}</span>
        <span
          className="audit-card__badge"
          style={{ background: intentBadgeColor(row.intent) }}
        >
          {row.intent}
        </span>
        <span className="audit-card__summary">{row.summary ?? `${row.intent} · ${row.decision}`}</span>
        <span className="audit-card__chev" data-open={expanded}>›</span>
      </button>
      {expanded && (
        <div className="audit-card__body">
          {loading && <div className="audit-card__loading">Đang tải…</div>}
          {err && <div className="audit-card__err">Lỗi: {err}</div>}
          {explain && (
            <>
              <div className="audit-card__kv">
                <strong>User msg:</strong>
                <span>{row.message || "—"}</span>
              </div>
              <div className="audit-card__kv">
                <strong>Tóm tắt:</strong>
                <span>{explain.summary}</span>
              </div>
              <ol className="audit-timeline">
                {explain.steps.map((s, i) => {
                  const c = layerColor(s.layer, hasFlag);
                  return (
                    <li key={i} className="audit-step">
                      <span className="audit-step__dot" style={{ background: c }} />
                      <div className="audit-step__body">
                        <div className="audit-step__head">
                          <span
                            className="audit-step__layer"
                            style={{ color: c, borderColor: c }}
                          >
                            {s.layer}
                          </span>
                          <span className="audit-step__decision">{s.decision}</span>
                          {s.source && (
                            <span className="audit-step__source">[{s.source}]</span>
                          )}
                        </div>
                        <div className="audit-step__rationale">{s.rationale}</div>
                        {s.rationale_en && s.rationale_en !== s.rationale && (
                          <div className="audit-step__rationale audit-step__rationale--en">
                            {s.rationale_en}
                          </div>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ol>
            </>
          )}
        </div>
      )}
    </div>
  );
};

export const AuditDrawer = () => {
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const cardRefs = useRef<Map<string, HTMLDivElement | null>>(new Map());
  const pendingFocus = useRef<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const data = await fetchLast(20);
      setRows(data);
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) {
      load();
    }
  }, [open, load]);

  // Listen for external open/focus events. App.tsx, TransactionCard, etc. fire
  // these without needing a prop wired through.
  useEffect(() => {
    const onOpen = () => setOpen(true);
    const onFocus = (e: Event) => {
      const detail = (e as CustomEvent<{ id?: string }>).detail;
      if (detail?.id) {
        pendingFocus.current = detail.id;
        setExpanded((prev) => {
          const next = new Set(prev);
          next.add(detail.id!);
          return next;
        });
      }
      setOpen(true);
    };
    window.addEventListener("omni-audit-open", onOpen);
    window.addEventListener("omni-audit-focus", onFocus as EventListener);
    return () => {
      window.removeEventListener("omni-audit-open", onOpen);
      window.removeEventListener("omni-audit-focus", onFocus as EventListener);
    };
  }, []);

  // After rows render, scroll to the requested focus card.
  useEffect(() => {
    if (!open || !pendingFocus.current) return;
    const id = pendingFocus.current;
    const el = cardRefs.current.get(id);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      pendingFocus.current = null;
    }
  }, [open, rows]);

  const toggle = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <>
      <button
        className="audit-fab"
        aria-label="Vì sao Omni quyết định như vậy?"
        title="Vì sao Omni quyết định như vậy?"
        onClick={() => setOpen(true)}
      >
        ?
      </button>
      {open && (
        <div className="audit-drawer__scrim" onClick={() => setOpen(false)} />
      )}
      <aside
        className={`audit-drawer ${open ? "audit-drawer--open" : ""}`}
        aria-hidden={!open}
      >
        <header className="audit-drawer__head">
          <div>
            <div className="audit-drawer__title">Vì sao Omni làm vậy?</div>
            <div className="audit-drawer__sub">
              20 quyết định gần nhất · nguồn: backend audit log
            </div>
          </div>
          <button
            className="audit-drawer__close"
            onClick={() => setOpen(false)}
            aria-label="Đóng"
          >
            ×
          </button>
        </header>
        <div className="audit-drawer__body">
          {loading && <div className="audit-drawer__empty">Đang tải…</div>}
          {err && <div className="audit-drawer__empty audit-drawer__empty--err">Lỗi: {err}</div>}
          {!loading && !err && rows.length === 0 && (
            <div className="audit-drawer__empty">
              Chưa có quyết định nào được ghi nhận. Hãy thử "Chuyển mẹ 2 triệu".
            </div>
          )}
          {rows.map((r) => (
            <AuditCard
              key={r.id}
              row={r}
              expanded={expanded.has(r.id)}
              onToggle={() => toggle(r.id)}
              cardRef={(el) => {
                if (el) cardRefs.current.set(r.id, el);
                else cardRefs.current.delete(r.id);
              }}
            />
          ))}
        </div>
      </aside>
    </>
  );
};
