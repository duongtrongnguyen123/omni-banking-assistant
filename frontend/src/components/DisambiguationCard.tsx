import type { Contact, TransactionDraft } from "../types";

interface Props {
  draft: TransactionDraft;
  onSelect: (contact: Contact) => void;
  disabled?: boolean;
}

export const DisambiguationCard = ({ draft, onSelect, disabled }: Props) => (
  <div className="tx-card tx-card--disambig" data-testid="disambig-card">
    {draft.candidates.map((c) => (
      <button
        key={c.id}
        className="candidate-row"
        onClick={() => onSelect(c)}
        disabled={disabled}
        data-testid={`disambig-candidate-${c.id}`}
      >
        <div className="candidate-avatar">
          {c.display_name
            .split(" ")
            .slice(-1)[0]
            .charAt(0)
            .toUpperCase()}
        </div>
        <div className="candidate-meta">
          <div className="candidate-name">{c.display_name}</div>
          <div className="candidate-bank">
            {c.bank} · {c.account_masked}
          </div>
        </div>
      </button>
    ))}
  </div>
);
