import type { Page } from "@playwright/test";

/**
 * Reset the backend's in-process session for the demo user so each test
 * starts from a clean slate (no dangling drafts).
 *
 * Sessions are keyed by the `x-user-id` header (defaults to `u_an` for
 * the demo). We call this before every test instead of restarting
 * uvicorn — restarting would re-bootstrap the SQLite seed every time
 * and triple the suite runtime.
 */
export async function resetSession(page: Page): Promise<void> {
  const res = await page.request.post("http://localhost:8000/api/session/reset", {
    headers: { "x-user-id": "u_an" },
  });
  if (!res.ok()) {
    throw new Error(`session reset failed: ${res.status()} ${await res.text()}`);
  }
}

/**
 * Click a QuickScenarios chip by its KB code (KB1, KB2, …).
 *
 * The chip fires the same `send(text)` path the input + send button
 * use, so this is a faithful smoke of the demo flow.
 */
export async function clickScenario(page: Page, code: string): Promise<void> {
  await page.getByTestId(`quick-chip-${code}`).click();
}

/**
 * Type a message into the chat input and submit via the send button.
 */
export async function sendChat(page: Page, text: string): Promise<void> {
  await page.getByTestId("chat-input").fill(text);
  await page.getByTestId("chat-send-btn").click();
}
