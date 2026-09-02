/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Relative paths so the bundle can be served from any origin (see
// src/api/httpClient.ts's API_BASE) -- this dev proxy is local convenience
// only, not what makes the app work. /discovery, /analysis, /sessions,
// /health and /media are real backend routers on the rebuilt backend
// (backend/api/{discovery,analysis,sessions,health,media}.py). /clients, /jobs,
// /scheduler are NOT -- the old frontend pages that call them (Clients,
// Scheduler) were restored for their UI/layout, but that backend layer
// (client_routes.py/job_routes.py/scheduler_routes.py + controllers/dto)
// was not rebuilt alongside them, so those three stay proxied here only so
// a request reaches the real backend's clean 404 JSON instead of falling
// through to Vite's SPA fallback and getting index.html back ("Unexpected
// token '<'... is not valid JSON") -- a clear error toast, not a crash.
const BACKEND = "http://127.0.0.1:8000";
const proxy = Object.fromEntries(
  [
    "/discovery", "/analysis", "/sessions", "/health", "/media",
    "/clients", "/jobs", "/scheduler",
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
