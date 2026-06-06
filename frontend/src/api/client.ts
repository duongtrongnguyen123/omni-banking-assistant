import type {
  AtmHit,
  BiometricScanResult,
  BudgetRow,
  Contact,
  InsightsSummary,
  OmniResponse,
  RecipientSuggestion,
  SavingsGoal,
} from "../types";

const HEADERS = { "Content-Type": "application/json", "x-user-id": "u_an" };

// `?dev=1` enables the telemetry overlay path on the backend. We detect
// it once at module load and forward as a query string on each /api/chat
// call so the orchestrator populates `OmniResponse.telemetry`.
const DEV_MODE = (() => {
  if (typeof window === "undefined") return false;
  try {
    return new URLSearchParams(window.location.search).get("dev") === "1";
  } catch {
    return false;
  }
})();

// Vietnamese fallback copy when the server returns a body-less error or
// when fetch itself fails (offline / DNS / CORS). Used by jsonFetch and
// surfaced as a top-frame toast via friendlyApiError().
const VI_NETWORK_DOWN = "Mất kết nối — kiểm tra mạng nhé";
const VI_GENERIC_ERROR = "Mạng tạm trục trặc — thử lại nhé";
const VI_RATE_LIMITED = "Bạn gửi hơi nhanh — chờ chút rồi thử lại nhé";

/**
 * Wraps a fetch-stage Error / HTTP-stage Error with a normalised shape:
 * `status` (0 = network unreachable), `detail` (server-supplied or our
 * Vietnamese fallback). Consumers can pattern-match on `status` to
 * decide between toast variants.
 */
export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(`${status} ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

/** Pick the right Vietnamese error string for a thrown ApiError. */
export function friendlyApiError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 0) return VI_NETWORK_DOWN;
    if (err.status === 429) return err.detail || VI_RATE_LIMITED;
    return err.detail || VI_GENERIC_ERROR;
  }
  if (err instanceof Error && err.message) return err.message;
  return VI_GENERIC_ERROR;
}

async function jsonFetch<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      ...init,
      headers: { ...HEADERS, ...(init?.headers ?? {}) },
    });
  } catch {
    // Browser fetch only rejects on network failure / CORS preflight
    // failure. Surface as status=0 so the caller can show the offline
    // toast instead of "TypeError: failed to fetch".
    throw new ApiError(0, VI_NETWORK_DOWN);
  }
  if (!res.ok) {
    let detail = "";
    try {
      detail = (await res.json()).detail ?? "";
    } catch {
      // Body wasn't JSON — fall back to statusText, then to the generic VN string.
    }
    if (!detail) detail = res.statusText || VI_GENERIC_ERROR;
    throw new ApiError(res.status, detail);
  }
  return res.json();
}

export interface RecordStopResponse {
  recording: boolean;
  turns: number;
  duration_ms: number;
  jsonl: string;
  script: Array<{ ts: string; user: string; omni: OmniResponse }>;
}

export interface ReplayResponse {
  played: number;
  duration_ms: number;
  transcript: Array<{ user: string; omni_text?: string; intent?: string }>;
}

export interface HealthResponse {
  status: string;
  service: string;
  version: string;
  git_sha: string;
  offline_demo: boolean;
  privacy_mode?: "off" | "redact" | "local-only";
}

export const api = {
  chat: (message: string) =>
    jsonFetch<OmniResponse>(DEV_MODE ? "/api/chat?dev=1" : "/api/chat", {
      method: "POST",
      body: JSON.stringify({ message }),
    }),
  health: () => jsonFetch<HealthResponse>("/health"),
  recordStart: () =>
    jsonFetch<{ recording: boolean; started_at: number }>(
      "/api/demo/record/start",
      { method: "POST" },
    ),
  recordStop: () =>
    jsonFetch<RecordStopResponse>("/api/demo/record/stop", { method: "POST" }),
  recordStatus: () =>
    jsonFetch<{ recording: boolean; turns: number }>(
      "/api/demo/record/status",
    ),
  replay: (
    script: Array<{ user: string }>,
    cadenceMs = 800,
  ) =>
    jsonFetch<ReplayResponse>("/api/demo/replay", {
      method: "POST",
      body: JSON.stringify({ script, cadence_ms: cadenceMs }),
    }),
  confirm: (
    draftId: string,
    otp: string,
    sourceAccountId?: string,
    biometricScan?: BiometricScanResult,
  ) =>
    jsonFetch<OmniResponse>(`/api/transactions/${draftId}/confirm`, {
      method: "POST",
      body: JSON.stringify({
        otp,
        source_account_id: sourceAccountId,
        biometric_scan: biometricScan,
      }),
    }),
  splitBill: (
    totalAmount: number,
    description: string,
    recipientIds: string[],
  ) =>
    jsonFetch<OmniResponse>(`/api/transactions/split`, {
      method: "POST",
      body: JSON.stringify({
        total_amount: totalAmount,
        description,
        recipient_ids: recipientIds,
      }),
    }),
  contacts: () =>
    jsonFetch<Array<Contact & { aliases?: string[]; label?: string | null }>>(
      `/api/banking/contacts`,
    ),
  // From origin/main — recent-recipients widget + STT endpoint. Speech
  // backend (app/speech) wires the latter; the former is a banking
  // helper route that already exists.
  recentRecipients: (limit = 10) =>
    jsonFetch<Array<{ contact: Contact; last_amount: number; last_at: string }>>(
      `/api/banking/recent-recipients?limit=${limit}`,
    ),
  stt: async (blob: Blob) => {
    const form = new FormData();
    form.append("audio", blob, "audio.webm");
    const res = await fetch("/api/speech/stt", {
      method: "POST",
      body: form,
      headers: { "x-user-id": "u_an" },
    });
    if (!res.ok) throw new ApiError(res.status, res.statusText || "STT failed");
    const data = (await res.json()) as { text: string };
    return data.text;
  },
  cancel: (draftId: string) =>
    jsonFetch<OmniResponse>(`/api/transactions/${draftId}/cancel`, {
      method: "POST",
    }),
  select: (draftId: string, contactId: string) =>
    jsonFetch<OmniResponse>(`/api/transactions/${draftId}/select`, {
      method: "POST",
      body: JSON.stringify({ contact_id: contactId }),
    }),
  confirmContact: (draftId: string) =>
    jsonFetch<OmniResponse>(`/api/contacts/${draftId}/confirm`, {
      method: "POST",
    }),
  cancelContact: (draftId: string) =>
    jsonFetch<OmniResponse>(`/api/contacts/${draftId}/cancel`, {
      method: "POST",
    }),
  confirmSchedule: (draftId: string, otp: string, sourceAccountId?: string) =>
    jsonFetch<OmniResponse>(`/api/schedules/${draftId}/confirm`, {
      method: "POST",
      body: JSON.stringify({ otp, source_account_id: sourceAccountId }),
    }),
  cancelSchedule: (draftId: string) =>
    jsonFetch<OmniResponse>(`/api/schedules/${draftId}/cancel`, {
      method: "POST",
    }),
  suggestions: (limit = 5) =>
    jsonFetch<RecipientSuggestion[]>(
      `/api/suggestions/recipients?limit=${limit}`,
    ),
  rankedContacts: () =>
    jsonFetch<RecipientSuggestion[]>(
      `/api/suggestions/recipients?all=true&limit=200`,
    ),
  insights: () => jsonFetch<InsightsSummary>("/api/insights/summary"),
  budgets: () => jsonFetch<BudgetRow[]>("/api/budgets"),
  createBudget: (category: string, monthlyLimitVnd: number) =>
    jsonFetch<BudgetRow>("/api/budgets", {
      method: "POST",
      body: JSON.stringify({
        category,
        monthly_limit_vnd: monthlyLimitVnd,
      }),
    }),
  updateBudget: (budgetId: string, monthlyLimitVnd: number) =>
    jsonFetch<BudgetRow>(`/api/budgets/${budgetId}`, {
      method: "PUT",
      body: JSON.stringify({ monthly_limit_vnd: monthlyLimitVnd }),
    }),
  deleteBudget: (budgetId: string) =>
    jsonFetch<{ ok: boolean }>(`/api/budgets/${budgetId}`, { method: "DELETE" }),
  confirmBudget: (draftId: string) =>
    jsonFetch<OmniResponse>(`/api/budgets/${draftId}/confirm`, {
      method: "POST",
    }),
  cancelBudget: (draftId: string) =>
    jsonFetch<OmniResponse>(`/api/budgets/${draftId}/cancel`, {
      method: "POST",
    }),
  goals: () => jsonFetch<SavingsGoal[]>("/api/goals"),
  createGoal: (name: string, targetVnd: number, deadline?: string) =>
    jsonFetch<SavingsGoal>("/api/goals", {
      method: "POST",
      body: JSON.stringify({
        name,
        target_vnd: targetVnd,
        deadline: deadline ?? null,
      }),
    }),
  contributeGoal: (goalId: string, amount: number) =>
    jsonFetch<SavingsGoal>(`/api/goals/${goalId}/contribute`, {
      method: "POST",
      body: JSON.stringify({ amount }),
    }),
  confirmGoal: (draftId: string) =>
    jsonFetch<OmniResponse>(`/api/goals/${draftId}/confirm`, {
      method: "POST",
    }),
  cancelGoal: (draftId: string) =>
    jsonFetch<OmniResponse>(`/api/goals/${draftId}/cancel`, {
      method: "POST",
    }),
  atmsNearby: (
    lat: number,
    lng: number,
    radiusKm = 2,
    bank?: string,
  ) => {
    const params = new URLSearchParams({
      lat: String(lat),
      lng: String(lng),
      radius_km: String(radiusKm),
    });
    if (bank) params.set("bank", bank);
    return jsonFetch<AtmHit[]>(`/api/atm/nearby?${params.toString()}`);
  },
  atmsByBank: (bank: string) =>
    jsonFetch<AtmHit[]>(
      `/api/atm/by-bank/${encodeURIComponent(bank)}`,
    ),
  qrGenerate: (body: {
    bank: string;
    account_number: string;
    amount?: number | null;
    message?: string | null;
  }) =>
    jsonFetch<{ qr_base64: string; payload_text: string }>(
      "/api/qr/generate",
      {
        method: "POST",
        body: JSON.stringify({
          bank: body.bank,
          account_number: body.account_number,
          amount: body.amount ?? null,
          message: body.message ?? null,
        }),
      },
    ),
  qrDecode: (payloadText: string) =>
    jsonFetch<{
      bank: string;
      account_number: string;
      amount: number | null;
      message: string | null;
    }>("/api/qr/decode", {
      method: "POST",
      body: JSON.stringify({ payload_text: payloadText }),
    }),
  me: () =>
    jsonFetch<{
      id: string;
      display_name: string;
      phone: string;
      accounts: Array<{
        id: string;
        bank: string;
        number: string;
        balance: number;
        currency: string;
        primary: boolean;
      }>;
    }>("/api/me"),
};
