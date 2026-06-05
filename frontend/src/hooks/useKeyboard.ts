import { useEffect } from "react";

/**
 * Global keyboard shortcut bindings for power users.
 *
 * The shortcuts are scoped so they don't fight with regular typing:
 *  - Cmd/Ctrl+K, Cmd/Ctrl+/, Cmd/Ctrl+B, Cmd/Ctrl+Enter fire anywhere
 *    (they all use a modifier, so the input still receives Cmd-A etc.).
 *  - Esc and ArrowUp behave differently depending on whether the chat
 *    input is focused — the handler inspects `document.activeElement`.
 */
export interface KeyboardHandlers {
  /** Focus the chat input. */
  onFocusInput: () => void;
  /** Re-send the last user message. */
  onResendLast: () => void;
  /** Toggle the floating balance peek. */
  onToggleBalance: () => void;
  /** Close any open modal / popover. Returns true if something was closed. */
  onEscape: () => boolean;
  /** Open the slash palette by inserting a "/" at the start of input. */
  onOpenSlash: () => void;
  /** When input is empty, cycle to the previous user message in history. */
  onPrevHistory: () => void;
  /** Mirror of onPrevHistory for ArrowDown. */
  onNextHistory: () => void;
  /** Clear the chat input. */
  onClearInput: () => void;
  /** Whether the input currently has the empty value (for ↑ history nav). */
  isInputEmpty: () => boolean;
  /** Read the id of the currently-focused element. */
  inputId: string;
}

const isMod = (e: KeyboardEvent) => e.metaKey || e.ctrlKey;

export const useKeyboard = (h: KeyboardHandlers) => {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const inInput =
        target?.tagName === "INPUT" ||
        target?.tagName === "TEXTAREA" ||
        target?.isContentEditable === true;
      const inOurInput =
        inInput &&
        (target as HTMLInputElement | HTMLTextAreaElement).id === h.inputId;

      // Mod-prefixed shortcuts: fire anywhere.
      if (isMod(e) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        h.onFocusInput();
        return;
      }
      if (isMod(e) && e.key === "Enter") {
        e.preventDefault();
        h.onResendLast();
        return;
      }
      if (isMod(e) && e.key.toLowerCase() === "b") {
        e.preventDefault();
        h.onToggleBalance();
        return;
      }
      if (isMod(e) && e.key === "/") {
        e.preventDefault();
        h.onOpenSlash();
        return;
      }

      // Esc: prefer closing modals; fall back to clearing input when focused.
      if (e.key === "Escape") {
        const closed = h.onEscape();
        if (closed) {
          e.preventDefault();
          return;
        }
        if (inOurInput) {
          e.preventDefault();
          h.onClearInput();
        }
        return;
      }

      // History navigation: only when our input is focused AND empty (so
      // the user can still use arrow keys to move the caret normally).
      if (inOurInput && h.isInputEmpty()) {
        if (e.key === "ArrowUp") {
          // Let the slash palette / autocomplete handle their own ArrowUp
          // first. They use capture-phase listeners with preventDefault,
          // so if we get here they aren't open.
          e.preventDefault();
          h.onPrevHistory();
          return;
        }
        if (e.key === "ArrowDown") {
          e.preventDefault();
          h.onNextHistory();
          return;
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [h]);
};
