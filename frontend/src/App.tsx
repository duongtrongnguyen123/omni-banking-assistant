import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, friendlyApiError } from "./api/client";
import { TOAST_EVENT_NAME, type ToastEvent } from "./hooks/useEventStream";
import type {
  AtmHit,
  BalanceResult,
  BiometricScanResult,
  ChatMessage,
  ChatSession,
  Contact,
  OmniResponse,
  TransactionDraft,
} from "./types";
import { ChatHistory } from "./components/ChatHistory";
import { ContactPicker } from "./components/ContactPicker";
import { BiometricFaceScan } from "./components/BiometricFaceScan";
import { Message } from "./components/Message";
import { InsightsCard } from "./components/InsightsCard";
import { BudgetCard } from "./components/BudgetCard";
import { GoalsCard } from "./components/GoalsCard";
import { OmniAvatar } from "./components/OmniAvatar";
import { QuickScenarios } from "./components/QuickScenarios";
import { SkillsCard } from "./components/SkillsCard";
import { TutorialOverlay } from "./components/TutorialOverlay";
import { VoiceButton, type VoiceButtonHandle } from "./components/VoiceButton";
import { ReceiveCard } from "./components/ReceiveCard";
import { QrScanButton } from "./components/QrScanButton";
import { SuggestionStrip } from "./components/SuggestionStrip";
import { QuickAmountChips } from "./components/QuickAmountChips";
import { RepeatLastCTA } from "./components/RepeatLastCTA";
import { ToastStack } from "./components/ToastStack";
import { TelemetryOverlay, TELEMETRY_EVENT } from "./components/TelemetryOverlay";
import { MetricsCard } from "./components/MetricsCard";
import { HealthStatus } from "./components/HealthStatus";
import { AbTestCard } from "./components/AbTestCard";
import { DemoRecorder } from "./components/DemoRecorder";
import { ExportMenu } from "./components/ExportMenu";
import { PrivacyBadge } from "./components/PrivacyBadge";
import { useEventStream } from "./hooks/useEventStream";
// (TOAST_EVENT_NAME / ToastEvent re-exported above for failOmni's
// top-frame toast.)
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
  // Persisted-conversation history (left drawer).
  const [historyOpen, setHistoryOpen] = useState(false);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  // Mirror of currentSessionId for use inside stable callbacks without
  // re-creating them on every conversation switch.
  const sessionIdRef = useRef<string | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [closedDraftIds, setClosedDraftIds] = useState<Set<string>>(new Set());
  // Drafts whose confirm/cancel HTTP request is in flight. Locks the
  // matching TransactionCard's Huỷ button so user can't fire a cancel
  // that races a confirm — the SAFETY bug from user feedback "nhập opt
  // rồi nhấn huỷ nhưng mà sao vẫn chuyển?". Cleared in the request's
  // finally block.
  const [inFlightDraftIds, setInFlightDraftIds] = useState<Set<string>>(new Set());
  const [closedScheduleDraftIds, setClosedScheduleDraftIds] = useState<Set<string>>(new Set());
  const [pickerOpen, setPickerOpen] = useState(false);
  // Bumped after every executed transfer so the suggestion strip re-ranks.
  const [suggestRefresh, setSuggestRefresh] = useState(0);
  // Counts confirmed transfers this session — gates the "Lặp lại lần
  // trước" CTA so it only appears once there's something to repeat.
  const [confirmedTransfers, setConfirmedTransfers] = useState(0);
  // Last confirmed transfer's amount — used by the "Cùng số tiền, người
  // khác" CTA to prefill the chat input with "chuyển <amount> cho ".
  const [lastConfirmedAmount, setLastConfirmedAmount] = useState<number | null>(null);
  // Transaction auth is handled by a full phone-frame overlay (not inside
  // the card): an OTP step, then — for risky transfers — the 8D face scan.
  // OTP + scan are submitted together to the backend.
  const [pendingAuth, setPendingAuth] = useState<{
    draftId: string;
    draft: TransactionDraft;
    sourceAccountId?: string;
  } | null>(null);
  const [authStage, setAuthStage] = useState<"otp" | "biometric">("otp");
  const [authOtp, setAuthOtp] = useState("");
  const [authOtpError, setAuthOtpError] = useState("");
  const [ttsEnabled, setTtsEnabled] = useState<boolean>(() => readStoredTtsPref());
  const ttsSupported = isSpeechSupported();
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  // Lets `send()` and other code paths stop voice recognition when the
  // user is done dictating (e.g. clicked Gửi), so the mic doesn't keep
  // listening and overwrite the cleared input on the next onresult.
  const voiceRef = useRef<VoiceButtonHandle | null>(null);

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
  const [receiveOpen, setReceiveOpen] = useState(false);
  // Phone-only mode: hide the pitch sidebar so the demo looks like a
  // real banking app instead of a presentation slide. Persisted per
  // browser so judges can toggle once and the choice survives reload.
  const [showSidebar, setShowSidebar] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    try {
      return window.localStorage.getItem("omni.sidebar.visible") === "1";
    } catch {
      return false;
    }
  });
  useEffect(() => {
    try {
      window.localStorage.setItem(
        "omni.sidebar.visible",
        showSidebar ? "1" : "0",
      );
    } catch {
      /* ignore */
    }
  }, [showSidebar]);

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

  // ATM finder — invoked by <AtmFinderButton /> after navigator.geolocation
  // resolves. We synthesise an Omni message so the standard <Message /> ->
  // <AtmCard /> render path picks it up; no extra UI plumbing needed.
  const onAtmsFromButton = useCallback((atms: AtmHit[], note?: string) => {
    const message: ChatMessage = {
      id: newId(),
      role: "omni",
      text:
        note ??
        (atms.length > 0
          ? `Tìm thấy ${atms.length} điểm ATM/chi nhánh quanh bạn.`
          : "Mình chưa tìm được điểm ATM nào — bạn thử mở rộng phạm vi nhé."),
      response: {
        intent: "atm_finder",
        text: "",
        draft: null,
        contact_draft: null,
        schedule_draft: null,
        history: null,
        balance: null,
        schedule: null,
        recurring_patterns: null,
        atms,
        needs_disambiguation: false,
      },
    };
    setMessages((prev) => [...prev, message]);
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
    const friendly = friendlyApiError(err);
    setMessages((prev) =>
      prev.map((m) =>
        m.id === id
          ? {
              ...m,
              text: `Lỗi: ${friendly}`,
              pending: false,
            }
          : m,
      ),
    );
    // Surface the same friendly message as a red top-frame toast so the
    // failure is impossible to miss even if the user has scrolled away.
    try {
      const toast: ToastEvent = {
        kind: "transfer_failed",
        title: "Không gửi được yêu cầu",
        body: friendly,
        severity: "error",
        ts: Date.now(),
      };
      window.dispatchEvent(
        new CustomEvent<ToastEvent>(TOAST_EVENT_NAME, { detail: toast }),
      );
    } catch {
      /* noop — CustomEvent fail is non-fatal */
    }
  };

  // Keep the ref in lockstep with the state so stable callbacks read the
  // live conversation id.
  useEffect(() => {
    sessionIdRef.current = currentSessionId;
  }, [currentSessionId]);

  const refreshSessions = useCallback(async () => {
    try {
      const list = await api.chatSessions();
      setSessions(list);
    } catch {
      /* sidebar is best-effort — never block chat on a list failure */
    }
  }, []);

  const adoptSession = useCallback((id: string | null) => {
    if (id) {
      sessionIdRef.current = id;
      setCurrentSessionId(id);
    }
  }, []);

  const startNewChat = useCallback(() => {
    setMessages([WELCOME]);
    setCurrentSessionId(null);
    sessionIdRef.current = null;
    setClosedDraftIds(new Set());
    setClosedScheduleDraftIds(new Set());
    setHistoryIdx(null);
    setHistoryOpen(false);
  }, []);

  const loadSession = useCallback(async (id: string) => {
    try {
      const { messages: stored } = await api.chatSessionMessages(id);
      const mapped: ChatMessage[] = stored.map((m) => ({
        id: m.id,
        role: m.role,
        text: m.content,
      }));
      setMessages(mapped.length ? mapped : [WELCOME]);
      setCurrentSessionId(id);
      sessionIdRef.current = id;
      setClosedDraftIds(new Set());
      setClosedScheduleDraftIds(new Set());
      setHistoryIdx(null);
      setHistoryOpen(false);
    } catch {
      /* ignore — keep the current view on a load failure */
    }
  }, []);

  const removeSession = useCallback(
    async (id: string) => {
      try {
        await api.deleteChatSession(id);
      } catch {
        /* ignore */
      }
      if (sessionIdRef.current === id) {
        startNewChat();
      }
      void refreshSessions();
    },
    [refreshSessions, startNewChat],
  );

  // On first paint, load the saved conversation list and re-open the most
  // recent one so the user lands back where they left off.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      setSessionsLoading(true);
      try {
        const list = await api.chatSessions();
        if (cancelled) return;
        setSessions(list);
        if (list.length > 0) {
          await loadSession(list[0].id);
        }
      } catch {
        /* offline / fresh DB — stay on the welcome screen */
      } finally {
        if (!cancelled) setSessionsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [loadSession]);

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || busy) return;
      // Release the mic before doing anything else — if the user
      // dictated this message, we don't want recognition to keep
      // running and clobber the now-empty input via a late onresult.
      voiceRef.current?.stop();
      appendUser(trimmed);
      const pendingId = appendOmniPending();
      setInput("");
      setSlashOpen(false);
      setMentionOpen(false);
      setHistoryIdx(null);
      setBusy(true);
      try {
        const { response: resp, sessionId } = await api.chat(
          trimmed,
          sessionIdRef.current,
        );
        adoptSession(sessionId);
        if (resp.draft) {
          setClosedDraftIds((prev) => {
            const next = new Set(prev);
            next.delete(resp.draft!.id);
            return next;
          });
        }
        // Surface telemetry to the dev overlay (no-op if ?dev=1 wasn't set
        // — the backend leaves `telemetry` null in that case).
        if (resp.telemetry) {
          try {
            window.dispatchEvent(
              new CustomEvent(TELEMETRY_EVENT, {
                detail: { telemetry: resp.telemetry },
              }),
            );
          } catch {
            /* noop */
          }
        }
        resolveOmni(pendingId, resp);
        // Reflect the new title / preview / ordering in the sidebar.
        void refreshSessions();
      } catch (e) {
        failOmni(pendingId, e);
      } finally {
        setBusy(false);
      }
    },
    [busy, adoptSession, refreshSessions],
  );

  const sendDraftAction = async (
    action: () => Promise<OmniResponse>,
    actionLabel: string,
    closeDraftId?: string,
  ) => {
    appendUser(actionLabel);
    const pendingId = appendOmniPending();
    setBusy(true);
    if (closeDraftId) {
      setInFlightDraftIds((prev) => {
        const next = new Set(prev);
        next.add(closeDraftId);
        return next;
      });
    }
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
      // Adding a contact via chat ("Lưu Lê Mai STK …" → Lưu danh bạ)
      // doesn't go through the closeDraftId branch above because the
      // contact confirm/cancel call sites don't pass a draft id. Bump
      // the refresh key here so the SuggestionStrip + ContactPicker
      // pick up the freshly-saved contact instead of waiting until the
      // next page load.
      if (resp.intent === "add_contact" && !resp.contact_draft) {
        setSuggestRefresh((n) => n + 1);
      }
      resolveOmni(pendingId, resp);
    } catch (e) {
      failOmni(pendingId, e);
    } finally {
      setBusy(false);
      if (closeDraftId) {
        setInFlightDraftIds((prev) => {
          if (!prev.has(closeDraftId)) return prev;
          const next = new Set(prev);
          next.delete(closeDraftId);
          return next;
        });
      }
    }
  };

  // Submit the confirm request with OTP and (for risky transfers) the 8D
  // face scan together. Drives the chat pending/resolve bubble.
  const runConfirm = async (
    draftId: string,
    otp: string,
    sourceAccountId?: string,
    biometricScan?: BiometricScanResult,
  ) => {
    appendUser(biometricScan ? "Xác minh sinh trắc học" : "Xác minh OTP");
    const pendingId = appendOmniPending();
    setBusy(true);
    try {
      const resp = await api.confirm(draftId, otp, sourceAccountId, biometricScan);
      resolveOmni(pendingId, resp);
      if (!resp.draft) {
        setClosedDraftIds((prev) => new Set(prev).add(draftId));
        if (resp.intent === "transfer") {
          setSuggestRefresh((n) => n + 1);
          setConfirmedTransfers((n) => n + 1);
        }
      }
    } catch (e) {
      failOmni(pendingId, e);
    } finally {
      setBusy(false);
    }
  };

  // Card → open the full-frame auth overlay. OTP first; biometric (if the
  // draft requires it) is a second stage inside the same overlay.
  const onConfirm = (
    draftId: string,
    draft: TransactionDraft,
    sourceAccountId?: string,
  ) => {
    // Capture the amount before the draft disappears so the "Cùng số
    // tiền, người khác" CTA can prefill the chat input after the
    // transfer lands.
    for (const m of messages) {
      if (m.response?.draft?.id === draftId && m.response.draft.amount != null) {
        setLastConfirmedAmount(m.response.draft.amount);
        break;
      }
    }
    const needsOtp = draft.auth_required?.includes("otp") ?? draft.requires_step_up;
    setPendingAuth({ draftId, draft, sourceAccountId });
    setAuthStage(needsOtp ? "otp" : "biometric");
    setAuthOtp("");
    setAuthOtpError("");
  };

  const cleanAuthOtp = authOtp.replace(/\D/g, "").slice(0, 6);

  // OTP stage "Tiếp tục": advance to the face scan if required, else submit.
  const submitOtp = () => {
    if (!pendingAuth) return;
    if (cleanAuthOtp !== "123456") {
      setAuthOtpError("OTP chưa đúng. Nhập mã demo 123456 để tiếp tục.");
      return;
    }
    setAuthOtpError("");
    if (pendingAuth.draft.auth_required?.includes("biometric")) {
      setAuthStage("biometric");
      return;
    }
    const { draftId, sourceAccountId } = pendingAuth;
    const otp = cleanAuthOtp;
    setPendingAuth(null);
    setAuthOtp("");
    runConfirm(draftId, otp, sourceAccountId);
  };

  // Face scan verified → submit OTP + scan together.
  const finishBiometric = (scan: BiometricScanResult) => {
    if (!pendingAuth) return;
    if (cleanAuthOtp !== "123456") {
      setAuthStage("otp");
      setAuthOtpError("OTP chưa đúng. Nhập mã demo 123456 để tiếp tục.");
      return;
    }
    const { draftId, sourceAccountId } = pendingAuth;
    const otp = cleanAuthOtp;
    setPendingAuth(null);
    setAuthOtp("");
    runConfirm(draftId, otp, sourceAccountId, scan);
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
    // If the user wipes the field while voice was filling it, treat
    // that as "I'm done dictating" — release the mic so the next
    // onresult doesn't repopulate the input behind their back.
    if (value === "" && voiceRef.current?.isListening()) {
      voiceRef.current.stop();
    }
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
        const { response: b } = await api.chat("số dư", sessionIdRef.current);
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
    <div className={`page${showSidebar ? "" : " page--phone-only"}`}>
      <button
        type="button"
        className="sidebar-toggle"
        onClick={() => setShowSidebar((v) => !v)}
        aria-pressed={showSidebar}
        title={showSidebar ? "Ẩn thông tin demo" : "Hiện thông tin demo"}
      >
        {showSidebar ? "←" : "i"}
      </button>
      <TelemetryOverlay />
      <MetricsCard />
      <AbTestCard />
      <div className="phone">
        <TutorialOverlay userMessageCount={messages.filter((m) => m.role === "user").length} draftVisible={actionableDraftIds.size > 0} />
        <ToastStack />
        <ChatHistory
          open={historyOpen}
          onClose={() => setHistoryOpen(false)}
          sessions={sessions}
          currentSessionId={currentSessionId}
          loading={sessionsLoading}
          onSelect={loadSession}
          onNew={startNewChat}
          onDelete={removeSession}
        />
        <DemoRecorder />
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
          <button
            type="button"
            className="phone__history-btn"
            onClick={() => setHistoryOpen(true)}
            aria-label="Mở lịch sử trò chuyện"
            aria-haspopup="dialog"
            title="Lịch sử trò chuyện"
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M3 12a9 9 0 1 0 3-6.7L3 8" />
              <path d="M3 4v4h4" />
              <path d="M12 7v5l3 2" />
            </svg>
          </button>
          <OmniAvatar size={40} />
          <div className="phone__title">
            <div className="phone__brand">OMNI</div>
            <div className="phone__sub">
              <span className="online-dot" aria-hidden="true" /> Trợ lý đang trực tuyến
            </div>
          </div>
          <PrivacyBadge />
          <ExportMenu />
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
              onSubmitText={(text) => send(text)}
              onSplitBill={async (amount, description) => {
                // Lightweight picker — collects comma-separated names,
                // resolves them to contact ids by display_name match,
                // then dispatches the split endpoint. Future polish:
                // multi-select modal with checkboxes; this is the
                // demo-friendly minimum.
                const raw = window.prompt(
                  `Chia ${amount.toLocaleString("vi-VN")}đ với ai? ` +
                    `Nhập tên cách nhau bởi dấu phẩy (vd: mẹ, Minh, Hùng)`,
                  "mẹ, Minh, Hùng",
                );
                if (!raw) return;
                const names = raw
                  .split(",")
                  .map((s) => s.trim().toLowerCase())
                  .filter((s) => s.length > 0);
                if (names.length === 0) return;
                try {
                  const contacts = await api.contacts();
                  const matchIds: string[] = [];
                  for (const n of names) {
                    const hit = contacts.find((c) => {
                      const dn = c.display_name.toLowerCase();
                      const label = (c.label || "").toLowerCase();
                      const aliases = (c.aliases || []).map((a) =>
                        a.toLowerCase(),
                      );
                      return (
                        dn.includes(n) ||
                        label.includes(n) ||
                        aliases.some((a) => a.includes(n))
                      );
                    });
                    if (hit) matchIds.push(hit.id);
                  }
                  if (matchIds.length === 0) {
                    alert("Không tìm thấy ai khớp trong danh bạ.");
                    return;
                  }
                  const resp = await api.splitBill(
                    amount,
                    description,
                    matchIds,
                  );
                  // Push the response into the message stream like a
                  // normal chat reply so the user sees the new draft
                  // queue land in chat.
                  setMessages((prev) => [
                    ...prev,
                    {
                      id: `s-${Date.now()}`,
                      role: "user",
                      text: `Chia tiền với ${names.join(", ")}`,
                    },
                    {
                      id: `o-${Date.now() + 1}`,
                      role: "omni",
                      text: resp.text,
                      response: resp,
                    },
                  ]);
                } catch (e) {
                  alert(friendlyApiError(e));
                }
              }}
              onDraftResolved={(resp) => {
                // A budget or goal draft was confirmed/cancelled. Bump
                // the sidebar refresh key so BudgetCard / GoalsCard
                // re-fetch and the new envelope / goal shows up
                // immediately instead of after the next page load.
                if (
                  resp.intent === "set_goal" ||
                  resp.intent === "set_budget"
                ) {
                  setSuggestRefresh((n) => n + 1);
                }
              }}
              busy={busy}
              actionableDraftIds={actionableDraftIds}
              inFlightDraftIds={inFlightDraftIds}
              actionableScheduleDraftIds={actionableScheduleDraftIds}
              ttsEnabled={ttsEnabled}
            />
          ))}
        </main>

        <SuggestionStrip
          refreshKey={suggestRefresh}
          busy={busy}
          onPick={pickRecipient}
          onAtms={onAtmsFromButton}
        />

        <RepeatLastCTA
          visible={confirmedTransfers > 0}
          busy={busy}
          onClick={() => send("Lặp lại giao dịch vừa rồi")}
          onSameAmountDifferentRecipient={
            lastConfirmedAmount != null
              ? () => {
                  setInput(`chuyển ${lastConfirmedAmount} cho `);
                  inputRef.current?.focus();
                }
              : undefined
          }
        />

        <QuickAmountChips
          input={input}
          busy={busy}
          onPick={(amount) => {
            // Append with a leading space when the input doesn't already
            // end on whitespace so "chuyển mẹ" + chip "500k" reads as
            // "chuyển mẹ 500k", not "chuyển mẹ500k".
            const sep = input.length === 0 || input.endsWith(" ") ? "" : " ";
            setInput(input + sep + amount + " ");
            inputRef.current?.focus();
          }}
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
              ref={voiceRef}
              onTranscript={(t) => setInput(t)}
              disabled={busy}
            />
            <QrScanButton
              disabled={busy}
              onPrefill={(text) => {
                setInput(text);
                setTimeout(() => inputRef.current?.focus(), 0);
              }}
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
        <ReceiveCard
          open={receiveOpen}
          onClose={() => setReceiveOpen(false)}
        />
        {pendingAuth && (
          <div
            className={`auth-overlay ${authStage === "biometric" ? "auth-overlay--full" : ""}`}
          >
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
                  onChange={(e) => {
                    setAuthOtp(e.target.value.replace(/\D/g, "").slice(0, 6));
                    setAuthOtpError("");
                  }}
                  inputMode="numeric"
                  maxLength={6}
                  placeholder="••••••"
                  autoFocus
                />
                {authOtpError && (
                  <div className="auth-card__error" role="alert">
                    {authOtpError}
                  </div>
                )}
                <div className="auth-card__actions">
                  <button
                    className="btn btn--ghost"
                    onClick={() => {
                      setPendingAuth(null);
                      setAuthOtp("");
                      setAuthOtpError("");
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
                    {pendingAuth.draft.auth_required?.includes("biometric")
                      ? "Tiếp tục"
                      : "Xác minh & chuyển"}
                  </button>
                </div>
              </div>
            ) : (
              <BiometricFaceScan
                open
                challengeId={`${pendingAuth.draftId}:${cleanAuthOtp || "no-otp"}`}
                onClose={() => {
                  // Closing the bio scan overlay means: cancel the
                  // transfer outright. Race window: a finishBiometric()
                  // call may have already fired runConfirm seconds ago.
                  // Even so, hitting cancel here clears the backend
                  // draft — if the in-flight confirm hadn't reached
                  // _execute_and_record yet, it now refuses ("Không
                  // tìm thấy giao dịch chờ xác nhận"). Belt + braces.
                  const draftId = pendingAuth?.draftId;
                  setPendingAuth(null);
                  setAuthOtp("");
                  setAuthOtpError("");
                  setAuthStage("otp");
                  if (draftId) {
                    onCancel(draftId);
                  }
                }}
                onVerified={(scan) => finishBiometric(scan)}
              />
            )}
          </div>
        )}
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

      {showSidebar && (
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
        <BudgetCard refreshKey={suggestRefresh} />
        <GoalsCard refreshKey={suggestRefresh} />
        <QuickScenarios onPick={send} />
        <SkillsCard onPrefill={pickRecipient} />
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
          <HealthStatus />
        </div>
      </aside>
      )}
    </div>
  );
}
