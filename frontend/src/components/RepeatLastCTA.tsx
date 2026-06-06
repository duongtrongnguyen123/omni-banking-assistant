interface Props {
  visible: boolean;
  busy?: boolean;
  onClick: () => void;
  /** Optional sibling action — when present renders a second ghost
   *  button "Cùng số tiền, người khác" next to the primary CTA. */
  onSameAmountDifferentRecipient?: () => void;
}

/**
 * Floating "Lặp lại lần trước" CTA shown above the input bar once the
 * session has at least one confirmed transfer. Sends the canonical
 * "Lặp lại giao dịch vừa rồi" message verbatim — the orchestrator
 * handles the rest.
 *
 * When ``onSameAmountDifferentRecipient`` is provided a second ghost
 * button appears next to it; tapping prefills the chat input with
 * ``chuyển <last amount> cho `` so the user only needs to add a
 * recipient.
 */
export const RepeatLastCTA = ({
  visible,
  busy,
  onClick,
  onSameAmountDifferentRecipient,
}: Props) => {
  if (!visible) return null;
  return (
    <div className="repeat-cta" data-testid="repeat-cta">
      <button
        type="button"
        className="repeat-cta__btn"
        onClick={onClick}
        disabled={busy}
        aria-label="Lặp lại giao dịch vừa rồi"
        data-testid="repeat-cta-btn"
      >
        <svg
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.4"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden
        >
          <path d="M21 12a9 9 0 1 1-3-6.7" />
          <path d="M21 4v5h-5" />
        </svg>
        Lặp lại giao dịch vừa rồi
      </button>
      {onSameAmountDifferentRecipient && (
        <button
          type="button"
          className="repeat-cta__btn repeat-cta__btn--ghost"
          onClick={onSameAmountDifferentRecipient}
          disabled={busy}
          aria-label="Chuyển cùng số tiền cho người khác"
          data-testid="repeat-cta-same-amount-btn"
        >
          Cùng số tiền, người khác
        </button>
      )}
    </div>
  );
};
