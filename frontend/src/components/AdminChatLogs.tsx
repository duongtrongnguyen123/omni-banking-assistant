import { useCallback, useEffect, useMemo, useState } from "react";
import { api, friendlyApiError } from "../api/client";
import type {
  AdminChatMessage,
  AdminChatSession,
  AdminChatSessionDetail,
  AdminTransaction,
} from "../types";
import { Message } from "./Message";
import { repairVietnameseText } from "../lib/repairVietnamese";

const ADMIN_TOKEN_KEY = "omni.admin.token";
const LIVE_REFRESH_MS = 2000;

const fmtTime = (value: string) =>
  new Date(value).toLocaleString("vi-VN", {
    hour: "2-digit",
    minute: "2-digit",
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  });

const fmtVnd = (value: number) =>
  new Intl.NumberFormat("vi-VN").format(value) + "đ";

const statusLabel = (status: string) =>
  status === "completed"
    ? "Hoàn tất"
    : status === "pending"
      ? "Đang chờ"
      : status === "cancelled"
        ? "Đã huỷ"
        : status;

function inferDraftState(messages: AdminChatMessage[]) {
  const closed = new Set<string>();
  const cancelled = new Set<string>();
  const confirmed = new Set<string>();
  let latestDraftId: string | null = null;
  for (const message of messages) {
    const resp = message.response;
    if (resp?.draft?.id) latestDraftId = resp.draft.id;
    if (resp?.intent === "transfer" && latestDraftId && !resp.draft) {
      closed.add(latestDraftId);
      const lower = resp.text.toLowerCase();
      if (lower.includes("huỷ") || lower.includes("hủy")) cancelled.add(latestDraftId);
      else if (lower.includes("đã chuyển") || lower.includes("mã giao dịch")) {
        confirmed.add(latestDraftId);
      }
    }
  }
  return { closed, cancelled, confirmed };
}

function MessageRow({
  message,
  closedDraftIds,
  cancelledDraftIds,
  confirmedDraftIds,
}: {
  message: AdminChatMessage;
  closedDraftIds: Set<string>;
  cancelledDraftIds: Set<string>;
  confirmedDraftIds: Set<string>;
}) {
  const content = repairVietnameseText(message.content);
  if (message.response) {
    const activeDraft = message.response.draft?.id;
    const actionableDraftIds =
      activeDraft && !closedDraftIds.has(activeDraft) ? new Set([activeDraft]) : new Set<string>();
    return (
      <div className="admin-log-replay">
        <Message
          message={{
            id: message.id,
            role: message.role,
            text: content,
            response: message.response,
          }}
          onConfirm={() => undefined}
          onCancel={() => undefined}
          onSelectCandidate={() => undefined}
          onConfirmContact={() => undefined}
          onCancelContact={() => undefined}
          onConfirmSchedule={() => undefined}
          onCancelSchedule={() => undefined}
          actionableDraftIds={actionableDraftIds}
          cancelledDraftIds={cancelledDraftIds}
          confirmedDraftIds={confirmedDraftIds}
          actionableScheduleDraftIds={new Set()}
          busy
        />
      </div>
    );
  }
  return (
    <div className={`admin-log-message admin-log-message--${message.role}`}>
      <div className="admin-log-message__meta">
        <strong>{message.role === "user" ? "Người dùng" : "Omni"}</strong>
        {message.intent && <span>{message.intent}</span>}
        <time>{fmtTime(message.created_at)}</time>
      </div>
      <div className="admin-log-message__body">{content}</div>
    </div>
  );
}

function TransactionRow({ tx }: { tx: AdminTransaction }) {
  return (
    <tr>
      <td>
        <strong>{fmtVnd(tx.amount)}</strong>
        <span>{statusLabel(tx.status)}</span>
      </td>
      <td>
        <strong>{tx.recipient_name || tx.contact_id || "Không rõ"}</strong>
        <span>
          {[tx.recipient_bank, tx.recipient_account_masked]
            .filter(Boolean)
            .join(" · ") || "Không có thông tin người nhận"}
        </span>
      </td>
      <td>{tx.user_id}</td>
      <td>{tx.description || "Chuyển khoản"}</td>
      <td>{tx.category}</td>
      <td>{fmtTime(tx.created_at)}</td>
    </tr>
  );
}

export function AdminChatLogs() {
  const [token, setToken] = useState(() => {
    try {
      return window.localStorage.getItem(ADMIN_TOKEN_KEY) ?? "";
    } catch {
      return "";
    }
  });
  const [q, setQ] = useState("");
  const [userId, setUserId] = useState("");
  const [intent, setIntent] = useState("");
  const [sessions, setSessions] = useState<AdminChatSession[]>([]);
  const [transactions, setTransactions] = useState<AdminTransaction[]>([]);
  const [total, setTotal] = useState(0);
  const [txTotal, setTxTotal] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<AdminChatSessionDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [txLoading, setTxLoading] = useState(false);
  const [error, setError] = useState("");
  const draftState = useMemo(
    () => inferDraftState(detail?.messages ?? []),
    [detail?.messages],
  );

  const saveToken = (next: string) => {
    setToken(next);
    try {
      if (next.trim()) window.localStorage.setItem(ADMIN_TOKEN_KEY, next);
      else window.localStorage.removeItem(ADMIN_TOKEN_KEY);
    } catch {
      /* localStorage can fail in private mode; the field still works. */
    }
  };

  const filters = useMemo(
    () => ({
      userId: userId.trim() || undefined,
      q: q.trim() || undefined,
      intent: intent.trim() || undefined,
      limit: 100,
    }),
    [intent, q, userId],
  );

  const loadSessions = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await api.adminChatSessions(token, filters);
      setSessions(res.sessions);
      setTotal(res.total);
      if (res.sessions.length > 0) {
        setSelectedId((current) =>
          current && res.sessions.some((s) => s.id === current)
            ? current
            : res.sessions[0].id,
        );
      } else {
        setSelectedId(null);
        setDetail(null);
      }
    } catch (err) {
      setError(friendlyApiError(err));
    } finally {
      setLoading(false);
    }
  }, [filters, token]);

  const loadTransactions = useCallback(async () => {
    setTxLoading(true);
    setError("");
    try {
      const res = await api.adminTransactions(token, {
        userId: filters.userId,
        q: filters.q,
        status: "completed",
        limit: 100,
      });
      setTransactions(res.transactions);
      setTxTotal(res.total);
    } catch (err) {
      setError(friendlyApiError(err));
    } finally {
      setTxLoading(false);
    }
  }, [filters.q, filters.userId, token]);

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  useEffect(() => {
    void loadTransactions();
  }, [loadTransactions]);

  useEffect(() => {
    if (!selectedId) return;
    let cancelled = false;
    setDetailLoading(true);
    setError("");
    api
      .adminChatSessionDetail(token, selectedId)
      .then((res) => {
        if (!cancelled) setDetail(res);
      })
      .catch((err) => {
        if (!cancelled) setError(friendlyApiError(err));
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId, token]);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      if (document.visibilityState !== "visible") return;
      try {
        const [sessionRes, txRes] = await Promise.all([
          api.adminChatSessions(token, filters),
          api.adminTransactions(token, {
            userId: filters.userId,
            q: filters.q,
            status: "completed",
            limit: 100,
          }),
        ]);
        if (cancelled) return;
        setSessions(sessionRes.sessions);
        setTotal(sessionRes.total);
        setTransactions(txRes.transactions);
        setTxTotal(txRes.total);
        const stillSelected =
          selectedId && sessionRes.sessions.some((s) => s.id === selectedId);
        const nextSelected = stillSelected
          ? selectedId
          : sessionRes.sessions[0]?.id ?? null;
        if (nextSelected !== selectedId) {
          setSelectedId(nextSelected);
        }
        if (nextSelected) {
          const nextDetail = await api.adminChatSessionDetail(token, nextSelected);
          if (!cancelled) setDetail(nextDetail);
        } else {
          setDetail(null);
        }
      } catch {
        // Background refresh is best-effort. Manual refresh still surfaces errors.
      }
    };
    const id = window.setInterval(() => {
      void tick();
    }, LIVE_REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [filters, selectedId, token]);

  return (
    <main className="admin-log-page">
      <header className="admin-log-header">
        <div>
          <p className="admin-log-kicker">OMNI Admin</p>
          <h1>Nhật ký hội thoại</h1>
          <p>
            Trang chỉ đọc dành cho quản trị viên kiểm tra hội thoại, xác thực
            và giao dịch. Trang này không hiển thị trong giao diện người dùng.
          </p>
        </div>
        <a className="admin-log-home" href="/">
          Mở app người dùng
        </a>
      </header>

      <section className="admin-log-filters" aria-label="Bộ lọc log hội thoại">
        <label>
          Token quản trị
          <input
            value={token}
            onChange={(e) => saveToken(e.target.value)}
            placeholder="Bearer token nếu OMNI_ADMIN_TOKEN được bật"
            type="password"
          />
        </label>
        <label>
          Mã người dùng
          <input
            value={userId}
            onChange={(e) => setUserId(e.target.value)}
            placeholder="u_an"
          />
        </label>
        <label>
          Ý định
          <input
            value={intent}
            onChange={(e) => setIntent(e.target.value)}
            placeholder="transfer, balance..."
          />
        </label>
        <label className="admin-log-search">
          Tìm kiếm
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Tìm trong nội dung hội thoại"
          />
        </label>
        <button onClick={loadSessions} disabled={loading}>
          {loading || txLoading ? "Đang tải..." : "Làm mới"}
        </button>
      </section>

      {error && (
        <div className="admin-log-error" role="alert">
          {error}
        </div>
      )}

      <section className="admin-log-shell">
        <aside className="admin-log-list" aria-label="Danh sách hội thoại">
          <div className="admin-log-list__summary">
            <strong>{total}</strong> cuộc hội thoại
          </div>
          {sessions.length === 0 && (
            <div className="admin-log-empty">Chưa có log phù hợp.</div>
          )}
          {sessions.map((session) => (
            (() => {
              const title =
                repairVietnameseText(session.title) || "Cuộc trò chuyện mới";
              const preview =
                repairVietnameseText(session.preview) || "Không có bản xem trước";
              return (
                <button
                  key={session.id}
                  className={`admin-log-session ${
                    selectedId === session.id ? "is-active" : ""
                  }`}
                  onClick={() => setSelectedId(session.id)}
                >
                  <span className="admin-log-session__title">{title}</span>
                  <span className="admin-log-session__meta">
                    {session.user_id} · {session.message_count} tin nhắn
                  </span>
                  <span className="admin-log-session__preview">{preview}</span>
                  <span className="admin-log-session__tags">
                    {session.intents.slice(0, 3).map((tag) => (
                      <em key={tag}>{tag}</em>
                    ))}
                  </span>
                </button>
              );
            })()
          ))}
        </aside>

        <article className="admin-log-detail">
          {!selectedId && (
            <div className="admin-log-empty">Chọn một hội thoại để xem.</div>
          )}
          {selectedId && detailLoading && (
            <div className="admin-log-empty">Đang tải hội thoại...</div>
          )}
          {detail && !detailLoading && (
            <>
              <div className="admin-log-detail__head">
                <div>
                  <p>{detail.user_id}</p>
                  <h2>
                    {repairVietnameseText(detail.title) || "Cuộc trò chuyện mới"}
                  </h2>
                </div>
                <time>{fmtTime(detail.updated_at)}</time>
              </div>
              <div className="admin-log-messages">
                {detail.messages.map((message) => (
                  <MessageRow
                    key={message.id}
                    message={message}
                    closedDraftIds={draftState.closed}
                    cancelledDraftIds={draftState.cancelled}
                    confirmedDraftIds={draftState.confirmed}
                  />
                ))}
              </div>
            </>
          )}
        </article>
      </section>

      <section className="admin-ledger" aria-label="Lịch sử giao dịch đã thực hiện">
        <div className="admin-ledger__head">
          <div>
            <p className="admin-log-kicker">Lịch sử giao dịch</p>
            <h2>Giao dịch đã thực hiện trong Omni</h2>
          </div>
          <button onClick={loadTransactions} disabled={txLoading}>
            {txLoading ? "Đang tải..." : "Làm mới lịch sử"}
          </button>
        </div>
        <p className="admin-ledger__hint">
          Dữ liệu này đọc từ bảng giao dịch thật. Nếu giao dịch đã xác nhận,
          bản ghi vẫn nằm ở đây và trong lịch sử ngân hàng.
        </p>
        <div className="admin-ledger__summary">
          <strong>{txTotal}</strong> giao dịch hoàn tất
        </div>
        <div className="admin-ledger__table-wrap">
          <table className="admin-ledger__table">
            <thead>
              <tr>
                <th>Số tiền</th>
                <th>Người nhận</th>
                <th>Người dùng</th>
                <th>Nội dung</th>
                <th>Category</th>
                <th>Thời gian</th>
              </tr>
            </thead>
            <tbody>
              {transactions.map((tx) => (
                <TransactionRow key={tx.id} tx={tx} />
              ))}
              {transactions.length === 0 && (
                <tr>
                  <td colSpan={6}>Chưa có giao dịch phù hợp.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </main>
  );
}
