import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { AuditDrawer } from "./components/AuditDrawer";
import "./styles/app.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
    <AuditDrawer />
  </React.StrictMode>,
);
