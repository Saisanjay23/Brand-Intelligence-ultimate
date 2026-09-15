// What a card asks the network for, and — more importantly — what it
// refuses to ask for.
//
// A blank card used to cost TWO doomed round trips before it drew anything:
// the proxy (which dragged our own server through a CDN fetch to collect a
// 403) and then the CDN itself (403 again). On a grid where a third of the
// cards had expired links that is ~66 pointless requests per page, and it
// is what made scrolling feel slow.
import { describe, expect, it } from "vitest";
import { avatarSources } from "./avatar";

const DEAD = Math.floor((Date.now() - 86_400_000) / 1000).toString(16);
const ALIVE = Math.floor((Date.now() + 86_400_000) / 1000).toString(16);
const fb = (oe: string) => `https://scontent.fblr8-1.fna.fbcdn.net/v/a.jpg?_nc_ht=x&oe=${oe}`;

describe("a stored copy is always preferred", () => {
  it("puts our own store first and never expires", () => {
    const out = avatarSources(fb(DEAD), "abc123");
    expect(out[0]).toContain("/media/avatar/abc123");
  });

  it("still lists the CDN behind it, in case the store is unreachable", () => {
    expect(avatarSources(fb(ALIVE), "abc123").length).toBeGreaterThan(1);
  });
});

describe("an expired link is not requested at all", () => {
  it("asks for nothing, so the fallback draws immediately", () => {
    expect(avatarSources(fb(DEAD))).toEqual([]);
  });

  it("but a live link IS tried, through the proxy so the bytes can be kept", () => {
    const out = avatarSources(fb(ALIVE));
    expect(out.length).toBeGreaterThan(0);
    expect(out[0]).toContain("/media/avatar?url=");
  });

  it("an unsigned link has no stated lifetime, so it is always worth trying", () => {
    // Twitter and YouTube do not sign. Writing these off would blank cards
    // whose pictures are perfectly fetchable.
    expect(avatarSources("https://pbs.twimg.com/profile/a.jpg").length).toBeGreaterThan(0);
    expect(avatarSources("https://yt3.ggpht.com/a.jpg").length).toBeGreaterThan(0);
  });

  it("a malformed expiry is treated as worth trying, not as dead", () => {
    expect(avatarSources("https://x.fbcdn.net/a.jpg?oe=zzz").length).toBeGreaterThan(0);
  });
});

describe("an uncached picture goes through the proxy first", () => {
  it("so the bytes pass somewhere that can save them", () => {
    // Loading direct from the CDN is faster and loses the picture: the
    // browser gets the bytes, the server never does.
    const out = avatarSources(fb(ALIVE));
    expect(out[0]).toContain("/media/avatar?url=");
    expect(out[1]).toBe(fb(ALIVE));
  });

  it("but once cached, neither the CDN nor the proxy is touched first", () => {
    const out = avatarSources(fb(ALIVE), "sha1");
    expect(out[0]).toContain("/media/avatar/sha1");
  });
});
