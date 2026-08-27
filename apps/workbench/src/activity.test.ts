import { describe, expect, it } from "vitest";
import { dockHeight, inspectorWidth, loadActivity, recordActivity } from "./activity";

describe("dockbench activity and layout", () => {
  it("keeps the current panel defaults and clamps unsafe sizes", () => {
    expect(inspectorWidth(1685, 330)).toBe(330);
    expect(inspectorWidth(1685, 600)).toBe(400);
    expect(dockHeight(1286, 250)).toBe(250);
    expect(inspectorWidth(900, 900)).toBe(340);
    expect(dockHeight(600, 900)).toBe(270);
  });

  it("loads valid persisted activity and tolerates corrupt storage", () => {
    expect(loadActivity({ getItem: () => "not-json" })).toEqual([]);
    expect(loadActivity({ getItem: () => JSON.stringify([{ id: "1", time: "now", level: "info", message: "ready" }]) })).toHaveLength(1);
  });

  it("records an operation with a timestamp", () => {
    const entries = recordActivity([], "Stopping dockbench…", "info", new Date("2026-08-26T00:00:00Z"));
    expect(entries[0]).toMatchObject({ level: "info", message: "Stopping dockbench…" });
  });
});
