import { useState } from "react";
import type { ReceiveQrPayload } from "../types";

interface Props {
  qr: ReceiveQrPayload;
}

const formatVND = (amount: number) =>
  new Intl.NumberFormat("vi-VN").format(amount) + "đ";

export const ReceiveQrCard = ({ qr }: Props) => {
  const [imgFailed, setImgFailed] = useState(false);
  const copy = (val: string) => {
    if (navigator.clipboard) navigator.clipboard.writeText(val);
  };
  return (
    <div className="qr-card">
      <div className="qr-card__header">QR nhận tiền</div>
      <div className="qr-card__body">
        {imgFailed ? (
          <div
            className="qr-card__image qr-card__image--fallback"
            role="img"
            aria-label="QR không hiển thị được"
          >
            Không hiển thị được mã QR. Vui lòng dùng STK bên dưới.
          </div>
        ) : (
          <img
            src={`data:image/png;base64,${qr.png_base64}`}
            alt="QR nhận tiền"
            className="qr-card__image"
            onError={() => setImgFailed(true)}
          />
        )}
        <div className="qr-card__meta">
          <div className="qr-card__bank">{qr.bank}</div>
          <div className="qr-card__account">
            <code>{qr.account}</code>
            <button
              type="button"
              className="qr-card__copy"
              onClick={() => copy(qr.account)}
              aria-label="Sao chép số tài khoản"
            >
              Sao chép STK
            </button>
          </div>
          <div className="qr-card__holder">Chủ: {qr.holder_name}</div>
          {qr.amount !== null && (
            <div className="qr-card__amount">
              Số tiền: <strong>{formatVND(qr.amount)}</strong>
            </div>
          )}
          {qr.description && (
            <div className="qr-card__memo">Nội dung: {qr.description}</div>
          )}
        </div>
      </div>
      <div className="qr-card__hint">
        Quét bằng app ngân hàng để chuyển tiền vào tài khoản.
      </div>
    </div>
  );
};
