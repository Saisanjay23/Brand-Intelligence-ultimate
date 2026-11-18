import { describe, expect, it } from "vitest";
import { actionFailure } from "./SessionPanel";

// "Verify Sweep Now" and "Launch Login" report failure inside a 200 body.
// If this reads them as success, the button looks like it worked.
describe("actionFailure", () => {
  it("surfaces a failed check's detail", () => {
    expect(actionFailure({ ok: false, detail: "expired -- log in again" })).toBe("expired -- log in again");
  });

  it("surfaces a timed-out or failed interactive login", () => {
    expect(actionFailure({ status: "timeout", message: "no login within the time limit" })).toBe(
      "no login within the time limit",
    );
    expect(actionFailure({ status: "failed", message: "" })).toBe("login failed");
  });

  it("treats success and plain pool responses as no failure", () => {
    expect(actionFailure({ ok: true, detail: "fine" })).toBe("");
    expect(actionFailure({ status: "saved", message: "12 cookies saved" })).toBe("");
    expect(actionFailure({ platform: "facebook", state: "ready", sessions: [] })).toBe("");
    expect(actionFailure(undefined)).toBe("");
  });
});
