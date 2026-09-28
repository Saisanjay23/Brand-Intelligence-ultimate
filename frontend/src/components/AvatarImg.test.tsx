/**
 * A stored avatar is offered at several widths so the browser downloads the
 * smallest one that still covers what it paints. The original stays `src`
 * and stays the widest candidate, and nothing changes for a picture with no
 * stored copy or a call site that does not say how big it is painted.
 */

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { storedSrcSet } from "../utils/avatar";
import { AvatarImg } from "./AvatarImg";

const SHA = "a".repeat(64);

describe("storedSrcSet", () => {
  it("offers resized copies and the original as the widest", () => {
    const set = storedSrcSet(SHA);
    expect(set).toContain(`/media/avatar/${SHA}?w=128 128w`);
    expect(set).toContain(`/media/avatar/${SHA}?w=1024 1024w`);
    expect(set.endsWith(`/media/avatar/${SHA} 2048w`)).toBe(true);
  });
});

describe("AvatarImg", () => {
  it("adds srcset only for the stored copy, when told the painted size", () => {
    const { container } = render(<AvatarImg src="https://pbs.twimg.com/a.jpg" sha={SHA} sizes="26px" />);
    const img = container.querySelector("img")!;
    expect(img.getAttribute("src")).toContain(`/media/avatar/${SHA}`);
    expect(img.getAttribute("src")).not.toContain("?w=");
    expect(img.getAttribute("srcset")).toBe(storedSrcSet(SHA));
    expect(img.getAttribute("sizes")).toBe("26px");
  });

  it("is unchanged without `sizes`", () => {
    const { container } = render(<AvatarImg src="https://pbs.twimg.com/a.jpg" sha={SHA} />);
    expect(container.querySelector("img")!.hasAttribute("srcset")).toBe(false);
  });

  it("is unchanged when there is no stored copy", () => {
    const { container } = render(<AvatarImg src="https://pbs.twimg.com/a.jpg" sizes="26px" />);
    expect(container.querySelector("img")!.hasAttribute("srcset")).toBe(false);
  });
});
