/**
 * Audit-mode a11y scan — does NOT fail the suite. Prints a full axe
 * violation summary so we can refresh `docs/a11y-audit.md`. Marked
 * `.skip` by default; run via:
 *
 *   AUDIT_A11Y=1 npx vitest run tests/unit/a11y.audit.test.tsx
 */
import { describe, it, vi, beforeAll, afterAll } from "vitest";
import { render, cleanup } from "@testing-library/react";
import { axe } from "jest-axe";
import App from "../../src/App";

const fetchStub = vi.fn(async (input: RequestInfo | URL) => {
  const url = typeof input === "string" ? input : input.toString();
  if (url.includes("/api/suggestions/recipients")) {
    return new Response(JSON.stringify([]), { status: 200 });
  }
  if (url.includes("/api/insights/summary")) {
    return new Response(
      JSON.stringify({ mom: {}, anomalies: [], subscriptions: [] }),
      { status: 200 },
    );
  }
  return new Response(JSON.stringify({ text: "ok" }), { status: 200 });
}) as unknown as typeof fetch;

class FakeWS {
  static CONNECTING = 0;
  readyState = 0;
  onopen: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onclose: ((e: CloseEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  constructor() {
    /* noop */
  }
  close() {}
  send() {}
  addEventListener() {}
  removeEventListener() {}
}

beforeAll(() => {
  vi.stubGlobal("fetch", fetchStub);
  vi.stubGlobal("WebSocket", FakeWS as unknown as typeof WebSocket);
  Element.prototype.scrollIntoView = vi.fn();
  Element.prototype.scrollTo = vi.fn() as unknown as Element["scrollTo"];
});

afterAll(() => {
  vi.unstubAllGlobals();
  cleanup();
});

const ENABLED = process.env.AUDIT_A11Y === "1";

describe.skipIf(!ENABLED)("axe audit (report only)", () => {
  it("dumps every violation grouped by impact", async () => {
    const { container } = render(<App />);
    const results = await axe(container, {
      runOnly: {
        type: "tag",
        values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"],
      },
    });
    const byImpact: Record<string, number> = {};
    for (const v of results.violations) {
      byImpact[v.impact ?? "minor"] =
        (byImpact[v.impact ?? "minor"] ?? 0) + v.nodes.length;
    }
    // eslint-disable-next-line no-console
    console.log("[a11y audit] violation summary:", byImpact);
    for (const v of results.violations) {
      // eslint-disable-next-line no-console
      console.log(
        `\n[${v.impact}] ${v.id} (${v.nodes.length}x)`,
        "\n  ", v.help,
        "\n  ", v.helpUrl,
        "\n  tags:", v.tags.join(", "),
      );
      for (const n of v.nodes.slice(0, 3)) {
        // eslint-disable-next-line no-console
        console.log("   →", n.target.join(" "), "\n     ", n.failureSummary);
      }
    }
  });
});
