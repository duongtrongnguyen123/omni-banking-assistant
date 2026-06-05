/**
 * Accessibility test harness — runs jest-axe over a rendered <App/>
 * and asserts zero WCAG 2.1 A/AA violations on initial render.
 *
 * The fetch + WebSocket APIs are stubbed because jsdom doesn't provide
 * them and we don't want test runs depending on a live backend.
 */
import { describe, it, expect, beforeAll, afterAll, vi } from "vitest";
import { render, cleanup } from "@testing-library/react";
import { axe, toHaveNoViolations } from "jest-axe";
import App from "../../src/App";

expect.extend(toHaveNoViolations);

// ---------------------------------------------------------------------
// Test scaffolding: stub network so <App /> mounts cleanly under jsdom.
// ---------------------------------------------------------------------

const emptyInsights = {
  mom: {},
  anomalies: [],
  subscriptions: [],
};

const fetchStub = vi.fn(async (input: RequestInfo | URL) => {
  const url = typeof input === "string" ? input : input.toString();
  // Suggestions / ranked contacts → empty list (no suggestion strip).
  if (url.includes("/api/suggestions/recipients")) {
    return new Response(JSON.stringify([]), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }
  if (url.includes("/api/insights/summary")) {
    return new Response(JSON.stringify(emptyInsights), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }
  // Anything else (chat) — return a minimal placeholder.
  return new Response(JSON.stringify({ text: "ok" }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}) as unknown as typeof fetch;

class FakeWebSocket {
  // Minimal no-op WebSocket so `useEventStream` can construct one
  // without throwing in jsdom. We never actually emit messages.
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  readyState = FakeWebSocket.CONNECTING;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  constructor(_url: string) {
    void _url;
  }
  close() {
    this.readyState = FakeWebSocket.CLOSED;
  }
  send() {
    /* noop */
  }
  addEventListener() {
    /* noop */
  }
  removeEventListener() {
    /* noop */
  }
}

beforeAll(() => {
  vi.stubGlobal("fetch", fetchStub);
  vi.stubGlobal("WebSocket", FakeWebSocket as unknown as typeof WebSocket);
  // jsdom doesn't implement scrollIntoView / scrollTo — silently mock.
  Element.prototype.scrollIntoView = vi.fn();
  Element.prototype.scrollTo = vi.fn() as unknown as Element["scrollTo"];
});

afterAll(() => {
  vi.unstubAllGlobals();
  cleanup();
});

describe("App accessibility (axe-core, WCAG 2.1 A + AA)", () => {
  it("has no detectable a11y violations on initial render", async () => {
    const { container } = render(<App />);
    // Restrict to WCAG 2.1 A + AA — matches the dev-bootstrap config in
    // src/lib/axe.ts so test failures track what dev sees in console.
    const results = await axe(container, {
      runOnly: {
        type: "tag",
        values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"],
      },
      // jest-axe's default rules include some that don't apply to a
      // single-component test (e.g. "region" landmark on the body —
      // satisfied at the document level via <main>/<aside> here).
    });
    expect(results).toHaveNoViolations();
  });
});
