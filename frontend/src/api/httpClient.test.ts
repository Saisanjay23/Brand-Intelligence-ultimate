/**
 * A rejected request must say WHY. FastAPI answers a validation failure
 * (422) with `detail` as a list of {loc, msg} objects, and wrapping that list
 * in `new Error(...)` put "[object Object]" in the analyst's toast.
 */

import { describe, expect, it } from "vitest";

import { errorDetail, json } from "./httpClient";

describe("errorDetail", () => {
  it("passes a plain string detail through", () => {
    expect(errorDetail({ detail: "no validated profiles to analyse" }, 422)).toBe(
      "no validated profiles to analyse",
    );
  });

  it("names each field of a 422 validation list", () => {
    const d = {
      detail: [
        { loc: ["body", "group_id"], msg: "Field required", type: "missing" },
        { loc: ["query", "limit"], msg: "Input should be less than or equal to 1000" },
      ],
    };
    expect(errorDetail(d, 422)).toBe(
      "group_id: Field required; query.limit: Input should be less than or equal to 1000",
    );
  });

  it("falls back to the status when there is nothing to say", () => {
    expect(errorDetail({}, 502)).toBe("request failed (502)");
    expect(errorDetail(null, 500)).toBe("request failed (500)");
  });
});

describe("json", () => {
  it("throws the readable message, never [object Object]", async () => {
    const res = new Response(
      JSON.stringify({ detail: [{ loc: ["body", "urls"], msg: "List should have at least 1 item" }] }),
      { status: 422 },
    );
    await expect(json(res)).rejects.toThrow("urls: List should have at least 1 item");
  });
});
