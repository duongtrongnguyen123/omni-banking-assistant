import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const apiTarget = env.VITE_API_PROXY_TARGET || "http://localhost:8000";
  const wsTarget = env.VITE_WS_PROXY_TARGET || apiTarget.replace(/^http/, "ws");

  return {
    plugins: [react()],
    server: {
      port: 5173,
      host: true, // listen on all interfaces (LAN + tunnel)
      // Cloudflare quick-tunnel / ngrok / generic — combine main's
      // permissive allowlist with the documented explicit suffixes so
      // judges and teammates can hit the dev server from outside the
      // LAN without bumping config per session.
      allowedHosts: true,
      proxy: {
        "/api": apiTarget,
        "/health": apiTarget,
        "/ws": { target: wsTarget, ws: true },
      },
    },
  };
});
