import type {
  BudgetRow,
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

async function jsonFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { ...HEADERS, ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let detail = "";
    try {
      detail = (await res.json()).detail ?? "";
    } catch {
      // ignore
    }
    throw new Error(`${res.status} ${detail || res.statusText}`);
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
  confirm: (draftId: string, otp: string, sourceAccountId?: string) =>
    jsonFetch<OmniResponse>(`/api/transactions/${draftId}/confirm`, {
      method: "POST",
      body: JSON.stringify({ otp, source_account_id: sourceAccountId }),
    }),
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
};
