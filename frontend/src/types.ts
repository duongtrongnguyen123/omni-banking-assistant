export type Intent =
  | "transfer"
  | "balance"
  | "history"
  | "schedule"
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
  auth_required: Array<"otp" | "biometric">;
  auth_completed: Array<"otp" | "biometric">;
}

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

export interface OmniResponse {
  intent: Intent;
  text: string;
  draft: TransactionDraft | null;
  contact_draft: ContactDraft | null;
  schedule_draft: ScheduleDraft | null;
  history: HistoryResult | null;
  balance: BalanceResult | null;
  schedule: Schedule | null;
  needs_disambiguation: boolean;
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
