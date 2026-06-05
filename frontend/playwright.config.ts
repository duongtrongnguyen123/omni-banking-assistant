import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright config for the Omni AI Banking Assistant chat UI.
 *
 * Spins up both servers locally:
 *   - Backend  : uvicorn on :8000   (LLM keys deliberately blanked so
 *                the rule-based pipeline drives every demo scenario —
 *                the LLM is too flaky for CI and adds 3-5s per turn).
 *   - Frontend : vite on :5173      (HMR off implicitly; we render once
 *                per spec).
 *
 * Headless by default; set PWDEBUG=1 to open the inspector / a headed
 * browser when iterating locally.
 */
export default defineConfig({
  testDir: "./tests/e2e",
  // Strict timeout per test; the backend warm path is < 1s, the slowest
  // single turn observed in smoke is ~3s, so 30s buys plenty of slack
  // for the first-test cold path that pays for the fastembed model load.
  timeout: 30_000,
  expect: { timeout: 7_000 },
  // The chat backend keeps an in-process per-user session, so two tests
  // running in parallel against the same user_id would step on each
  // other's drafts. Serialize for safety; the suite is small.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: "http://localhost:5173",
    headless: !process.env.PWDEBUG,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    locale: "vi-VN",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: [
    {
      // Force rule-based NLU by blanking provider keys so the assistant
      // is deterministic and CI doesn't pay for LLM tokens.
      command:
        "cd ../backend && GROQ_API_KEY= GEMINI_API_KEY= .venv/bin/python -m uvicorn app.main:app --port 8000 --log-level warning",
      url: "http://localhost:8000/docs",
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      command: "npm run dev -- --port 5173 --strictPort",
      url: "http://localhost:5173",
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
      stdout: "pipe",
      stderr: "pipe",
    },
  ],
});
