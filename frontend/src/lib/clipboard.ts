/**
 * Clipboard helper with a synchronous DOM fallback.
 *
 * The async `navigator.clipboard.writeText` API can:
 *   - reject (denied permission, expired transient activation, …);
 *   - be entirely undefined (older Safari, non-HTTPS dev origin,
 *     in-app webviews).
 *
 * Without a fallback, callers silently lose the copy. This helper
 * tries the modern API first, then falls back to a hidden `<textarea>`
 * + `document.execCommand("copy")` — which is deprecated but still
 * works everywhere we care about.
 *
 * Returns `true` if at least one method reported success.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (
      typeof navigator !== "undefined" &&
      navigator.clipboard &&
      typeof navigator.clipboard.writeText === "function"
    ) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Fall through to the legacy path.
  }

  if (typeof document === "undefined") return false;

  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "absolute";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

/**
 * Convenience: copy `text` and surface a Vietnamese toast via the
 * existing `omni:toast` window event bus that `<ToastStack />`
 * subscribes to.
 *
 * `successTitle` lets call sites distinguish "Đã sao chép STK" from
 * "Đã sao chép nội dung" without each one wiring its own toast.
 */
export async function copyTextWithToast(
  text: string,
  successTitle: string = "Đã sao chép",
): Promise<boolean> {
  const ok = await copyText(text);
  try {
    const detail = {
      kind: "transfer_success" as const,
      title: ok ? successTitle : "Không sao chép được",
      body: ok ? text : "Vui lòng thử lại.",
      severity: (ok ? "success" : "error") as "success" | "error",
      ts: Date.now(),
    };
    window.dispatchEvent(new CustomEvent("omni:toast", { detail }));
  } catch {
    /* no toast plumbing in this environment — silent. */
  }
  return ok;
}
