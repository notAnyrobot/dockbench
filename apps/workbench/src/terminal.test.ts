import { describe, expect, it } from "vitest";
import { closeTerminalSession } from "./terminal";

describe("terminal session lifecycle", () => {
  it("closes and disposes the active tab exactly once", () => {
    let closes = 0, disposals = 0;
    const session = { id: "one", socket: { close: () => { closes += 1; } }, terminal: { dispose: () => { disposals += 1; } } };
    const sessions = new Map([[session.id, session]]);
    expect(closeTerminalSession(sessions, session)).toBe(true);
    expect([closes, disposals, sessions.size]).toEqual([1, 1, 0]);
  });

  it("does not let a stale callback dispose a replacement tab", () => {
    const stale = { id: "one", socket: { close: () => { throw new Error("closed stale"); } }, terminal: { dispose: () => { throw new Error("disposed stale"); } } };
    const current = { id: "one", socket: { close: () => undefined }, terminal: { dispose: () => undefined } };
    const sessions = new Map([[current.id, current]]);
    expect(closeTerminalSession(sessions, stale)).toBe(false);
    expect(sessions.get("one")).toBe(current);
  });
});
