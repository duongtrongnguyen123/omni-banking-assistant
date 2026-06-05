import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api/client";
import type { BalanceResult, ChatMessage, Contact, OmniResponse } from "./types";
import { ContactPicker } from "./components/ContactPicker";
import { Message } from "./components/Message";
import { InsightsCard } from "./components/InsightsCard";
import { OmniAvatar } from "./components/OmniAvatar";
import { QuickScenarios } from "./components/QuickScenarios";
import { VoiceButton } from "./components/VoiceButton";
import { SuggestionStrip } from "./components/SuggestionStrip";
import { RepeatLastCTA } from "./components/RepeatLastCTA";
import { ToastStack } from "./components/ToastStack";
import { useEventStream } from "./hooks/useEventStream";
import {
  SlashPalette,
  buildMessageFromSlash,
  type SlashCommand,
} from "./components/SlashPalette";
import { RecipientAutocomplete } from "./components/RecipientAutocomplete";
import { useKeyboard } from "./hooks/useKeyboard";
import { cancelSpeech, isSpeechSupported } from "./lib/tts";
import { formatVND } from "./format";

const TTS_STORAGE_KEY = "omni.tts.enabled";

const readStoredTtsPref = (): boolean => {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(TTS_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
};

const newId = () => Math.random().toString(36).slice(2, 10);

const INPUT_DOM_ID = "omni-chat-input";

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
  const [pickerOpen, setPickerOpen] = useState(false);
  // Bumped after every executed transfer so the suggestion strip re-ranks.
  const [suggestRefresh, setSuggestRefresh] = useState(0);
  // Counts confirmed transfers this session — gates the "Lặp lại lần
  // trước" CTA so it only appears once there's something to repeat.
  const [confirmedTransfers, setConfirmedTransfers] = useState(0);
  const [ttsEnabled, setTtsEnabled] = useState<boolean>(() => readStoredTtsPref());
  const ttsSupported = isSpeechSupported();
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Power-user state.
  const [slashOpen, setSlashOpen] = useState(false);
  const [slashQuery, setSlashQuery] = useState("");
  const [mentionOpen, setMentionOpen] = useState(false);
  const [mentionQuery, setMentionQuery] = useState("");
  const [mentionAnchor, setMentionAnchor] = useState<number>(-1); // index of '@' in input
  const [balancePeek, setBalancePeek] = useState<BalanceResult | null>(null);
  const [balancePeekVisible, setBalancePeekVisible] = useState(false);
  const [historyIdx, setHistoryIdx] = useState<number | null>(null);
  const [showClearConfirm, setShowClearConfirm] = useState(false);
  const [insightsOpen, setInsightsOpen] = useState(false);

  useEffect(() => {
    try {
      window.localStorage.setItem(TTS_STORAGE_KEY, ttsEnabled ? "1" : "0");
    } catch {
      /* ignore quota / private mode */
    }
    if (!ttsEnabled) cancelSpeech();
  }, [ttsEnabled]);

  const pickRecipient = (text: string) => {
    setInput(text);
    setTimeout(() => inputRef.current?.focus(), 0);
  };

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
      setSlashOpen(false);
      setMentionOpen(false);
      setHistoryIdx(null);
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
        // Transfer was executed (or cancelled) — re-rank the suggestion
        // strip so the freshly-paid contact moves up or out.
        if (resp.intent === "transfer") {
          setSuggestRefresh((n) => n + 1);
          // Only count CONFIRMED transfers (not cancellations) toward
          // gating the "Lặp lại lần trước" CTA. The orchestrator
          // returns intent="transfer" for both paths, so we rely on
          // the action label: cancel uses "Huỷ", confirm uses
          // "Xác minh OTP".
          if (!resp.draft && actionLabel !== "Huỷ") {
            setConfirmedTransfers((n) => n + 1);
          }
        }
      }
      resolveOmni(pendingId, resp);
    } catch (e) {
      failOmni(pendingId, e);
    } finally {
      setBusy(false);
    }
  };

  const onConfirm = (draftId: string, otp: string, sourceAccountId?: string) =>
    sendDraftAction(
      () => api.confirm(draftId, otp, sourceAccountId),
      "Xác minh OTP",
      draftId,
    );

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

  // ---------------------------------------------------------------
  // Power-user features wiring
  // ---------------------------------------------------------------

  // Recent user messages, newest first, for the ↑-history cycle.
  const recentUserMessages = useMemo(
    () =>
      messages
        .filter((m) => m.role === "user")
        .map((m) => m.text)
        .reverse(),
    [messages],
  );

  const lastUserMessage = recentUserMessages[0] ?? null;

  // Recompute slash / mention state from the input text on every change.
  // We choose to watch the value rather than parse onKeyDown so paste
  // and IME composition both work.
  const updatePopovers = useCallback((value: string, caret: number) => {
    // Slash palette: only when "/" is the first char.
    if (value.startsWith("/")) {
      // Close once the user types whitespace — that's the args separator.
      const firstWord = value.slice(1).split(/\s/)[0];
      // "lang en" needs two tokens — special-case keep open until newline.
      const probe = value.slice(1).toLowerCase();
      const stillMatching =
        firstWord.length === 0 ||
        [
          "transfer",
          "balance",
          "history",
          "repeat",
          "insights",
          "help",
          "lang",
          "clear",
        ].some((k) => k.startsWith(firstWord.toLowerCase())) ||
        probe.startsWith("lang");
      setSlashOpen(stillMatching);
      setSlashQuery(probe);
    } else {
      setSlashOpen(false);
    }

    // @-mention: look backwards from caret for the last "@".
    const upToCaret = value.slice(0, caret);
    const atIdx = upToCaret.lastIndexOf("@");
    if (atIdx >= 0) {
      // Stop the mention popover if the chunk after @ has whitespace.
      const chunk = upToCaret.slice(atIdx + 1);
      const prevChar = atIdx > 0 ? value[atIdx - 1] : "";
      const wordBoundary = atIdx === 0 || /\s/.test(prevChar);
      if (wordBoundary && !/\s/.test(chunk)) {
        setMentionOpen(true);
        setMentionQuery(chunk);
        setMentionAnchor(atIdx);
        return;
      }
    }
    setMentionOpen(false);
    setMentionAnchor(-1);
  }, []);

  const onInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const value = e.target.value;
    setInput(value);
    setHistoryIdx(null);
    const caret = e.target.selectionStart ?? value.length;
    updatePopovers(value, caret);
  };

  const closeAllModals = (): boolean => {
    if (showClearConfirm) {
      setShowClearConfirm(false);
      return true;
    }
    if (pickerOpen) {
      setPickerOpen(false);
      return true;
    }
    if (slashOpen) {
      setSlashOpen(false);
      return true;
    }
    if (mentionOpen) {
      setMentionOpen(false);
      return true;
    }
    if (balancePeekVisible) {
      setBalancePeekVisible(false);
      return true;
    }
    return false;
  };

  const handleSlashPick = (cmd: SlashCommand, raw: string) => {
    if (cmd.action.kind === "send") {
      const msg = buildMessageFromSlash(cmd, raw) ?? cmd.action.text;
      send(msg);
      return;
    }
    if (cmd.action.kind === "prefill") {
      // Replace the slash command in the input with the prefilled text,
      // preserve any args the user already typed.
      const rest = raw.replace(/^\s*\/\S+\s*/, "");
      const next = cmd.action.text + rest;
      setInput(next);
      setSlashOpen(false);
      setTimeout(() => inputRef.current?.focus(), 0);
      return;
    }
    if (cmd.action.kind === "ui") {
      setSlashOpen(false);
      setInput("");
      if (cmd.action.name === "insights") {
        setInsightsOpen(true);
        // Scroll the sidebar insights into view on small screens.
        document.querySelector(".insights-card")?.scrollIntoView({
          behavior: "smooth",
          block: "center",
        });
      } else if (cmd.action.name === "clear") {
        setShowClearConfirm(true);
      } else if (cmd.action.name === "lang_en") {
        // i18n branch hasn't landed yet — surface a friendly notice.
        appendUser("/lang en");
        setMessages((prev) => [
          ...prev,
          {
            id: newId(),
            role: "omni",
            text: "Chế độ tiếng Anh sẽ có trong bản cập nhật sắp tới. Hiện tại Omni hỗ trợ tiếng Việt.",
          },
        ]);
      }
    }
  };

  const handleMentionPick = (contact: Contact) => {
    if (mentionAnchor < 0) {
      setMentionOpen(false);
      return;
    }
    const before = input.slice(0, mentionAnchor);
    const after = input.slice(mentionAnchor + 1 + mentionQuery.length);
    const next = `${before}${contact.display_name} ${after.trimStart()}`;
    setInput(next);
    setMentionOpen(false);
    setMentionAnchor(-1);
    setTimeout(() => {
      inputRef.current?.focus();
      const pos = (before + contact.display_name + " ").length;
      inputRef.current?.setSelectionRange(pos, pos);
    }, 0);
  };

  const cycleHistory = (dir: "up" | "down") => {
    if (recentUserMessages.length === 0) return;
    const cur = historyIdx ?? -1;
    let next = dir === "up" ? cur + 1 : cur - 1;
    if (next < -1) next = -1;
    if (next >= recentUserMessages.length) next = recentUserMessages.length - 1;
    setHistoryIdx(next);
    setInput(next < 0 ? "" : recentUserMessages[next]);
  };

  const toggleBalancePeek = useCallback(async () => {
    if (balancePeekVisible) {
      setBalancePeekVisible(false);
      return;
    }
    if (!balancePeek) {
      try {
        const b = await api.chat("số dư");
        if (b.balance) setBalancePeek(b.balance);
      } catch {
        /* ignore */
      }
    }
    setBalancePeekVisible(true);
  }, [balancePeek, balancePeekVisible]);

  useKeyboard({
    inputId: INPUT_DOM_ID,
    onFocusInput: () => inputRef.current?.focus(),
    onResendLast: () => {
      if (lastUserMessage && !busy) send(lastUserMessage);
    },
    onToggleBalance: toggleBalancePeek,
    onEscape: closeAllModals,
    onOpenSlash: () => {
      if (!input.startsWith("/")) {
        setInput("/");
        updatePopovers("/", 1);
      } else {
        setSlashOpen(true);
      }
      setTimeout(() => inputRef.current?.focus(), 0);
    },
    onPrevHistory: () => cycleHistory("up"),
    onNextHistory: () => cycleHistory("down"),
    onClearInput: () => {
      setInput("");
      setSlashOpen(false);
      setMentionOpen(false);
    },
    isInputEmpty: () => input.length === 0,
  });

  useEventStream("u_an");

  const confirmClear = () => {
    setMessages([WELCOME]);
    setClosedDraftIds(new Set());
    setClosedScheduleDraftIds(new Set());
    setHistoryIdx(null);
    setShowClearConfirm(false);
  };

  // Per WCAG 4.1.3 (Status Messages), surface "Omni is replying" as a
  // polite live-region announcement. Screen readers will hear it when
  // any reply becomes pending, then hear the resolved text via the
  // chat log (role="log", aria-live="polite" below).
  const pendingReply = messages.some(
    (m) => m.role === "omni" && m.pending === true,
  );

  return (
    <div className="page">
      <div className="phone">
        <ToastStack />
        {/*
          Visually-hidden live region. Announces transient AT-only
          status without affecting layout. The chat log itself is also
          a live region (role="log") for the actual messages.
        */}
        <div className="sr-only" role="status" aria-live="polite">
          {pendingReply ? "Omni đang trả lời" : ""}
        </div>
        <div className="phone__statusbar" aria-hidden="true">9:41</div>
        <header className="phone__header">
          <OmniAvatar size={40} />
          <div className="phone__title">
            <div className="phone__brand">OMNI</div>
            <div className="phone__sub">
              <span className="online-dot" aria-hidden="true" /> Trợ lý đang trực tuyến
            </div>
          </div>
          {ttsSupported && (
            <button
              type="button"
              className={`phone__tts-btn ${ttsEnabled ? "phone__tts-btn--on" : ""}`}
              onClick={() => setTtsEnabled((v) => !v)}
              aria-label={ttsEnabled ? "Tắt giọng đọc" : "Bật giọng đọc"}
              aria-pressed={ttsEnabled}
              title={ttsEnabled ? "Đang đọc to (vi-VN)" : "Đọc to câu trả lời"}
            >
              <span aria-hidden="true">{ttsEnabled ? "🔊" : "🔇"}</span>
            </button>
          )}
          <div className="user-pill" aria-label="Người dùng An">AN</div>
        </header>

        <main
          className="phone__chat"
          ref={scrollRef}
          role="log"
          aria-live="polite"
          aria-relevant="additions text"
          aria-label="Hội thoại với Omni"
        >
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
              onPrefill={(text) => {
                setInput(text);
                setTimeout(() => inputRef.current?.focus(), 0);
              }}
              busy={busy}
              actionableDraftIds={actionableDraftIds}
              actionableScheduleDraftIds={actionableScheduleDraftIds}
              ttsEnabled={ttsEnabled}
            />
          ))}
        </main>

        <SuggestionStrip
          refreshKey={suggestRefresh}
          busy={busy}
          onPick={pickRecipient}
        />

        <RepeatLastCTA
          visible={confirmedTransfers > 0}
          busy={busy}
          onClick={() => send("Lặp lại giao dịch vừa rồi")}
        />

        <div className="phone__input-wrap">
          <SlashPalette
            open={slashOpen}
            query={slashQuery}
            rawInput={input}
            onPick={handleSlashPick}
            onClose={() => setSlashOpen(false)}
          />
          <RecipientAutocomplete
            open={mentionOpen}
            query={mentionQuery}
            onPick={handleMentionPick}
            onClose={() => setMentionOpen(false)}
          />
          {balancePeekVisible && balancePeek && (
            <div className="balance-peek" role="status">
              <div className="balance-peek__label">Số dư khả dụng</div>
              <div className="balance-peek__amount">
                {formatVND(balancePeek.total)}
              </div>
              <button
                type="button"
                className="balance-peek__close"
                onClick={() => setBalancePeekVisible(false)}
                aria-label="Đóng số dư"
              >
                <span aria-hidden="true">×</span>
              </button>
            </div>
          )}
          <form
            className="phone__input"
            role="search"
            aria-label="Trò chuyện với Omni"
            onSubmit={(e) => {
              e.preventDefault();
              send(input);
            }}
          >
            <VoiceButton
              onTranscript={(t) => setInput(t)}
              disabled={busy}
            />
            <button
              type="button"
              className="phone__contacts-btn"
              onClick={() => setPickerOpen(true)}
              aria-label="Mở danh bạ"
              aria-haspopup="dialog"
              title="Danh bạ"
            >
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M3 5h18v14H3z" />
                <path d="M16 3v4" />
                <path d="M8 3v4" />
                <circle cx="12" cy="13" r="2.5" />
                <path d="M8 18c.5-1.5 2-2.5 4-2.5s3.5 1 4 2.5" />
              </svg>
            </button>
            {/*
              The input has no visible <label> by design (chat-app
              convention) — supply an aria-label so AT users get a
              proper field name. Backed by aria-controls/-expanded so
              the slash + mention popovers announce as a combobox.
            */}
            <label htmlFor={INPUT_DOM_ID} className="sr-only">
              Nhập câu lệnh cho Omni
            </label>
            <input
              id={INPUT_DOM_ID}
              ref={inputRef}
              value={input}
              onChange={onInputChange}
              onKeyDown={(e) => {
                // Slash palette / autocomplete intercept Enter, Arrow*,
                // Esc via their own capture-phase listeners. We only
                // reach this branch when neither popover is open.
                if (e.key === "Enter" && !e.shiftKey && !slashOpen && !mentionOpen) {
                  e.preventDefault();
                  send(input);
                }
              }}
              placeholder="Nhập câu lệnh, ví dụ: chuyển cho mẹ 2 triệu… (/ để mở lệnh nhanh, @ để chọn danh bạ)"
              disabled={busy}
              autoComplete="off"
              aria-label="Nhập câu lệnh cho Omni"
              // role="combobox" + aria-* unlocks predictable AT behavior
              // for the slash/mention popovers (per ARIA APG combobox
              // pattern). Only set the listbox/aria-expanded attrs when
              // a popover is actually open so axe doesn't complain that
              // the attribute is unsupported on a plain <input>.
              {...(slashOpen || mentionOpen
                ? {
                    role: "combobox",
                    "aria-autocomplete": "list" as const,
                    "aria-expanded": true,
                    "aria-controls": slashOpen
                      ? "omni-slash-palette"
                      : "omni-mention-list",
                  }
                : {})}
            />
            <button
              type="submit"
              className="btn btn--primary btn--send"
              disabled={busy || !input.trim()}
              aria-label="Gửi câu lệnh"
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M22 2L11 13" />
                <path d="M22 2l-7 20-4-9-9-4 20-7z" />
              </svg>
            </button>
          </form>
        </div>
        <ContactPicker
          open={pickerOpen}
          onClose={() => setPickerOpen(false)}
          onPick={pickRecipient}
        />
        {showClearConfirm && (
          <div
            className="clear-confirm"
            role="dialog"
            aria-modal="true"
            aria-labelledby="clear-confirm-title"
            aria-describedby="clear-confirm-body"
          >
            <div className="clear-confirm__card">
              <div className="clear-confirm__title" id="clear-confirm-title">
                Xoá toàn bộ đoạn chat?
              </div>
              <div className="clear-confirm__body" id="clear-confirm-body">
                Lịch sử hội thoại hiện tại sẽ bị xoá. Hành động này không thể hoàn tác.
              </div>
              <div className="clear-confirm__actions">
                <button
                  className="btn btn--ghost"
                  onClick={() => setShowClearConfirm(false)}
                >
                  Huỷ
                </button>
                <button className="btn btn--warn" onClick={confirmClear} autoFocus>
                  Xoá
                </button>
              </div>
            </div>
          </div>
        )}
      </div>

      <aside className="sidebar" aria-label="Bảng giới thiệu và kịch bản demo">
        <h1 className="sidebar__brand">
          Omni <span>AI Assistant</span>
        </h1>
        <p className="sidebar__lead">
          Ứng dụng xử lý ngôn ngữ tự nhiên trong hoạt động ngân hàng — Team One
          Last Token.
        </p>
        <div className={insightsOpen ? "insights-highlight" : ""}>
          <InsightsCard />
        </div>
        <QuickScenarios onPick={send} />
        <div className="sidebar__legend">
          <div>
            <strong>Pipeline:</strong> Câu lệnh → Hiểu ý định → Trích xuất →
            Ngữ cảnh cá nhân → Kiểm tra an toàn → Thực thi.
          </div>
          <div>
            <strong>Phím tắt:</strong> Cmd/Ctrl+K (focus), Cmd/Ctrl+/ (lệnh),
            Cmd/Ctrl+Enter (gửi lại), Cmd/Ctrl+B (số dư), Esc (đóng), ↑ (lịch sử).
          </div>
          <div>
            <strong>Mock user:</strong> An — số dư tài khoản chính 24.350.000đ.
          </div>
        </div>
      </aside>
    </div>
  );
}
