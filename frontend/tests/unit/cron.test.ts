import { describe, expect, it } from "vitest";
import { humanizeCron } from "../../src/lib/cron";

describe("humanizeCron", () => {
  // Pin the 5 shapes the orchestrator emits — ScheduleCard's pill
  // depends on these reading naturally in Vietnamese, and a silent
  // formatter regression would just hide the pill, not break anything
  // loud enough to spot in QA.
  it.each([
    ["0 9 1 * *", "ngày 1 hàng tháng lúc 09:00"],
    ["30 8 15 * *", "ngày 15 hàng tháng lúc 08:30"],
    ["0 0 1,15 * *", "ngày 1, 15 hàng tháng lúc 00:00"],
    ["0 8 * * 1", "hàng tuần thứ 2 lúc 08:00"],
    ["0 8 * * 6", "hàng tuần thứ 7 lúc 08:00"],
    ["0 9 * * 1,5", "hàng tuần thứ 2 và thứ 6 lúc 09:00"],
    ["0 7 * * *", "hàng ngày lúc 07:00"],
  ])("formats %s as %s", (cron, expected) => {
    expect(humanizeCron(cron)).toBe(expected);
  });

  it("returns null for invalid input", () => {
    expect(humanizeCron("not a cron")).toBeNull();
    expect(humanizeCron("0 0 *")).toBeNull();
    expect(humanizeCron("xx 0 1 * *")).toBeNull();
  });
});
