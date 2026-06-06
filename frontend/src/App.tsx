import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api/client";
import type { BiometricScanResult, ChatMessage, Contact, OmniResponse, TransactionDraft } from "./types";
import { Message } from "./components/Message";
import { OmniAvatar } from "./components/OmniAvatar";
import { QuickScenarios } from "./components/QuickScenarios";
import { BiometricFaceScan } from "./components/BiometricFaceScan";

const newId = () => Math.random().toString(36).slice(2, 10);
const currentClock = () =>
  new Intl.DateTimeFormat("vi-VN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date());

const WELCOME: ChatMessage = {
  id: "welcome",
  role: "omni",
  text:
    "Chào An! Mình là Omni - bạn cần chuyển tiền, xem số dư, hay tra lịch sử? Hãy nói thật tự nhiên nhé.",
};

interface PendingAuth {
  draftId: string;
  draft: TransactionDraft;
  sourceAccountId?: string;
}

export default function App() {
  const [messages, setMessages] = useState<ChatMessage[]>([WELCOME]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [closedDraftIds, setClosedDraftIds] = useState<Set<string>>(new Set());
  const [closedScheduleDraftIds, setClosedScheduleDraftIds] = useState<Set<string>>(new Set());
  const [pendingAuth, setPendingAuth] = useState<PendingAuth | null>(null);
  const [authStage, setAuthStage] = useState<"otp" | "biometric">("otp");
  const [authOtp, setAuthOtp] = useState("");
  const [clock, setClock] = useState(currentClock);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    const tick = window.setInterval(() => setClock(currentClock()), 1000);
    return () => window.clearInterval(tick);
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages]);

  const appendUser = (text: string): ChatMessage => {
    const m: ChatMessage = { id: newId(), role: "user", text };
    setMessages((prev) => [...prev, m]);
    return m;
  };

  const appendOmniPending = (): string => {
    const id = newId();
    setMessages((prev) => [
      ...prev,
      { id, role: "omni", text: "", pending: true },
    ]);
    return id;
  };

  const resolveOmni = (id: string, resp: OmniResponse) => {
    setMessages((prev) =>
      prev.map((m) =>
        m.id === id
          ? { ...m, text: resp.text, response: resp, pending: false }
          : m,
      ),
    );
  };

  const failOmni = (id: string, err: unknown) => {
    setMessages((prev) =>
      prev.map((m) =>
        m.id === id
          ? {
              ...m,
              text: `Lỗi: ${String(err instanceof Error ? err.message : err)}`,
              pending: false,
            }
          : m,
      ),
    );
  };

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || busy) return;
      appendUser(trimmed);
      const pendingId = appendOmniPending();
      setInput("");
      setBusy(true);
      try {
        const resp = await api.chat(trimmed);
        if (resp.draft) {
          setClosedDraftIds((prev) => {
            const next = new Set(prev);
            next.delete(resp.draft!.id);
            return next;
          });
        }
        resolveOmni(pendingId, resp);
      } catch (e) {
        failOmni(pendingId, e);
      } finally {
        setBusy(false);
      }
    },
    [busy],
  );

  const sendDraftAction = async (
    action: () => Promise<OmniResponse>,
    actionLabel: string,
    closeDraftId?: string,
  ) => {
    appendUser(actionLabel);
    const pendingId = appendOmniPending();
    setBusy(true);
    try {
      const resp = await action();
      if (closeDraftId && !resp.draft) {
        setClosedDraftIds((prev) => new Set(prev).add(closeDraftId));
      }
      resolveOmni(pendingId, resp);
    } catch (e) {
      failOmni(pendingId, e);
    } finally {
      setBusy(false);
    }
  };

  const completeDraftAction = async (
    action: () => Promise<OmniResponse>,
    closeDraftId?: string,
  ) => {
    const pendingId = appendOmniPending();
    setBusy(true);
    try {
      const resp = await action();
      if (closeDraftId && !resp.draft) {
        setClosedDraftIds((prev) => new Set(prev).add(closeDraftId));
      }
      resolveOmni(pendingId, resp);
    } catch (e) {
      failOmni(pendingId, e);
    } finally {
      setBusy(false);
    }
  };

  const onConfirm = (
    draftId: string,
    draft: TransactionDraft,
    sourceAccountId?: string,
  ) => {
    const needsOtp = draft.auth_required?.includes("otp") ?? draft.requires_step_up;
    const needsBiometric = draft.auth_required?.includes("biometric");

    if (!needsOtp && !needsBiometric) {
      completeDraftAction(() => api.confirm(draftId, "", sourceAccountId), draftId);
      return;
    }

    setPendingAuth({ draftId, draft, sourceAccountId });
    setAuthStage(needsOtp ? "otp" : "biometric");
    setAuthOtp("");
  };

  const finishAuth = (biometricScan?: BiometricScanResult) => {
    if (!pendingAuth) return;
    const { draftId, sourceAccountId } = pendingAuth;
    const otp = authOtp.replace(/\D/g, "").slice(0, 6);
    setPendingAuth(null);
    setAuthOtp("");
    completeDraftAction(
      () => api.confirm(draftId, otp, sourceAccountId, biometricScan),
      draftId,
    );
  };

  const submitOtp = () => {
    if (!pendingAuth) return;
    if (pendingAuth.draft.auth_required?.includes("biometric")) {
      setAuthStage("biometric");
      return;
    }
    finishAuth();
  };

  const onCancel = (draftId: string) =>
    sendDraftAction(() => api.cancel(draftId), "Huỷ", draftId);

  const onSelectCandidate = (draftId: string, contact: Contact) =>
    sendDraftAction(
      () => api.select(draftId, contact.id),
      `Chọn ${contact.display_name}`,
    );

  const actionableDraftIds = new Set<string>();
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const draft = messages[i].response?.draft;
    if (draft && draft.recipient && !closedDraftIds.has(draft.id)) {
      actionableDraftIds.add(draft.id);
      break;
    }
  }

  const onConfirmContact = (draftId: string) =>
    sendDraftAction(() => api.confirmContact(draftId), "Lưu danh bạ");

  const onCancelContact = (draftId: string) =>
    sendDraftAction(() => api.cancelContact(draftId), "Huỷ lưu danh bạ");

  const onConfirmSchedule = (
    draftId: string,
    otp: string,
    sourceAccountId?: string,
  ) =>
    sendDraftAction(
      async () => {
        const resp = await api.confirmSchedule(draftId, otp, sourceAccountId);
        if (!resp.schedule_draft) {
          setClosedScheduleDraftIds((prev) => new Set(prev).add(draftId));
        }
        return resp;
      },
      "Xác minh OTP",
    );

  const onCancelSchedule = (draftId: string) =>
    sendDraftAction(() => api.cancelSchedule(draftId), "Huỷ đặt lịch");

  const actionableScheduleDraftIds = new Set<string>();
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const draft = messages[i].response?.schedule_draft;
    if (draft && !closedScheduleDraftIds.has(draft.id)) {
      actionableScheduleDraftIds.add(draft.id);
      break;
    }
  }

  const cleanAuthOtp = authOtp.replace(/\D/g, "").slice(0, 6);

  return (
    <div className="page">
      <div className="phone">
        <div className="phone__statusbar">{clock}</div>
        <header className="phone__header">
          <OmniAvatar size={40} />
          <div className="phone__title">
            <div className="phone__brand">OMNI</div>
            <div className="phone__sub">
              <span className="online-dot" /> Trợ lý đang trực tuyến
            </div>
          </div>
          <div className="user-pill">AN</div>
        </header>

        <div className="phone__chat" ref={scrollRef}>
          <div className="day-divider">Hôm nay · 08:14</div>
          {messages.map((m) => (
            <Message
              key={m.id}
              message={m}
              onConfirm={onConfirm}
              onCancel={onCancel}
              onSelectCandidate={onSelectCandidate}
              onConfirmContact={onConfirmContact}
              onCancelContact={onCancelContact}
              onConfirmSchedule={onConfirmSchedule}
              onCancelSchedule={onCancelSchedule}
              busy={busy}
              actionableDraftIds={actionableDraftIds}
              actionableScheduleDraftIds={actionableScheduleDraftIds}
            />
          ))}
        </div>

        <div className="phone__input">
          <RecentRecipients
            disabled={busy}
            onPick={(name) => {
              const prefix = `Chuyển cho ${name} `;
              setInput(prefix);
              requestAnimationFrame(() => {
                const el = inputRef.current;
                if (!el) return;
                el.focus();
                el.setSelectionRange(prefix.length, prefix.length);
              });
            }}
          />
          <input
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send(input);
              }
            }}
            placeholder="Nhập câu lệnh, ví dụ: chuyển cho mẹ 2 triệu..."
            disabled={busy}
          />
          <button
            className="btn btn--primary btn--send"
            onClick={() => send(input)}
            disabled={busy || !input.trim()}
            aria-label="Gửi"
          >
            &gt;
          </button>
        </div>

        {pendingAuth && (
          <div className={`auth-overlay ${authStage === "biometric" ? "auth-overlay--full" : ""}`}>
            {authStage === "otp" ? (
              <div className="auth-card">
                <div className="auth-card__eyebrow">Xác thực giao dịch</div>
                <h2>Nhập mã OTP</h2>
                <p>
                  Mã demo: <strong>123456</strong>
                </p>
                <input
                  className="otp-input auth-card__otp"
                  value={cleanAuthOtp}
                  onChange={(e) => setAuthOtp(e.target.value.replace(/\D/g, "").slice(0, 6))}
                  inputMode="numeric"
                  maxLength={6}
                  placeholder="******"
                  autoFocus
                />
                <div className="auth-card__actions">
                  <button
                    className="btn btn--ghost"
                    onClick={() => {
                      setPendingAuth(null);
                      setAuthOtp("");
                    }}
                    disabled={busy}
                  >
                    Huỷ
                  </button>
                  <button
                    className="btn btn--primary"
                    onClick={submitOtp}
                    disabled={busy || cleanAuthOtp.length !== 6}
                  >
                    Tiếp tục
                  </button>
                </div>
              </div>
            ) : (
              <BiometricFaceScan
                open
                challengeId={`${pendingAuth.draftId}:${cleanAuthOtp || "no-otp"}`}
                onClose={() => setPendingAuth(null)}
                onVerified={(scanResult) => finishAuth(scanResult)}
              />
            )}
          </div>
        )}
      </div>

      <aside className="sidebar">
        <h1 className="sidebar__brand">
          Omni <span>AI Assistant</span>
        </h1>
        <p className="sidebar__lead">
          Ứng dụng xử lý ngôn ngữ tự nhiên trong hoạt động ngân hàng - Team One Last Token.
        </p>
        <QuickScenarios onPick={send} />
        <div className="sidebar__legend">
          <div>
            <strong>Pipeline:</strong> Câu lệnh - Hiểu ý định - Trích xuất -
            Ngữ cảnh cá nhân - Kiểm tra an toàn - Thực thi.
          </div>
          <div>
            <strong>Mock user:</strong> An - số dư tài khoản chính 100.000.000đ.
          </div>
        </div>
      </aside>
    </div>
  );
}
