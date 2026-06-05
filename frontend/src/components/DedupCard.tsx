import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { DedupCandidate, DedupGroup } from "../types";

const REASON_LABEL: Record<string, string> = {
  same_account_number: "Cùng số tài khoản",
  same_bank_prefix_and_alias_overlap: "Cùng ngân hàng, tên trùng",
  alias_exact_match: "Biệt danh trùng tên",
};

function fmtVnd(n: number): string {
  return n.toLocaleString("vi-VN") + "đ";
}

function CandidateBlock({
  c,
  isPrimary,
}: {
  c: DedupCandidate;
  isPrimary?: boolean;
}) {
  const last4 =
    c.account_masked?.replace(/\D/g, "").slice(-4) ||
    c.account_number?.slice(-4) ||
    "";
  return (
    <div className={`dedup__col${isPrimary ? " dedup__col--primary" : ""}`}>
      {isPrimary && <div className="dedup__pill">Giữ lại</div>}
      <div className="dedup__name">{c.display_name}</div>
      {c.label && <div className="dedup__sublabel">{c.label}</div>}
      <div className="dedup__sub">
        {c.bank} · *{last4}
      </div>
      <div className="dedup__stats">
        <span>{c.tx_count} giao dịch</span>
        <span>· {fmtVnd(c.tx_total)}</span>
      </div>
      {c.aliases.length > 0 && (
        <div className="dedup__aliases">
          {c.aliases.slice(0, 4).map((a) => (
            <span key={a} className="dedup__chip">
              {a}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export const DedupCard = ({
  onMerged,
}: {
  onMerged?: () => void;
}) => {
  const [groups, setGroups] = useState<DedupGroup[] | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [activeIdx, setActiveIdx] = useState(0);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .contactDuplicates()
      .then((data) => {
        if (!cancelled) setGroups(data);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 2400);
    return () => clearTimeout(t);
  }, [toast]);

  if (error) return null;
  if (!groups || groups.length === 0) return null;

  const active = groups[activeIdx];

  const merge = async () => {
    if (!active) return;
    setBusy(true);
    try {
      const res = await api.mergeContacts(
        active.primary.id,
        active.candidates.map((c) => c.id),
      );
      setToast(
        `Đã gộp ${active.candidates.length + 1} danh bạ — ${res.merged_tx_count} giao dịch chuyển sang ${active.primary.display_name}.`,
      );
      // Drop this group; if more remain, keep modal open; else close.
      const remaining = groups.filter((_, i) => i !== activeIdx);
      setGroups(remaining);
      setActiveIdx(0);
      if (remaining.length === 0) setModalOpen(false);
      onMerged?.();
    } catch (e) {
      setToast(`Không gộp được: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const count = groups.length;

  return (
    <>
      <button
        type="button"
        className="dedup__banner"
        onClick={() => {
          setActiveIdx(0);
          setModalOpen(true);
        }}
      >
        <span className="dedup__wand" aria-hidden>
          ✦
        </span>
        <span className="dedup__banner-text">
          {count === 1
            ? "Có thể bạn có 2 danh bạ trùng — gộp lại?"
            : `Có thể bạn có ${count} nhóm danh bạ trùng — gộp lại?`}
        </span>
      </button>

      {modalOpen && active && (
        <div
          className="dedup__overlay"
          role="dialog"
          aria-modal="true"
          onClick={(e) => {
            if (e.target === e.currentTarget) setModalOpen(false);
          }}
        >
          <div className="dedup__modal">
            <div className="dedup__head">
              <div>
                <div className="dedup__title">Gộp danh bạ trùng</div>
                <div className="dedup__reason">
                  {REASON_LABEL[active.reason] || active.reason}
                </div>
              </div>
              <button
                type="button"
                className="dedup__close"
                aria-label="Đóng"
                onClick={() => setModalOpen(false)}
              >
                ✕
              </button>
            </div>

            <div className="dedup__grid">
              <CandidateBlock c={active.primary} isPrimary />
              {active.candidates.map((c) => (
                <CandidateBlock key={c.id} c={c} />
              ))}
            </div>

            <div className="dedup__actions">
              {groups.length > 1 && (
                <span className="dedup__counter">
                  Nhóm {activeIdx + 1} / {groups.length}
                </span>
              )}
              <button
                type="button"
                className="btn btn--ghost"
                onClick={() => setModalOpen(false)}
                disabled={busy}
              >
                Bỏ qua
              </button>
              <button
                type="button"
                className="btn btn--primary"
                onClick={merge}
                disabled={busy}
              >
                {busy ? "Đang gộp…" : "Gộp"}
              </button>
            </div>
          </div>
        </div>
      )}

      {toast && <div className="dedup__toast">{toast}</div>}
    </>
  );
};
