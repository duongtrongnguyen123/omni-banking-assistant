export type Intent =
  | "transfer"
  | "balance"
  | "history"
  | "schedule"
  | "recurring"
  | "reminder"
  | "add_contact"
  | "smalltalk"
  | "unknown";

export interface ContactDraft {
  id: string;
  display_name: string;
  bank: string;
  account_number: string;
  account_masked: string;
  aliases: string[];
  label: string | null;
  flags: SafetyFlag[];
}

export interface Contact {
  id: string;
  display_name: string;
  bank: string;
  account_number: string;
  account_masked: string;
  aliases: string[];
  label: string | null;
  verified: boolean;
  frequent: boolean;
}

export interface SafetyFlag {
  code: string;
  severity: "info" | "warn" | "block";
  message: string;
}

export interface TransactionDraft {
  id: string;
  recipient: Contact | null;
  candidates: Contact[];
  source_account_id: string | null;
  source_accounts: Account[];
  amount: number | null;
  description: string;
  source_text: string;
  reference_transaction_id: string | null;
  flags: SafetyFlag[];
  requires_step_up: boolean;
  predicted_amount?: boolean;
}

export interface HistoryItem {
  id: string;
  amount: number;
  description: string;
  created_at: string;
  contact: { display_name: string; bank: string; account_masked: string; label: string | null };
}

export interface HistoryResult {
  period: string;
  count: number;
  total: number;
  average: number;
  items: HistoryItem[];
}

export interface Account {
  id: string;
  bank: string;
  number: string;
  balance: number;
  currency: string;
  primary: boolean;
}

export interface BalanceResult {
  display_name: string;
  total: number;
  accounts: Account[];
}

export interface Schedule {
  id: string;
  source_account_id: string | null;
  contact_id: string;
  amount: number;
  description: string;
  cron: string;
  next_run: string;
  active: boolean;
}

export interface ScheduleDraft {
  id: string;
  recipient: Contact;
  source_account_id: string | null;
  source_accounts: Account[];
  amount: number;
  description: string;
  cron: string;
  cron_label: string;
  next_run: string;
  flags: SafetyFlag[];
}

export interface RecurringPattern {
  contact_id: string;
  description: string;
  typical_amount: number;
  typical_day: number;
  occurrence_count: number;
  month_count: number;
  first_seen: string;
  last_seen: string;
  next_run: string;
  confidence: number;
  recipient_name: string | null;
  recipient_bank: string | null;
}

export interface TelemetryPayload {
  nlu_latency_ms?: number;
  nlu_source?: "llm" | "rule";
  intent?: string;
  intent_confidence?: number;
  total_latency_ms?: number;
  safety_flags?: number;
  safety_codes?: string[];
  suggester_ms?: number;
}

export interface OmniResponse {
  intent: Intent;
  text: string;
  draft: TransactionDraft | null;
  contact_draft: ContactDraft | null;
  schedule_draft: ScheduleDraft | null;
  history: HistoryResult | null;
  balance: BalanceResult | null;
  schedule: Schedule | null;
  recurring_patterns: RecurringPattern[] | null;
  needs_disambiguation: boolean;
  telemetry?: TelemetryPayload | null;
}

export interface MoMEntry {
  this: number;
  last: number;
  delta_pct: number;
}

export interface AnomalyItem {
  tx_id: string;
  amount: number;
  contact_name: string;
  z_score: number;
  reason: string;
}

export interface SubscriptionItem {
  contact: string;
  contact_id: string;
  typical_amount: number;
  occurrences: number;
  last_seen: string;
  median_gap_days: number;
}

export interface InsightsSummary {
  mom: Record<string, MoMEntry>;
  anomalies: AnomalyItem[];
  subscriptions: SubscriptionItem[];
  generated_at: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "omni";
  text: string;
  response?: OmniResponse;
  pending?: boolean;
}

export interface RecipientSuggestion {
  contact: Contact;
  score: number;
  reason: string;
}
