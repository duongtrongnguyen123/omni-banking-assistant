import type { ChatMessage, Contact, TransactionDraft } from "../types";
import { OmniAvatar } from "./OmniAvatar";
import { TransactionCard } from "./TransactionCard";
import { DisambiguationCard } from "./DisambiguationCard";
import { HistoryCard } from "./HistoryCard";
import { BalanceCard } from "./BalanceCard";
import { ScheduleCard } from "./ScheduleCard";
import { ContactDraftCard } from "./ContactDraftCard";
import { ScheduleDraftCard } from "./ScheduleDraftCard";

interface ConfirmPayload {
  otp?: string;
  biometric_verified?: boolean;
  source_account_id?: string;
}

interface Props {
  message: ChatMessage;
  onConfirm: (
    draftId: string,
    draft: TransactionDraft,
    sourceAccountId?: string,
  ) => void;
  onCancel: (draftId: string) => void;
  onSelectCandidate: (draftId: string, contact: Contact) => void;
  onConfirmContact: (draftId: string) => void;
  onCancelContact: (draftId: string) => void;
  onConfirmSchedule: (draftId: string, otp: string, sourceAccountId?: string) => void;
  onCancelSchedule: (draftId: string) => void;
  busy?: boolean;
  actionableDraftIds?: Set<string>;
  actionableScheduleDraftIds?: Set<string>;
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
  busy,
  actionableDraftIds,
  actionableScheduleDraftIds,
}: Props) => {
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
        <div className="bubble bubble--omni">
          {message.pending ? <span className="typing"><i /><i /><i /></span> : message.text}
        </div>
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
            onConfirm={(sourceAccountId) => onConfirm(r.draft!.id, r.draft!, sourceAccountId)}
            onCancel={() => onCancel(r.draft!.id)}
            disabled={busy}
            actionable={actionableDraftIds?.has(r.draft.id) ?? true}
          />
        )}
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
      </div>
    </div>
  );
};
