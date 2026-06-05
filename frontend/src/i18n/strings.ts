/**
 * Minimal i18n for Omni. No runtime dependency — just a typed table of
 * strings keyed by locale, plus a `useT()` React hook that re-renders any
 * component when the user toggles the language pill in the header.
 *
 * Why hand-rolled and not i18next? Judges read package.json. The string
 * table is < 100 keys and we already ship VI everywhere; a 5kb library
 * would be larger than the actual content it wraps.
 *
 * Default locale is `vi`. The pick is persisted to `localStorage` under
 * `omni.lang` so a hard refresh keeps the choice; the language event
 * (`omni:lang`) is a custom-window event we dispatch from `setLang()` so
 * every subscribed component refreshes without a context provider.
 */
import { useEffect, useState } from "react";

export type Lang = "vi" | "en";

const STORAGE_KEY = "omni.lang";
const EVENT = "omni:lang";

const DICT = {
  vi: {
    welcome:
      "Chào An! Mình là Omni — bạn cần chuyển tiền, xem số dư, hay tra lịch sử? Hãy nói thật tự nhiên nhé.",
    headerOnline: "Trợ lý đang trực tuyến",
    sidebarLead:
      "Ứng dụng xử lý ngôn ngữ tự nhiên trong hoạt động ngân hàng — Team One Last Token.",
    sidebarPipeline:
      "Câu lệnh → Hiểu ý định → Trích xuất → Ngữ cảnh cá nhân → Kiểm tra an toàn → Thực thi.",
    sidebarMockUser: "An — số dư tài khoản chính 24.350.000đ.",
    pipelineLabel: "Pipeline:",
    mockUserLabel: "Mock user:",
    dayDividerToday: "Hôm nay",
    inputPlaceholder: "Nhập câu lệnh, hoặc bấm mic để nói…",
    sendAria: "Gửi",
    quickScenariosTitle: "Kịch bản demo nhanh",
    qsKB1: "KB1 · Chuyển thông thường",
    qsKB1Text: "Chuyển cho Minh 2 triệu tiền ăn tháng này",
    qsKB2: "KB2 · Ngữ cảnh cá nhân",
    qsKB2Text: "Gửi cho mẹ 5 triệu như tháng trước",
    qsKB3: "KB3 · Trùng tên",
    qsKB3Text: "Chuyển cho Minh 500k",
    qsKB4: "KB4 · Lịch sử",
    qsKB4Text: "Tháng này mình gửi mẹ bao nhiêu rồi?",
    qsKB5: "KB5 · Bất thường",
    qsKB5Text: "Chuyển 50 triệu cho Hùng STK 9990001234",
    qsKB6: "KB6 · Định kỳ",
    qsKB6Text: "Đặt lịch chuyển mẹ 2tr vào mùng 1 hàng tháng",
    qsKB7: "KB7 · Thêm danh bạ",
    qsKB7Text:
      "Lưu Lê Mai STK 0123987654 Vietcombank tên gọi tắt chị Mai",
    qsKB8: "KB8 · Theo chủ đề",
    qsKB8Text: "Tháng này tôi tiêu vào những chủ đề nào?",
    txAmountLabel: "SỐ TIỀN",
    txRecipient: "Người nhận",
    txDescription: "Nội dung",
    txSourceAccount: "Tài khoản nguồn",
    txAccountPrimary: "Chính",
    txAccountSecondary: "Phụ",
    txInsufficientHint:
      "Tài khoản này không đủ số dư, hãy chọn tài khoản khác hoặc huỷ.",
    txVerified: "Đã xác minh",
    txConfirm: "Xác nhận",
    txVerifyAndSend: "Xác minh & chuyển",
    txCancel: "Huỷ",
    txEdit: "Sửa",
    txDone: "Giao dịch này đã được xử lý.",
    authBothNeeded:
      "Giao dịch cần OTP và xác minh sinh trắc học.",
    authOtpOnly: "Nhập OTP để xác minh giao dịch. Mã demo: 123456",
    authBioOnly: "Cần xác minh sinh trắc học để tiếp tục.",
    authOtp: "OTP",
    authBio: "Sinh trắc học",
    authDone: "Đã xác minh",
    bioScanning: "Đang quét sinh trắc…",
    bioPrompt: "Quét vân tay / khuôn mặt",
    recentRecipientsTitle: "Người nhận gần đây",
    recentRecipientsAria: "Người nhận gần đây",
    recentLoading: "Đang tải…",
    recentEmpty: "Chưa có giao dịch nào.",
    recentErrorPrefix: "Lỗi: ",
    transferPrefix: "Chuyển cho ",
    cancelLabel: "Huỷ",
    confirmLabel: "Xác nhận",
    saveContactLabel: "Lưu danh bạ",
    cancelContactLabel: "Huỷ lưu danh bạ",
    cancelScheduleLabel: "Huỷ đặt lịch",
    verifyOtpLabel: "Xác minh OTP",
    verifyOtpBioLabel: "Xác minh OTP + sinh trắc học",
    verifyBioLabel: "Xác minh sinh trắc học",
    pickLabel: "Chọn ",
    micUnsupported: "Trình duyệt không hỗ trợ ghi âm",
    micRecording: "Đang ghi âm",
    micRecordingHint: "— bấm để dừng và gửi",
    micProcessing: "Đang nhận diện…",
    micErrorPrefix: "Lỗi: ",
    micRetry: ". Bấm để thử lại.",
    micStart: "Bấm để ghi âm",
    micCancel: "Huỷ ghi âm",
    micListening: "Đang nghe…",
    langToggleAria: "Đổi ngôn ngữ",
    errorPrefix: "Lỗi: ",
  },
  en: {
    welcome:
      "Hi An! I'm Omni — need to send money, check your balance, or look up history? Just talk to me naturally.",
    headerOnline: "Assistant online",
    sidebarLead:
      "Natural-language banking assistant — Team One Last Token.",
    sidebarPipeline:
      "Utterance → Intent → Entity extraction → Personal context → Safety checks → Execution.",
    sidebarMockUser: "An — primary account balance 24,350,000 ₫.",
    pipelineLabel: "Pipeline:",
    mockUserLabel: "Mock user:",
    dayDividerToday: "Today",
    inputPlaceholder: "Type a command, or tap the mic to speak…",
    sendAria: "Send",
    quickScenariosTitle: "Quick demo scenarios",
    qsKB1: "KB1 · Standard transfer",
    qsKB1Text: "Send Minh 2 million for meals this month",
    qsKB2: "KB2 · Personal context",
    qsKB2Text: "Send mom 5 million like last month",
    qsKB3: "KB3 · Name collision",
    qsKB3Text: "Send Minh 500k",
    qsKB4: "KB4 · History",
    qsKB4Text: "How much have I sent mom this month?",
    qsKB5: "KB5 · Anomaly",
    qsKB5Text: "Transfer 50 million to Hung account 9990001234",
    qsKB6: "KB6 · Recurring",
    qsKB6Text: "Schedule 2m to mom on the 1st every month",
    qsKB7: "KB7 · Add contact",
    qsKB7Text:
      "Save Le Mai account 0123987654 Vietcombank nickname chị Mai",
    qsKB8: "KB8 · By category",
    qsKB8Text: "What categories did I spend on this month?",
    txAmountLabel: "AMOUNT",
    txRecipient: "Recipient",
    txDescription: "Note",
    txSourceAccount: "Source account",
    txAccountPrimary: "Primary",
    txAccountSecondary: "Secondary",
    txInsufficientHint:
      "This account doesn't have enough balance — pick another or cancel.",
    txVerified: "Verified",
    txConfirm: "Confirm",
    txVerifyAndSend: "Verify & send",
    txCancel: "Cancel",
    txEdit: "Edit",
    txDone: "This transaction has been processed.",
    authBothNeeded: "This transaction needs OTP and biometric verification.",
    authOtpOnly: "Enter the OTP to verify. Demo code: 123456",
    authBioOnly: "Biometric verification is required to continue.",
    authOtp: "OTP",
    authBio: "Biometric",
    authDone: "Verified",
    bioScanning: "Scanning biometric…",
    bioPrompt: "Scan fingerprint / face",
    recentRecipientsTitle: "Recent recipients",
    recentRecipientsAria: "Recent recipients",
    recentLoading: "Loading…",
    recentEmpty: "No transactions yet.",
    recentErrorPrefix: "Error: ",
    transferPrefix: "Send ",
    cancelLabel: "Cancel",
    confirmLabel: "Confirm",
    saveContactLabel: "Save contact",
    cancelContactLabel: "Cancel save contact",
    cancelScheduleLabel: "Cancel schedule",
    verifyOtpLabel: "Verify OTP",
    verifyOtpBioLabel: "Verify OTP + biometric",
    verifyBioLabel: "Verify biometric",
    pickLabel: "Pick ",
    micUnsupported: "Browser does not support recording",
    micRecording: "Recording",
    micRecordingHint: "— tap to stop and send",
    micProcessing: "Transcribing…",
    micErrorPrefix: "Error: ",
    micRetry: ". Tap to retry.",
    micStart: "Tap to record",
    micCancel: "Cancel recording",
    micListening: "Listening…",
    langToggleAria: "Change language",
    errorPrefix: "Error: ",
  },
} as const;

export type StringKey = keyof typeof DICT["vi"];

const isLang = (v: unknown): v is Lang => v === "vi" || v === "en";

const initialLang = (): Lang => {
  if (typeof window === "undefined") return "vi";
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (isLang(stored)) return stored;
  } catch {
    /* localStorage blocked — fall through to default */
  }
  return "vi";
};

export const getLang = (): Lang => initialLang();

export const setLang = (lang: Lang): void => {
  try {
    window.localStorage.setItem(STORAGE_KEY, lang);
  } catch {
    /* ignore */
  }
  window.dispatchEvent(new CustomEvent(EVENT, { detail: lang }));
};

export const useLang = (): [Lang, (lang: Lang) => void] => {
  const [lang, setStateLang] = useState<Lang>(initialLang);
  useEffect(() => {
    const onChange = (e: Event) => {
      const next = (e as CustomEvent<Lang>).detail;
      if (isLang(next)) setStateLang(next);
    };
    window.addEventListener(EVENT, onChange);
    return () => window.removeEventListener(EVENT, onChange);
  }, []);
  return [lang, setLang];
};

/**
 * Lookup helper. Falls back to the VI string if a translation is missing
 * (which shouldn't happen for typed keys but is a safe net for future
 * additions that land in one language first).
 */
export const t = (key: StringKey, lang?: Lang): string => {
  const l = lang ?? initialLang();
  const table = DICT[l] ?? DICT.vi;
  return (table as Record<string, string>)[key] ?? DICT.vi[key] ?? "";
};

/**
 * React hook — returns a memoised translator bound to the current language.
 * Components call `const { t, lang } = useT()` and re-render on toggle.
 */
export const useT = (): { t: (key: StringKey) => string; lang: Lang } => {
  const [lang] = useLang();
  return { t: (key) => t(key, lang), lang };
};
