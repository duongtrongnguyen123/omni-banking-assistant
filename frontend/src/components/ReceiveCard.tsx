import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { formatVND } from "../format";

interface Props {
  open: boolean;
  onClose: () => void;
  /**
   * Optional toast hook — App.tsx already mounts <ToastStack /> wired
   * to the WS event bus. We dispatch a window event here that the
   * stack listens for. Keeping this prop slot in case a parent wants
   * to override.
   */
  onToast?: (message: string) => void;
}

interface AccountInfo {
  id: string;
  display_name: string;
  bank: string;
  account_number: string;
}

const DEFAULT_OWNER: AccountInfo = {
  id: "acc_an_main",
  display_name: "An",
  bank: "Omni Bank",
  account_number: "1234567890",
};

/**
 * "Nhận tiền" modal.
 *
 * Shows a generated VietQR-style QR with the user's primary account.
 * Lets the user optionally embed an amount so the sender's wallet can
 * pre-fill it. "Sao chép STK" copies to clipboard; "Lưu ảnh" downloads
 * the PNG.
 *
 * Important: the QR is regenerated server-side every time the amount
 * input changes (debounced ~250ms) — the backend is the single source
 * of truth for the wire format.
 */
export function ReceiveCard({ open, onClose, onToast }: Props) {
  const [owner, setOwner] = useState<AccountInfo>(DEFAULT_OWNER);
  const [amountInput, setAmountInput] = useState<string>("");
  const [qrBase64, setQrBase64] = useState<string>("");
  const [payloadText, setPayloadText] = useState<string>("");
  const [error, setError] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(false);

  // Pull the real /api/me payload on first open so the QR carries the
  // actual primary account. Falls back to the hard-coded mock if the
  // request fails — the demo must still show *something*.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    (async () => {
      try {
        const me = await api.me();
        const primary =
          me.accounts.find((a) => a.primary) ?? me.accounts[0] ?? null;
        if (!cancelled && primary) {
          setOwner({
            id: primary.id,
            display_name: me.display_name,
            bank: primary.bank,
            account_number: primary.number,
          });
        }
      } catch {
        /* keep default */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open]);

  const parsedAmount = useMemo(() => {
    const cleaned = amountInput.replace(/[^\d]/g, "");
    if (!cleaned) return null;
    const n = Number(cleaned);
    return Number.isFinite(n) && n > 0 ? n : null;
  }, [amountInput]);

  // Debounce QR regeneration — typing each digit shouldn't slam the
  // backend.
  useEffect(() => {
    if (!open || !owner.account_number) return;
    setLoading(true);
    setError("");
    const handle = window.setTimeout(async () => {
      try {
        const out = await api.qrGenerate({
          bank: owner.bank,
          account_number: owner.account_number,
          amount: parsedAmount ?? undefined,
        });
        setQrBase64(out.qr_base64);
        setPayloadText(out.payload_text);
      } catch (e) {
        setError(String(e instanceof Error ? e.message : e));
      } finally {
        setLoading(false);
      }
    }, 250);
    return () => window.clearTimeout(handle);
  }, [open, owner.account_number, owner.bank, parsedAmount]);

  const fireToast = useCallback(
    (msg: string) => {
      if (onToast) {
        onToast(msg);
        return;
      }
      // Generic window event — falls through silently if no listener.
      try {
        window.dispatchEvent(
          new CustomEvent("omni:toast", { detail: { message: msg } }),
        );
      } catch {
        /* ignore */
      }
    },
    [onToast],
  );

  const onCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(owner.account_number);
      fireToast("Đã sao chép số tài khoản.");
    } catch {
      fireToast("Không sao chép được — vui lòng thử lại.");
    }
  }, [owner.account_number, fireToast]);

  const onDownload = useCallback(() => {
    if (!qrBase64) return;
    const dataUrl = `data:image/png;base64,${qrBase64}`;
    const a = document.createElement("a");
    a.href = dataUrl;
    const stamp = new Date().toISOString().slice(0, 10);
    a.download = `omni-qr-${owner.account_number}-${stamp}.png`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    fireToast("Đã lưu QR vào máy.");
  }, [qrBase64, owner.account_number, fireToast]);

  // Esc closes.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const maskedAccount = owner.account_number.replace(
    /^(\d{4})(\d+)(\d{2})$/,
    "$1 •••• $3",
  );

  return (
    <div
      className="receive-modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="receive-modal-title"
    >
      <div className="receive-modal__backdrop" onClick={onClose} />
      <div className="receive-modal__card">
        <header className="receive-modal__header">
          <h2 id="receive-modal-title" className="receive-modal__title">
            Nhận tiền qua QR
          </h2>
          <button
            type="button"
            className="receive-modal__close"
            onClick={onClose}
            aria-label="Đóng"
          >
            <span aria-hidden="true">×</span>
          </button>
        </header>
        <div className="receive-modal__qr-wrap" aria-busy={loading}>
          {qrBase64 ? (
            <img
              className="receive-modal__qr"
              src={`data:image/png;base64,${qrBase64}`}
              alt={`Mã QR nhận tiền cho ${owner.display_name}`}
            />
          ) : (
            <div className="receive-modal__qr-placeholder" aria-hidden="true">
              {error || (loading ? "Đang tạo QR…" : "Chuẩn bị QR…")}
            </div>
          )}
        </div>
        <div className="receive-modal__owner">
          <div className="receive-modal__owner-name">
            {owner.display_name}
          </div>
          <div className="receive-modal__owner-bank">{owner.bank}</div>
          <div
            className="receive-modal__owner-account"
            aria-label={`Số tài khoản: ${owner.account_number}`}
            title={owner.account_number}
          >
            {maskedAccount}
          </div>
          {parsedAmount !== null && (
            <div className="receive-modal__amount-line">
              Số tiền: <strong>{formatVND(parsedAmount)}</strong>
            </div>
          )}
        </div>
        <div className="receive-modal__amount-row">
          <label htmlFor="receive-amount" className="receive-modal__amount-label">
            Gắn số tiền (không bắt buộc)
          </label>
          <input
            id="receive-amount"
            inputMode="numeric"
            placeholder="VD: 500000"
            value={amountInput}
            onChange={(e) => setAmountInput(e.target.value)}
          />
        </div>
        {error && (
          <div className="receive-modal__error" role="alert">
            {error}
          </div>
        )}
        <div className="receive-modal__actions">
          <button
            type="button"
            className="btn btn--ghost"
            onClick={onCopy}
          >
            Sao chép STK
          </button>
          <button
            type="button"
            className="btn btn--primary"
            onClick={onDownload}
            disabled={!qrBase64}
          >
            Lưu ảnh
          </button>
        </div>
        {payloadText && (
          <details className="receive-modal__payload">
            <summary>Chuỗi dữ liệu QR</summary>
            <code>{payloadText}</code>
          </details>
        )}
      </div>
    </div>
  );
}

export default ReceiveCard;
