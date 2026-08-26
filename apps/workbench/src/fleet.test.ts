import { describe, expect, it } from "vitest";
import { apiErrorMessage, imageJobLog, suggestContainerName } from "./fleet";
describe("fleet UI helpers", () => {
  it("suggests an unused editable name", () => expect(suggestContainerName(["workstation-1", "workstation-2"])).toBe("workstation-3"));
  it("prefers live job logs", () => expect(imageJobLog({ logs: ["one", "two"], message: "done" })).toBe("one\ntwo"));
  it("shows actionable API detail and correlation reference", () => {
    expect(apiErrorMessage(403, { detail: "CSRF validation failed" })).toBe("CSRF validation failed (HTTP 403)");
    expect(apiErrorMessage(503, { message: "Docker unavailable", correlation_id: "abc123" })).toBe("Docker unavailable (HTTP 503, reference abc123)");
  });
});
