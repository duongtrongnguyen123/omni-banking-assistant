import type { MyAccount } from "../types";
import { copyTextWithToast } from "../lib/clipboard";

interface Props {
  accounts: MyAccount[];
}

export const MyAccountCard = ({ accounts }: Props) => {
  if (!accounts || accounts.length === 0) return null;
  const copy = (val: string) => {
    void copyTextWithToast(val, "Đã sao chép STK");
  };
  return (
    <div className="my-account-card">
      <div className="my-account-card__header">Tài khoản của bạn</div>
      {accounts.map((a) => (
        <div key={a.id} className={`my-account-card__row${a.primary ? " is-primary" : ""}`}>
          <div className="my-account-card__bank">
            {a.bank}
            {a.primary && <span className="my-account-card__badge">Chính</span>}
          </div>
          <div className="my-account-card__number">
            <code>{a.number}</code>
            <button
              type="button"
              className="my-account-card__copy"
              onClick={() => copy(a.number)}
              aria-label={`Sao chép STK ${a.number}`}
            >
              Sao chép
            </button>
          </div>
          <div className="my-account-card__holder">Chủ: {a.holder_name}</div>
        </div>
      ))}
    </div>
  );
};
