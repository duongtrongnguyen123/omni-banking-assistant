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
      // Allow Cloudflare quick-tunnel / ngrok hosts so judges and
      // teammates can hit the dev server from outside the LAN without
      // bumping config per session. The default-deny is for SSRF
      // protection — these public tunnel domains are safe to whitelist.
      allowedHosts: [".trycloudflare.com", ".ngrok-free.app", ".ngrok.io"],
      proxy: {
        "/api": apiTarget,
        "/health": apiTarget,
        "/ws": { target: wsTarget, ws: true },
      },
    },
  };
});
