// API calls for the backend's sessions module (backend/api/session_routes.py).
// One call per platform id (facebook/twitter/instagram/youtube/telegram),
// the backend has no per-platform route, `platform` is always a parameter
// here, not a separate resource, so this one file covers every platform's
// session/credential management rather than one file each.
import { json, post, url } from "./httpClient";
import type { SessionInfo } from "./types";

/** What POST /sessions/proxy/test reports back. `ok` false means traffic did
 * NOT actually egress through the proxy -- see `warnings`, which is ordered
 * most-severe first and is written to be shown verbatim. */
export interface ProxyTestResult {
  ok: boolean;
  exit_ip?: string | null;
  direct_ip?: string | null;
  country?: string | null;
  country_code?: string | null;
  city?: string | null;
  timezone?: string | null;
  isp?: string | null;
  org?: string | null;
  /** true = hosting range (loud), false = residential/mobile (quiet), null = unknown */
  is_datacenter?: boolean | null;
  is_known_proxy?: boolean | null;
  is_mobile?: boolean | null;
  latency_ms?: number;
  intel_source?: string | null;
  warnings?: string[];
  error?: string;
}

export const sessionsApi = {
  // Every platform's pool in one request. Preferred over fanning
  // sessionStatus out across the platform list: it's one round trip instead
  // of six per poll, and every platform in the response is read against the
  // same snapshot of the server's health cache, so they can't disagree with
  // each other about a sweep that landed mid-fan-out.
  allSessionStatus: () => fetch(url("/sessions")).then(json<{ items: SessionInfo[] }>),
  sessionStatus: (platform: string) => fetch(url(`/sessions/${platform}`)).then(json<SessionInfo>),
  saveCookies: (platform: string, blob: string, identifier = "") =>
    post(`/sessions/${platform}/cookies`, { blob, identifier }).then(json<SessionInfo>),
  saveCredentials: (platform: string, data: { username: string; password: string; two_factor_secret?: string; identifier?: string }) =>
    post(`/sessions/${platform}/credentials`, data).then(json<SessionInfo>),
  saveApiKey: (platform: string, key: string, identifier = "") =>
    post(`/sessions/${platform}/api-key`, { key, identifier }).then(json<SessionInfo>),
  updateSessionItem: (
    platform: string,
    sessionId: string,
    body: { blob?: string; api_key?: string; identifier?: string },
  ) =>
    fetch(url(`/sessions/${platform}/${sessionId}`), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(json<SessionInfo>),
  launchLogin: (platform: string, timeoutS = 300, identifier = "") =>
    post(`/sessions/${platform}/login`, { timeout_s: timeoutS, identifier }).then(
      json<{ platform: string; status: string; message: string; started: string; finished: string }>,
    ),
  checkSessionNow: (platform: string) =>
    post(`/sessions/${platform}/check`, {}).then(json<{ ok: boolean; detail: string }>),
  // Verify ONE named account rather than whichever the sweep considers most
  // overdue, what you want right after re-pasting cookies. Returns the
  // refreshed pool alongside the verdict.
  checkSessionItem: (platform: string, sessionId: string) =>
    post(`/sessions/${platform}/${sessionId}/check`, {}).then(
      json<{ ok: boolean; detail: string; conclusive: boolean; session: SessionInfo }>,
    ),
  setSessionProxy: (
    platform: string,
    sessionId: string,
    proxy: { server: string; username?: string; password?: string; timezone_id?: string },
  ) =>
    fetch(url(`/sessions/${platform}/${sessionId}/proxy`), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ proxy }),
    }).then(json<SessionInfo>),
  // Checks a proxy BEFORE it is attached to anything: the backend starts a
  // throwaway browser through it (and one without it, to compare) and
  // reports the address the world actually sees. Slow by API standards --
  // it is launching real browsers -- so callers should show a spinner.
  testProxy: (proxy: { server: string; username?: string; password?: string }) =>
    fetch(url(`/sessions/proxy/test`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ proxy }),
    }).then(json<ProxyTestResult>),
  // Backend has no separate DELETE-proxy route, clearing is PUT with
  // proxy: null (see backend/controllers/session_controller.py::set_proxy).
  clearSessionProxy: (platform: string, sessionId: string) =>
    fetch(url(`/sessions/${platform}/${sessionId}/proxy`), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ proxy: null }),
    }).then(json<SessionInfo>),
  deleteSessionItem: (platform: string, sessionId: string) =>
    fetch(url(`/sessions/${platform}/${sessionId}`), { method: "DELETE" }).then(json<SessionInfo>),
  deleteSessionPool: (platform: string) =>
    fetch(url(`/sessions/${platform}`), { method: "DELETE" }).then(json<SessionInfo>),

  // Telegram's MTProto login is multi-step (code, then optionally a 2FA
  // password) so it can't reuse launchLogin's single-shot headful-browser
  // shape, see backend/services/telegram_login_service.py.
  telegramLoginStart: (apiId: number, apiHash: string, phone: string) =>
    post("/sessions/telegram/login/start", { api_id: apiId, api_hash: apiHash, phone }).then(
      json<{ status: string; phone: string }>,
    ),
  telegramLoginCode: (code: string) =>
    post("/sessions/telegram/login/code", { code }).then(
      json<{ status: "need_password" | "saved"; message?: string }>,
    ),
  telegramLoginPassword: (password: string) =>
    post("/sessions/telegram/login/password", { password }).then(
      json<{ status: "saved"; message: string }>,
    ),
  telegramLoginCancel: () =>
    post("/sessions/telegram/login/cancel", {}).then(json<{ status: string }>),
};
