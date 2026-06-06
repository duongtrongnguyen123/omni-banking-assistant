// Convert a simple 5-field cron expression to a short Vietnamese phrase.
// Returns null when the pattern isn't one of the common shapes the
// orchestrator emits — callers fall back to showing the raw cron.
//
// Supported shapes:
//   "m H -slash-N -slash- -slash- " (every N days)       → "mỗi N ngày lúc HH:MM"
//   "m H -star- -star- d"            (weekly on day d)   → "hàng tuần thứ X lúc HH:MM"
//   "m H -star- -star- d1,d2"        (weekly two days)   → "thứ X và thứ Y lúc HH:MM"
//   "m H D -star- -star-"            (monthly on day D)  → "ngày D hàng tháng lúc HH:MM"
//   "m H D1,D2 -star- -star-"        (monthly multiple)  → "ngày D1, D2 hàng tháng lúc HH:MM"
//   "m H -star- -star- -star-"        (daily)            → "hàng ngày lúc HH:MM"
export function humanizeCron(cron: string): string | null {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [m, h, dom, mon, dow] = parts;

  const mn = parseInt(m, 10);
  const hn = parseInt(h, 10);
  if (!isFinite(mn) || !isFinite(hn)) return null;
  const time = `${String(hn).padStart(2, "0")}:${String(mn).padStart(2, "0")}`;

  const DOW: Record<string, string> = {
    "0": "chủ nhật",
    "1": "thứ 2",
    "2": "thứ 3",
    "3": "thứ 4",
    "4": "thứ 5",
    "5": "thứ 6",
    "6": "thứ 7",
    "7": "chủ nhật",
  };

  if (mon === "*" && dow !== "*" && dom === "*") {
    const days = dow.split(",").map((d) => DOW[d.trim()] ?? `thứ ${d}`);
    return `hàng tuần ${days.join(" và ")} lúc ${time}`;
  }

  if (mon === "*" && dow === "*" && dom !== "*") {
    if (/^\*\/\d+$/.test(dom)) {
      const n = parseInt(dom.slice(2), 10);
      return `mỗi ${n} ngày lúc ${time}`;
    }
    const days = dom.split(",").map((d) => d.trim());
    if (days.length === 1) return `ngày ${days[0]} hàng tháng lúc ${time}`;
    return `ngày ${days.join(", ")} hàng tháng lúc ${time}`;
  }

  if (mon === "*" && dow === "*" && dom === "*") {
    return `hàng ngày lúc ${time}`;
  }

  return null;
}
