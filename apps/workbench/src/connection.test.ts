import { describe, expect, it } from "vitest";
import { applyStatusToDesktop, desktopButtonLabel, disconnectState, fullscreenButtonLabel, passwordForConnection, workstationActionEnabled, workstationButtonLabel } from "./connection";

describe("desktop connection state", () => {
  it("keeps a connected or connecting canvas visible while status refreshes", () => {
    expect(applyStatusToDesktop("connected")).toBe(false);
    expect(applyStatusToDesktop("connecting")).toBe(false);
    expect(applyStatusToDesktop("running")).toBe(true);
  });

  it("retains a page-session password without retaining the input value", () => {
    expect(passwordForConnection("", "memory-only")).toBe("memory-only");
    expect(passwordForConnection("replacement", "memory-only")).toBe("replacement");
  });

  it("does not overwrite an authentication failure with disconnect", () => {
    expect(disconnectState(true, false)).toBeNull();
    expect(disconnectState(false, true)).toBe("stopped");
  });

  it("turns the desktop action into close while connected", () => {
    expect(desktopButtonLabel("running")).toBe("Open desktop");
    expect(desktopButtonLabel("connected")).toBe("Close desktop");
  });

  it("turns the workstation action into stop while running", () => {
    expect(workstationButtonLabel(false)).toBe("Start workstation");
    expect(workstationButtonLabel(true)).toBe("Stop workstation");
  });

  it("keeps Start available for a stopped managed container but not an absent one", () => {
    expect(workstationActionEnabled("stopped")).toBe(true);
    expect(workstationActionEnabled("absent")).toBe(false);
  });

  it("turns the fullscreen action into exit while active", () => {
    expect(fullscreenButtonLabel(false)).toBe("Enter fullscreen");
    expect(fullscreenButtonLabel(true)).toBe("Exit fullscreen");
  });
});
