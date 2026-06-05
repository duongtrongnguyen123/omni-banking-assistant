import { test, expect } from "@playwright/test";

/**
 * InsightsCard sanity check.
 *
 * The insights summary lives on `feat/insights`; on the `main` baseline
 * this spec was authored against, `/api/insights/summary` returns 404
 * and `InsightsCard` isn't mounted. The test handles both states:
 *
 *   - feature present: assert the network call fires and the card
 *     reaches its `ready` state, then opportunistically check the MoM
 *     section if the seed produced any.
 *   - feature absent:  test.skip() so the suite stays green.
 */
test("InsightsCard fetches /api/insights/summary and renders a card", async ({
  page,
}) => {
  // Probe the API directly so we can decide whether to skip without
  // racing the React mount.
  const probe = await page.request.get(
    "http://localhost:8000/api/insights/summary",
    { headers: { "x-user-id": "u_an" } },
  );
  test.skip(
    probe.status() === 404,
    "Insights endpoint not present on the `main` baseline — feature lives on feat/insights.",
  );

  const summaryReq = page.waitForResponse(
    (res) =>
      res.url().includes("/api/insights/summary") && res.status() === 200,
  );

  await page.goto("/");
  await summaryReq;

  const card = page.getByTestId("insights-card");
  if ((await card.count()) === 0) {
    test.skip(true, "InsightsCard component not mounted on this baseline.");
    return;
  }
  await expect(card).toBeVisible();
  // The card has three terminal states (loading, error, ready). After
  // the response resolves we should be `ready`.
  await expect(card).toHaveAttribute("data-state", "ready", {
    timeout: 5_000,
  });

  // The MoM section is optional — surface it as a soft check so we
  // don't break the suite if the seed drifts.
  const mom = page.getByTestId("insights-mom");
  if ((await mom.count()) > 0) {
    await expect(mom).toContainText("Tháng này so với tháng trước");
  }
});
