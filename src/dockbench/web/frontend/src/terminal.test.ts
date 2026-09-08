import { describe, expect, it } from "vitest";
import { closeTerminalSession, resizeTerminalOnSocketOpen, restoreTerminalSurface } from "./terminal";

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

describe("terminal surface restoration", () => {
  it("waits for the surface to mount before sending its initial dimensions", () => {
    const messages: string[] = [];
    const session = {
      opened: false,
      socket: { readyState: 1, send: (value: string) => { messages.push(value); } },
      fit: { fit: () => { throw new Error("must not fit before mount"); } },
      terminal: { cols: 80, rows: 24, refresh: () => undefined, focus: () => undefined },
    };

    expect(resizeTerminalOnSocketOpen(session, 1)).toBe(false);
    expect(messages).toEqual([]);
  });

  it("fits a mounted surface before sending its initial dimensions", () => {
    const messages: string[] = [];
    const session = {
      opened: true,
      socket: { readyState: 1, send: (value: string) => { messages.push(value); } },
      fit: { fit: () => { session.terminal.cols = 132; session.terminal.rows = 43; } },
      terminal: { cols: 80, rows: 24, refresh: () => undefined, focus: () => undefined },
    };

    expect(resizeTerminalOnSocketOpen(session, 1)).toBe(true);
    expect(messages).toEqual([JSON.stringify({ type: "resize", cols: 132, rows: 43 })]);
  });

  it("refits, repaints, focuses, and reports dimensions after becoming visible", () => {
    const events: string[] = [];
    const messages: string[] = [];
    const session = {
      socket: { readyState: 1, send: (value: string) => { messages.push(value); } },
      fit: { fit: () => { events.push("fit"); } },
      terminal: {
        cols: 132,
        rows: 43,
        refresh: (start: number, end: number) => { events.push(`refresh:${start}:${end}`); },
        focus: () => { events.push("focus"); },
      },
    };

    restoreTerminalSurface(session, 1);

    expect(events).toEqual(["fit", "refresh:0:42", "focus"]);
    expect(messages).toEqual([JSON.stringify({ type: "resize", cols: 132, rows: 43 })]);
  });

  it("does not send a resize through a closed socket", () => {
    const messages: string[] = [];
    const session = {
      socket: { readyState: 3, send: (value: string) => { messages.push(value); } },
      fit: { fit: () => undefined },
      terminal: { cols: 80, rows: 24, refresh: () => undefined, focus: () => undefined },
    };

    restoreTerminalSurface(session, 1);

    expect(messages).toEqual([]);
  });
});
