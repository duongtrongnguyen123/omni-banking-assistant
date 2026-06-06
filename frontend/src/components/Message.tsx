import { useEffect, useRef } from "react";
import type { ChatMessage, Contact, OmniResponse } from "../types";
import { OmniAvatar } from "./OmniAvatar";
import { TransactionCard } from "./TransactionCard";
import { DisambiguationCard } from "./DisambiguationCard";
import { HistoryCard } from "./HistoryCard";
import { BalanceCard } from "./BalanceCard";
import { ScheduleCard } from "./ScheduleCard";
import { ContactDraftCard } from "./ContactDraftCard";
import { ScheduleDraftCard } from "./ScheduleDraftCard";
import { BudgetDraftCard, GoalDraftCard } from "./BudgetDraftCard";
import { RecurringList } from "./RecurringList";
import { AtmCard } from "./AtmCard";
import { HelpCard } from "./HelpCard";
import { speak } from "../lib/tts";

interface Props {
  message: ChatMessage;
  onConfirm: (draftId: string, otp: string, sourceAccountId?: string) => void;
  onCancel: (draftId: string) => void;
  onSelectCandidate: (draftId: string, contact: Contact) => void;
  onConfirmContact: (draftId: string) => void;
  onCancelContact: (draftId: string) => void;
  onConfirmSchedule: (draftId: string, otp: string, sourceAccountId?: string) => void;
  onCancelSchedule: (draftId: string) => void;
  onPrefill?: (text: string) => void;
  /** Fire-and-forget send — bypasses the input box. Used by the inline
   *  edit-amount UI on TransactionCard so the user gets one-tap edits
   *  without a typing detour. */
  onSubmitText?: (text: string) => void;
  /** Open the split-bill picker — called from the just-confirmed receipt
   *  card. App.tsx implements the contact picker + POST. */
  onSplitBill?: (amount: number, description: string) => void;
  /**
   * Notify the parent that a budget/goal draft was confirmed (or
   * cancelled) so it can refresh the sidebar BudgetCard / GoalsCard
   * that fetch from REST. Without this the user creates a goal in
   * chat and the sidebar widget stays empty until the next page
   * load — visible "where's my goal" demo bug.
   */
  onDraftResolved?: (resp: OmniResponse) => void;
  busy?: boolean;
  actionableDraftIds?: Set<string>;
  /** Drafts whose confirm/cancel request is currently in flight.
   *  TransactionCard locks both buttons + shows a spinner so the user
   *  can't fire a cancel that races a confirm. */
  inFlightDraftIds?: Set<string>;
  actionableScheduleDraftIds?: Set<string>;
  ttsEnabled?: boolean;
}

export const Message = ({
  message,
  onConfirm,
  onCancel,
  onSelectCandidate,
  onConfirmContact,
  onCancelContact,
  onConfirmSchedule,
  onCancelSchedule,
  onPrefill,
  onSubmitText,
  onSplitBill,
  onDraftResolved,
  busy,
  actionableDraftIds,
  inFlightDraftIds,
  actionableScheduleDraftIds,
  ttsEnabled,
}: Props) => {
  // Only speak when this is a FRESH Omni reply. We detect freshness by
  // watching the pending → resolved transition: every real reply starts
  // as `pending: true`, then flips to `false` with text. The WELCOME and
  // any history-style replays never carry a `pending` flag at all, so
  // they're naturally skipped.
  const spokenRef = useRef(false);
  const wasPendingRef = useRef(message.pending === true);
  useEffect(() => {
    if (!ttsEnabled) return;
    if (message.role !== "omni") return;
    if (spokenRef.current) return;
    const justResolved =
      wasPendingRef.current && !message.pending && !!message.text;
    if (!justResolved) {
      wasPendingRef.current = message.pending === true;
      return;
    }
    spokenRef.current = true;
    speak(message.text);
  }, [ttsEnabled, message.role, message.pending, message.text]);

  if (message.role === "user") {
    return (
      <div className="msg msg--user">
        <div className="bubble bubble--user">{message.text}</div>
      </div>
    );
  }

  const r = message.response;
  return (
    <div className="msg msg--omni">
      <OmniAvatar />
      <div className="msg__stack">
        {r?.recurring_patterns && r.recurring_patterns.length > 0 && (
          <RecurringList
            patterns={r.recurring_patterns}
            onSchedule={(text) => onPrefill?.(text)}
          />
        )}
        <div className="bubble bubble--omni">
          {message.pending ? <span className="typing"><i /><i /><i /></span> : message.text}
        </div>
        {r?.help_sections && r.help_sections.length > 0 && (
          <HelpCard
            sections={r.help_sections}
            onPrefill={(text) => onPrefill?.(text)}
          />
        )}
        {r?.draft && r.draft.candidates.length > 0 && r.draft.recipient === null && (
          <DisambiguationCard
            draft={r.draft}
            onSelect={(c) => onSelectCandidate(r.draft!.id, c)}
            disabled={busy}
          />
        )}
        {r?.draft && r.draft.recipient && (
          <TransactionCard
            draft={r.draft}
            onConfirm={(otp, sourceAccountId) => onConfirm(r.draft!.id, otp, sourceAccountId)}
            onCancel={() => onCancel(r.draft!.id)}
            onEdit={
              onPrefill
                ? () => onPrefill("đổi sang ")
                : undefined
            }
            onModifyAmount={
              onSubmitText
                ? (amount: number) => onSubmitText(`đổi sang ${amount}`)
                : undefined
            }
            onSplitBill={onSplitBill}
            disabled={busy}
            inFlight={inFlightDraftIds?.has(r.draft.id) ?? false}
            actionable={actionableDraftIds?.has(r.draft.id) ?? true}
          />
        )}
        {r?.atms && r.atms.length > 0 && <AtmCard atms={r.atms} />}
        {r?.history && <HistoryCard history={r.history} />}
        {r?.balance && <BalanceCard balance={r.balance} />}
        {r?.schedule && <ScheduleCard schedule={r.schedule} />}
        {r?.contact_draft && (
          <ContactDraftCard
            draft={r.contact_draft}
            onConfirm={() => onConfirmContact(r.contact_draft!.id)}
            onCancel={() => onCancelContact(r.contact_draft!.id)}
            disabled={busy}
          />
        )}
        {r?.schedule_draft && (
          <ScheduleDraftCard
            draft={r.schedule_draft}
            onConfirm={(otp, sourceAccountId) =>
              onConfirmSchedule(r.schedule_draft!.id, otp, sourceAccountId)
            }
            onCancel={() => onCancelSchedule(r.schedule_draft!.id)}
            disabled={busy}
            actionable={actionableScheduleDraftIds?.has(r.schedule_draft.id) ?? true}
          />
        )}
        {r?.budget_draft && (
          <BudgetDraftCard
            draft={r.budget_draft}
            onResolve={(resp) => {
              // Mutate the message in place — the App-level state
              // already references this object, so updating ``response``
              // would also propagate. We keep the simpler approach of
              // letting the user re-trigger via chat keyword if they
              // want a second action.
              message.response = resp;
              onDraftResolved?.(resp);
            }}
            busy={busy}
          />
        )}
        {r?.goal_draft && (
          <GoalDraftCard
            draft={r.goal_draft}
            onResolve={(resp) => {
              message.response = resp;
              onDraftResolved?.(resp);
            }}
            busy={busy}
          />
        )}
      </div>
    </div>
  );
};
