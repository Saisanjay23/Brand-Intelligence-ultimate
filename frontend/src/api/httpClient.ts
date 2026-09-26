// Shared low-level HTTP plumbing every api/*Api.ts module builds on: where
// the backend lives, and the fetch/error-shape boilerplate every call needs.
//
// No Authorization header is attached today, this backend has no auth
// layer (see backend/docs/adr/0005-no-auth-layer.md), but this is the one
// place it would be added if that ever changes, so every API module picks
// it up for free instead of each hand-rolling its own fetch wrapper.
//
// Empty by default (same-origin relative calls); set VITE_API_BASE_URL to
// point this app at a backend hosted elsewhere. Trailing slash stripped so
// `${API_BASE}/clients` never doubles up.
export const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/+$/, "");

export const url = (path: string) => `${API_BASE}${path}`;

// The error text for a failed response. FastAPI's 422 carries `detail` as a
// LIST of {loc, msg} objects, not a string, and `new Error(list)` rendered
// as "[object Object]" -- so a rejected form told the analyst nothing.
export function errorDetail(d: unknown, status: number): string {
  const detail = (d as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail
      .map((e) => {
        const { loc, msg } = (e ?? {}) as { loc?: unknown[]; msg?: string };
        const field = Array.isArray(loc) ? loc.filter((p) => p !== "body").join(".") : "";
        return field ? `${field}: ${msg ?? "invalid"}` : (msg ?? String(e));
      })
      .join("; ");
  }
  return `request failed (${status})`;
}

async function failure(res: Response): Promise<never> {
  const d = await res.json().catch(() => ({ detail: res.statusText }));
  throw new Error(errorDetail(d, res.status));
}

export async function json<T>(res: Response): Promise<T> {
  if (!res.ok) await failure(res);
  return res.json() as Promise<T>;
}

export const post = (path: string, body: unknown) =>
  fetch(url(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

// Same error-shape handling as `json<T>`, but for an endpoint whose success
// body is a binary file (e.g. a generated .xlsx) rather than JSON.
export async function blob(res: Response): Promise<Blob> {
  if (!res.ok) await failure(res);
  return res.blob();
}
