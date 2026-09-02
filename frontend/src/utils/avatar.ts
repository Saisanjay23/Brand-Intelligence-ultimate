// Where an `<img>` should actually point to show a profile picture.
//
// Instagram's CDN returns its avatars with `Cross-Origin-Resource-Policy:
// same-origin`. The browser fetches the image, gets a perfectly good 200
// with real JPEG bytes, then discards it because of that header and fires
// `onerror` -- so every Instagram card fell back to its initial-letter
// circle and looked like it had no picture at all. The bug is invisible
// from the server side: curl and the scrapers see the same URL as healthy,
// and pasting it into a tab renders fine (a top-level navigation is not a
// cross-origin embed, so CORP does not apply).
//
// No client-side flag lifts CORP -- not `referrerPolicy`, not `crossOrigin`,
// not a CSS background. The image has to come from an origin the page is
// allowed to embed, so Meta-hosted avatars are routed through our own
// backend, which fetches them server-side where CORP has no meaning. See
// backend/api/media.py for the endpoint and its SSRF allowlist.
//
// Everything else is passed through untouched and keeps loading straight
// from its CDN: Twitter (pbs.twimg.com) and YouTube (yt3.ggpht.com) both
// send `cross-origin` and always worked, and Telegram inlines a `data:` URI
// that never touches the network. Proxying those would only add a hop and
// throw away CDN caching.
import { url } from "../api/httpClient";

// Matched against the parsed hostname, never the raw URL text -- a
// substring test on the string would accept `https://evil.com/?x=.fbcdn.net`.
// The backend allowlist is the real security boundary; this one only decides
// which URLs are worth sending there.
const PROXIED_HOST_SUFFIXES = [".fbcdn.net", ".cdninstagram.com"];

export function avatarSrc(raw: string | null | undefined): string {
  if (!raw) return "";
  // Telegram stores the picture itself, not a link to one.
  if (raw.startsWith("data:")) return raw;

  let host: string;
  try {
    host = new URL(raw).hostname.toLowerCase();
  } catch {
    // Not a URL we can reason about (a relative path, or malformed) -- hand
    // it back unchanged rather than routing something unparsed at the proxy.
    return raw;
  }

  const proxied = PROXIED_HOST_SUFFIXES.some(
    (suffix) => host === suffix.slice(1) || host.endsWith(suffix),
  );
  return proxied ? url(`/media/avatar?url=${encodeURIComponent(raw)}`) : raw;
}
