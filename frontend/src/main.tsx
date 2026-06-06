import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { ErrorBoundary } from "./components/ErrorBoundary";
import "./styles/app.css";

// DEV-only: run axe-core over the rendered app so violations land in
// the browser console while the team is iterating. Stripped from the
// production bundle by Vite via the `import.meta.env.DEV` guard.
if (import.meta.env.DEV) {
  import("./lib/axe").then((m) => m.bootAxe());
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
);

// Register the PWA service worker (only on secure contexts: https / localhost).
if ("serviceWorker" in navigator && window.isSecureContext) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      /* ignore — app still works without offline support */
    });
  });
}
