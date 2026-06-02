import type { BalanceResult } from "../types";
import { formatVND } from "../format";

export const BalanceCard = ({ balance }: { balance: BalanceResult }) => (
  <div className="bal-card">
    <div className="bal-card__label">TỔNG SỐ DƯ</div>
    <div className="bal-card__total">{formatVND(balance.total)}</div>
    <ul className="bal-card__accounts">
      {balance.accounts.map((a) => (
        <li key={a.id}>
          <div>
            <div className="bal-account__bank">
              {a.bank} {a.primary && <span className="bal-tag">Chính</span>}
            </div>
            <div className="bal-account__num">•••• {a.number.slice(-4)}</div>
          </div>
          <div className="bal-account__amount">{formatVND(a.balance)}</div>
        </li>
      ))}
    </ul>
  </div>
);
