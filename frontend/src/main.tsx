import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles/app.css";

// DEV-only: run axe-core over the rendered app so violations land in
// the browser console while the team is iterating. Stripped from the
// production bundle by Vite via the `import.meta.env.DEV` guard.
if (import.meta.env.DEV) {
  import("./lib/axe").then((m) => m.bootAxe());
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
