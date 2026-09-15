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
  ".ytimg.com",
  ".licdn.com",
  ".tiktokcdn.com",
  ".tiktokcdn-us.com",
  ".t.me",
  ".telegram.org",
  ".telesco.pe",
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

/**
 * Candidate URLs for this avatar, best first. Empty when there is no picture.
 *
 * `sha` is the digest of the bytes the backend already pulled down and kept
 * (a profile's `avatar_sha`). When it is set it goes FIRST, ahead of the CDN
 * itself, because it is the only candidate that does not expire: Meta signs
 * its picture URLs and the signature dies within hours, which is what made a
 * card look right when it was discovered and blank by the next morning. The
 * live CDN URL stays in the list behind it, so a profile whose bytes have not
 * been cached yet -- caching runs behind the sweep -- still shows a picture.
 */
// Meta stamps a signed URL's own expiry into it, as a hex unix timestamp.
// Mirrors backend/shared/imagefetch.py::signed_expiry -- deliberately, so
// both sides agree on when a link is dead.
const OE = /[?&]oe=([0-9a-fA-F]{1,16})/;

function isDeadLink(raw: string): boolean {
  const m = OE.exec(raw);
  if (!m) return false;            // unsigned: no stated lifetime, worth trying
  const expiry = parseInt(m[1], 16);
  return Number.isFinite(expiry) && expiry * 1000 < Date.now();
}

export function avatarSources(
  raw: string | null | undefined,
  sha?: string | null,
): string[] {
  const stored = sha ? [url(`/media/avatar/${sha}`)] : [];
  if (!raw) return stored;
  // A LINK THAT IS ALREADY DEAD COSTS TWO ROUND TRIPS TO DISCOVER.
  //
  // Without this, a card with an expired Meta URL and no stored copy asks
  // the proxy (which fetches the CDN, gets 403, and answers 403), then asks
  // the CDN itself (403 again), and only then draws its fallback. Two
  // doomed requests, one of them dragging our own server through a CDN
  // round trip -- per card. On a grid where a third of the cards were blank
  // that is ~66 pointless requests a page, and it is what made scrolling
  // feel slow.
  //
  // The URL says when it died. Reading it costs a regex and saves both
  // requests, so the fallback draws immediately instead of after two
  // timeouts. Only applies when there is no stored copy: with one, `stored`
  // is first and nothing here is reached.
  if (!stored.length && isDeadLink(raw)) return [];
  // Telegram stores the picture itself rather than a link to one.
  // When we have a stored copy (sha), prefer it over the inline blob so the
  // card renders a permanent GridFS-backed URL and avoids the DOM overhead
  // of a multi-kilobyte data: URI in every card element.
  if (raw.startsWith("data:")) return [...stored, raw];

  let host: string;
  try {
    host = new URL(raw).hostname.toLowerCase();
  } catch {
    // Not something we can reason about (relative, or malformed) -- hand it
    // to the browser unchanged rather than routing it at the proxy.
    return [...stored, raw];
  }

  const proxied = url(`/media/avatar?url=${encodeURIComponent(raw)}`);
  if (isKnownBlocked(host)) return [...stored, proxied];
  if (matches(host, PROXYABLE_HOST_SUFFIXES)) {
    // NO STORED COPY YET -> GO THROUGH THE PROXY FIRST, DELIBERATELY.
    //
    // This looks like the slower option and is the only one that keeps the
    // picture. Meta signs its CDN URLs and they expire in days, so a card
    // rendering straight from the CDN looks perfect this week and is blank
    // the next -- and because the browser fetched those bytes, not us, we
    // never had the chance to save them. A third of one client's cards went
    // that way.
    //
    // The proxy stores what it serves (backend/api/media.py::_keep), so the
    // act of an analyst LOOKING at a card is what makes its picture
    // permanent. The cost is one hop, once, per picture: the next render
    // has `sha` set and takes the first branch below, straight from our own
    // store. The direct CDN URL stays as the fallback in case the proxy is
    // unreachable.
    //
    // Once cached, none of this applies -- `stored` is first and neither
    // the CDN nor the proxy is touched again.
    if (!stored.length) return [proxied, raw];
    return [...stored, raw, proxied];
  }
  return [...stored, raw];
}
