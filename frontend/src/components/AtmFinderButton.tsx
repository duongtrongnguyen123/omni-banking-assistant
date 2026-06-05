import { useState } from "react";
import { api } from "../api/client";
import type { AtmHit } from "../types";

interface Props {
  busy?: boolean;
  onAtms: (atms: AtmHit[], note?: string) => void;
}

/**
 * "ATM gần đây" pill — surfaced in the suggestion strip.
 *
 * Tap flow:
 *   1. Ask the browser for the user's coordinates.
 *   2. POST to /api/atm/nearby with a 2km radius.
 *   3. Permission denied / no support → fall back to /api/atm/nearby
 *      with the HN city centre so the demo still shows something.
 *
 * Geolocation is browser-native, no npm dep.
 */
export const AtmFinderButton = ({ busy, onAtms }: Props) => {
  const [pending, setPending] = useState(false);

  const fallback = async (note: string) => {
    try {
      // Hà Nội city centre — guarantees a non-empty card so the demo
      // doesn't show "[]" when the browser blocks geolocation.
      const list = await api.atmsNearby(21.0285, 105.8542, 5);
      onAtms(list, note);
    } catch {
      onAtms([], note);
    }
  };

  const onClick = () => {
    if (busy || pending) return;
    setPending(true);
    if (!("geolocation" in navigator)) {
      fallback("Trình duyệt không hỗ trợ vị trí — Omni hiển thị ATM khu vực Hà Nội.").finally(
        () => setPending(false),
      );
      return;
    }
    navigator.geolocation.getCurrentPosition(
      async (pos) => {
        try {
          const list = await api.atmsNearby(
            pos.coords.latitude,
            pos.coords.longitude,
            2,
          );
          if (list.length === 0) {
            // No hits within 2km — widen the radius once.
            const wider = await api.atmsNearby(
              pos.coords.latitude,
              pos.coords.longitude,
              10,
            );
            onAtms(wider, "Không có ATM trong 2km — mở rộng phạm vi 10km.");
          } else {
            onAtms(list, "Đã sắp xếp theo khoảng cách từ vị trí của bạn.");
          }
        } catch {
          onAtms([], "Không gọi được dịch vụ ATM.");
        } finally {
          setPending(false);
        }
      },
      () => {
        fallback("Bạn chưa cho phép truy cập vị trí — Omni hiển thị 15 ATM mẫu.").finally(
          () => setPending(false),
        );
      },
      { enableHighAccuracy: false, timeout: 5000, maximumAge: 60_000 },
    );
  };

  return (
    <button
      type="button"
      className="atm-pill"
      onClick={onClick}
      disabled={busy || pending}
      aria-label="Tìm ATM gần đây"
      title="Sử dụng vị trí để tìm ATM gần bạn"
    >
      <span aria-hidden="true">📍</span>
      {pending ? "Đang định vị…" : "ATM gần đây"}
    </button>
  );
};
