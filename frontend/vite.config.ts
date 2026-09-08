/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Relative paths so the bundle can be served from any origin (see
// src/api/httpClient.ts's API_BASE) -- this dev proxy is local convenience
// only, not what makes the app work. /clients, /discovery, /analysis,
// /sessions, /health and /media are all real backend routers
// (backend/api/{clients,discovery,analysis,sessions,health,media}.py).
//
// /jobs is NOT -- the old frontend page that calls it was restored for its
// UI/layout, but job_routes.py was not rebuilt alongside it, so it stays
// proxied here only so a request reaches the real backend's clean 404 JSON
// instead of falling through to Vite's SPA fallback and getting index.html
// back ("Unexpected token '<'... is not valid JSON") -- a clear error
// toast, not a crash.
//
// /scheduler is gone from this list entirely: nothing calls it any more. The
// Scheduler tab sequences its own queue over POST /discovery/jobs (see
// services/scheduleRunner.ts), and Live Activity's Client Coverage tab reads
// that same store instead of the /scheduler/status that never existed here.
//
// Overridable so a second backend (a branch, a test instance on another
// port) can be pointed at without editing this file and forgetting to put
// it back: BACKEND_ORIGIN=http://127.0.0.1:8001 npm run dev
const BACKEND = process.env.BACKEND_ORIGIN || "http://127.0.0.1:8000";
const proxy = Object.fromEntries(
  [
    "/discovery", "/analysis", "/sessions", "/health", "/media",
    "/clients", "/jobs", "/reports", "/alerts",
    "/docs", "/redoc", "/openapi.json",
  ].map((path) => [path, { target: BACKEND, changeOrigin: true }]),
);

export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    port: 5173,
    proxy,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    // e2e/ is Playwright's (a different `test`/`expect` API entirely) --
    // without this exclusion Vitest tries to parse it too and fails
    exclude: ["e2e/**", "node_modules/**"],
  },
});
