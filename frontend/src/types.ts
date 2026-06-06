export type Intent =
  | "transfer"
  | "balance"
  | "history"
  | "schedule"
  | "recurring"
  | "reminder"
  | "add_contact"
  | "set_budget"
  | "set_goal"
  | "budget_status"
  | "atm_finder"
  // Proactive analytics — backend ships this for "có gì bất thường",
  // "so với tháng trước", "phân tích chi tiêu". The chat reply is
  // text-only (no structured card) but exhaustive switches on Intent
  // need to know about it.
  | "insights"
  | "smalltalk"
  | "unknown";

export interface AtmHit {
  id: string;
  bank: string;
  name: string;
  address: string;
  lat: number;
  lng: number;
  hours: string;
  // Only present on /api/atm/nearby responses.
  distance_km?: number;
}

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
  details?: AnomalyDetails | Record<string, unknown> | null;
}

export interface AnomalyDetails {
  kind: "per_recipient";
  recipient_name: string;
  median: number;
  p90: number;
  n_samples: number;
  ratio: number;
  current_amount: number;
}

export type AuthMethod = "otp" | "biometric";

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
  amount_prediction_reason?: string | null;
  amount_prediction_confidence?: number | null;
  recent_to_recipient?: Array<{
    amount: number;
    created_at: string;
    description: string;
    category?: string | null;
  }> | null;
  category?: string | null;
  // Biometric auth additions from origin/main. Optional so existing
  // draft producers don't need to change.
  auth_required?: Array<"otp" | "biometric">;
  auth_completed?: Array<"otp" | "biometric">;
}

// Biometric scan types — added on origin/main for the face-scan flow.
// Kept exported even though the integration TransactionCard doesn't
// render them yet; the next merge wave wires the UI.
export type BiometricScanTarget = "center" | "sideA" | "verticalA" | "sideB";
export type BiometricScanPath = "clockwise" | "counterClockwise";

export interface BiometricScanPose {
  yaw: number;
  pitch: number;
  roll: number;
  faceCenterX: number;
  faceCenterY: number;
}

export interface BiometricScanStepResult {
  index: number;
  target: BiometricScanTarget;
  stableFrames: number;
  detectionScore: number;
  elapsedMs: number;
  pose: BiometricScanPose;
  frameSignature: number;
}

export interface BiometricScanSample {
  elapsedMs: number;
  detectionScore: number;
  pose: BiometricScanPose;
  frameSignature: number;
}

export interface BiometricScanResult {
  challengeId: string;
  path: BiometricScanPath;
  requiredStableFrames: number;
  startedAt: string;
  finishedAt: string;
  continuityBreaks: number;
  faceDescriptor: number[];
  profileDescriptors: number[][];
  samples: BiometricScanSample[];
  steps: BiometricScanStepResult[];
}

export interface HistoryItem {
  id: string;
  amount: number;
  description: string;
  created_at: string;
  category?: string | null;
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
  /** Oldest → newest 7-day spending series (one cell per day). */
  recent_outflow_7d?: number[];
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

export interface BudgetDraft {
  id: string;
  category: string;
  category_label: string;
  monthly_limit_vnd: number;
  replaces_existing: boolean;
  flags: SafetyFlag[];
}

export interface GoalDraft {
  id: string;
  name: string;
  target_vnd: number;
  deadline: string | null;
  flags: SafetyFlag[];
}

export interface BudgetStatus {
  category: string;
  category_label: string;
  monthly_limit_vnd: number;
  spent_vnd: number;
  remaining_vnd: number;
  ratio: number;
}

export interface BudgetRow extends BudgetStatus {
  id: string;
  user_id: string;
  created_at: string;
}

export interface SavingsGoal {
  id: string;
  user_id: string;
  name: string;
  target_vnd: number;
  current_vnd: number;
  deadline: string | null;
  created_at: string;
}

export interface OmniResponse {
  intent: Intent;
  text: string;
  draft: TransactionDraft | null;
  contact_draft: ContactDraft | null;
  schedule_draft: ScheduleDraft | null;
  budget_draft?: BudgetDraft | null;
  goal_draft?: GoalDraft | null;
  history: HistoryResult | null;
  balance: BalanceResult | null;
  schedule: Schedule | null;
  recurring_patterns: RecurringPattern[] | null;
  budget_statuses?: BudgetStatus[] | null;
  atms?: AtmHit[] | null;
  help_sections?: HelpSectionPayload[] | null;
  needs_disambiguation: boolean;
  telemetry?: TelemetryPayload | null;
}

export interface HelpSectionPayload {
  id: string;
  title: string;
  items?: { label: string; example: string }[];
  shortcuts?: { keys: string; label: string }[];
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

export interface RecentRecipient {
  contact: {
    id: string;
    display_name: string;
    bank: string;
    account_masked: string;
    label: string | null;
  };
  last_at: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "omni";
  text: string;
  response?: OmniResponse;
  pending?: boolean;
}

/** A saved conversation as listed in the left-hand history sidebar. */
export interface ChatSession {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  preview: string;
}

/** One persisted turn, as returned when re-opening a conversation. */
export interface StoredChatMessage {
  id: string;
  role: "user" | "omni";
  content: string;
  intent: string | null;
  created_at: string;
}

export interface RecipientSuggestion {
  contact: Contact;
  score: number;
  reason: string;
}
