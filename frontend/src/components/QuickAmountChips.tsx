interface Props {
  /** The current chat input value — chips hide once an amount-shaped
   *  token shows up so they don't fight the user's own typing. */
  input: string;
  busy?: boolean;
  onPick: (amountText: string) => void;
}

const CHIPS: { label: string; text: string }[] = [
  { label: "100k", text: "100k" },
  { label: "500k", text: "500k" },
  { label: "1 triệu", text: "1 triệu" },
  { label: "2 triệu", text: "2 triệu" },
  { label: "5 triệu", text: "5 triệu" },
];

/** Heuristic: input is "transfer-shaped" if it starts with a known transfer
 *  verb, and an amount hasn't already been typed. The detection is loose
 *  on purpose — if it false-positives the chips just sit there harmlessly. */
const HAS_AMOUNT_RE = /\d|nghìn|triệu|tỷ|\bk\b/i;
const TRANSFER_PREFIX_RE = /^(chuyển|gửi|cho|gui|chuyen)\b/i;

/**
 * Floating row of common amounts above the input bar — appears only
 * while the user is composing a transfer without an amount yet. Tapping
 * a chip appends the amount text to the input (with a leading space if
 * needed) so the user can keep typing the recipient first or last.
 */
export const QuickAmountChips = ({ input, busy, onPick }: Props) => {
  const trimmed = input.trim();
  const shouldShow =
    trimmed.length >= 2 &&
    TRANSFER_PREFIX_RE.test(trimmed) &&
    !HAS_AMOUNT_RE.test(trimmed);
  if (!shouldShow) return null;

  return (
    <div className="quick-amounts" aria-label="Số tiền nhanh">
      <span className="quick-amounts__title">Số tiền nhanh:</span>
      <div className="quick-amounts__list">
        {CHIPS.map((c) => (
          <button
            key={c.text}
            type="button"
            className="quick-amount-chip"
            disabled={busy}
            onClick={() => onPick(c.text)}
          >
            {c.label}
          </button>
        ))}
      </div>
    </div>
  );
};
