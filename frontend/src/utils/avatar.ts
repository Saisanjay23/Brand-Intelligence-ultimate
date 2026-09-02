// Where an `<img>` should try to get a profile picture from, in order.
//
// THE PROBLEM. Instagram serves its avatars with the response header
// `Cross-Origin-Resource-Policy: same-origin`. CORP is enforced by the
// BROWSER, not the server, so the URL looks perfectly healthy from every
// angle you would normally check it: curl and the scrapers get a clean 200
// with real JPEG bytes, and pasting it into a tab renders it (a top-level
// navigation is not a cross-origin embed). Chrome fetches it, sees the
// header, discards the bytes before they reach the `<img>`, and fires
// `onerror` with no status and nothing in the console -- so the card falls
// back to its initial-letter circle and the profile reads as having no
// picture at all. No client-side flag lifts CORP: not `referrerPolicy`, not
// `crossOrigin`, not a CSS background, not `fetch` in any mode.
//
// THE FIX is to fetch the image server-side, where CORP has no meaning, and
// re-serve it from an origin the page is allowed to embed. That is
// backend/api/media.py.
//
// WHY A LIST, RATHER THAN ALWAYS PROXYING. Measured against the live CDNs,
// only Instagram sends `same-origin` today -- Facebook, Twitter and YouTube
// all send `cross-origin` and load directly just fine. Routing those
// through the proxy anyway is not free and not safe: it adds a hop, throws
// away CDN caching, and (as this file's first version proved the hard way)
// puts every platform's avatars behind one piece of our own code that can
// break them all at once. So:
//
//   - hosts KNOWN to be blocked  -> go straight to the proxy, no wasted
//                                   round trip on a request that cannot work
//   - everything else            -> try the CDN directly first, and fall
//                                   back to the proxy only if that fails
//
// The fallback is what makes this hold up over time. Which header Meta,
// Google or Twitter attach is their decision, not ours, and it can change
// without warning. Any platform that starts sending `same-origin` recovers
// on its own through the second entry in this list, with no code change --
// while a platform that never breaks never pays for the proxy.
import { url } from "../api/httpClient";

// Hosts the backend's own allowlist will accept (backend/api/media.py).
// Producing a proxy URL for anything else would just trade a broken image
// for a guaranteed 400, so those hosts get a direct attempt only.
const PROXYABLE_HOST_SUFFIXES = [
  ".fbcdn.net",
  ".cdninstagram.com",
  ".twimg.com",
  ".ggpht.com",
  ".googleusercontent.com",
  ".licdn.com",
  ".tiktokcdn.com",
  ".tiktokcdn-us.com",
];

// Instagram specifically. Its avatars come off the shared Meta CDN under an
// `instagram.*` hostname (e.g. instagram.fblr8-1.fna.fbcdn.net) or off
// cdninstagram.com -- note that Facebook's own pictures share `.fbcdn.net`
// under `scontent.*` and are NOT blocked, so this cannot key on the domain
// alone.
function isKnownBlocked(host: string): boolean {
  return host.startsWith("instagram.") || host === "cdninstagram.com" || host.endsWith(".cdninstagram.com");
}

function matches(host: string, suffixes: string[]): boolean {
  return suffixes.some((s) => host === s.slice(1) || host.endsWith(s));
}

/** Candidate URLs for this avatar, best first. Empty when there is no picture. */
export function avatarSources(raw: string | null | undefined): string[] {
  if (!raw) return [];
  // Telegram stores the picture itself rather than a link to one.
  if (raw.startsWith("data:")) return [raw];

  let host: string;
  try {
    host = new URL(raw).hostname.toLowerCase();
  } catch {
    // Not something we can reason about (relative, or malformed) -- hand it
    // to the browser unchanged rather than routing it at the proxy.
    return [raw];
  }

  const proxied = url(`/media/avatar?url=${encodeURIComponent(raw)}`);
  if (isKnownBlocked(host)) return [proxied];
  if (matches(host, PROXYABLE_HOST_SUFFIXES)) return [raw, proxied];
  return [raw];
}
