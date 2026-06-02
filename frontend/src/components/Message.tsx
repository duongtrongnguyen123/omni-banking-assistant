import type { ChatMessage, Contact } from "../types";
import { OmniAvatar } from "./OmniAvatar";
import { TransactionCard } from "./TransactionCard";
import { DisambiguationCard } from "./DisambiguationCard";
import { HistoryCard } from "./HistoryCard";
import { BalanceCard } from "./BalanceCard";
import { ScheduleCard } from "./ScheduleCard";
import { ContactDraftCard } from "./ContactDraftCard";

interface Props {
  message: ChatMessage;
  onConfirm: (draftId: string) => void;
  onCancel: (draftId: string) => void;
  onSelectCandidate: (draftId: string, contact: Contact) => void;
  onConfirmContact: (draftId: string) => void;
  onCancelContact: (draftId: string) => void;
  busy?: boolean;
}

export const Message = ({
  message,
  onConfirm,
  onCancel,
  onSelectCandidate,
  onConfirmContact,
  onCancelContact,
  busy,
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
            onConfirm={() => onConfirm(r.draft!.id)}
            onCancel={() => onCancel(r.draft!.id)}
            disabled={busy}
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
      </div>
    </div>
  );
};
