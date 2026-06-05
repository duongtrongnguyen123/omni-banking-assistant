/// <reference types="vite/client" />
/**
 * axe-core bootstrap — DEV ONLY.
 *
 * Logs WCAG 2.1 A/AA violations to the browser console as the app
 * renders, so the team gets immediate feedback while editing the UI.
 * Wrapped in a dynamic `import()` guarded by `import.meta.env.DEV` so
 * the axe runtime is **tree-shaken out of the production bundle**.
 *
 * Usage (already wired in `main.tsx`):
 *   if (import.meta.env.DEV) import("./lib/axe").then(m => m.bootAxe());
 *
 * The runtime cost is debounced (1s) so re-renders during typing don't
 * spam the console.
 */

export async function bootAxe(): Promise<void> {
  if (!import.meta.env.DEV) return;
  if (typeof window === "undefined") return;
  // Avoid double-init on Vite HMR.
  if ((window as unknown as { __omniAxeBooted?: boolean }).__omniAxeBooted) {
    return;
  }
  (window as unknown as { __omniAxeBooted?: boolean }).__omniAxeBooted = true;

  try {
    const [{ default: React }, ReactDOM, { default: axe }] =
      await Promise.all([
        import("react"),
        import("react-dom"),
        import("@axe-core/react"),
      ]);
    // 1s debounce — `@axe-core/react` calls axe.run after React commits.
    // Restrict to WCAG 2.1 A + AA (best-practice rules add noise that
    // judges won't care about for a hackathon demo).
    // `@axe-core/react` widens its Spec type for runOnly[].
    await axe(React, ReactDOM, 1000, {
      runOnly: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"],
    });
    // eslint-disable-next-line no-console
    console.info(
      "[a11y] axe-core enabled (DEV only). Violations log to the console.",
    );
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn("[a11y] axe-core failed to initialise:", err);
  }
}
