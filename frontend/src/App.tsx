import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api/client";
import type { ChatMessage, Contact, OmniResponse } from "./types";
import { Message } from "./components/Message";
import { OmniAvatar } from "./components/OmniAvatar";
import { QuickScenarios } from "./components/QuickScenarios";
import { RecentRecipients } from "./components/RecentRecipients";
import { VoiceMode } from "./components/VoiceMode";

const newId = () => Math.random().toString(36).slice(2, 10);

const WELCOME: ChatMessage = {
  id: "welcome",
  role: "omni",
  text:
    "Chào An! Mình là Omni — bạn cần chuyển tiền, xem số dư, hay tra lịch sử? Hãy nói thật tự nhiên nhé.",
};

export default function App() {
  const [messages, setMessages] = useState<ChatMessage[]>([WELCOME]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [closedDraftIds, setClosedDraftIds] = useState<Set<string>>(new Set());
  const [closedScheduleDraftIds, setClosedScheduleDraftIds] = useState<Set<string>>(new Set());
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [voiceOpen, setVoiceOpen] = useState(false);

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
    async (text: string): Promise<OmniResponse | null> => {
      const trimmed = text.trim();
      if (!trimmed || busy) return null;
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
        return resp;
      } catch (e) {
        failOmni(pendingId, e);
        return null;
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

  const onConfirm = (
    draftId: string,
    payload: {
      otp?: string;
      biometric_verified?: boolean;
      source_account_id?: string;
    },
  ) => {
    const label =
      payload.otp && payload.biometric_verified
        ? "Xác minh OTP + sinh trắc học"
        : payload.biometric_verified
          ? "Xác minh sinh trắc học"
          : payload.otp
            ? "Xác minh OTP"
            : "Xác nhận";
    return sendDraftAction(() => api.confirm(draftId, payload), label, draftId);
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

  return (
    <div className="page">
      <div className="phone">
        <div className="phone__statusbar">9:41</div>
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
            placeholder="Nhập câu lệnh, hoặc nhấn 🎙 để nói…"
            disabled={busy}
          />
          {input.trim() ? (
            <button
              className="btn btn--primary btn--send"
              onClick={() => send(input)}
              disabled={busy}
              aria-label="Gửi"
            >
              ➤
            </button>
          ) : (
            <button
              className="btn btn--send voice-trigger"
              onClick={() => setVoiceOpen(true)}
              disabled={busy}
              aria-label="Trò chuyện bằng giọng nói"
              title="Trò chuyện bằng giọng nói"
            >
              🎙
            </button>
          )}
        </div>

        <VoiceMode
          open={voiceOpen}
          onClose={() => setVoiceOpen(false)}
          send={send}
        />
      </div>

      <aside className="sidebar">
        <h1 className="sidebar__brand">
          Omni <span>AI Assistant</span>
        </h1>
        <p className="sidebar__lead">
          Ứng dụng xử lý ngôn ngữ tự nhiên trong hoạt động ngân hàng — Team One
          Last Token.
        </p>
        <QuickScenarios onPick={(t) => send(t)} />
        <div className="sidebar__legend">
          <div>
            <strong>Pipeline:</strong> Câu lệnh → Hiểu ý định → Trích xuất →
            Ngữ cảnh cá nhân → Kiểm tra an toàn → Thực thi.
          </div>
          <div>
            <strong>Mock user:</strong> An — số dư tài khoản chính 24.350.000đ.
          </div>
        </div>
      </aside>
    </div>
  );
}
