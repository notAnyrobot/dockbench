export type ActivityLevel = "info" | "success" | "warning" | "error";
export type ActivityEntry = { id: string; time: string; level: ActivityLevel; message: string };

const ACTIVITY_KEY = "dockbench.activity";
const INSPECTOR_KEY = "dockbench.inspector-width";
const DOCK_KEY = "dockbench.dock-height";
const MAX_ACTIVITY = 120;

export function loadActivity(storage: Pick<Storage, "getItem"> = localStorage): ActivityEntry[] {
  try {
    const value = JSON.parse(storage.getItem(ACTIVITY_KEY) || "[]");
    return Array.isArray(value) ? value.filter((entry) => entry && typeof entry.message === "string").slice(-MAX_ACTIVITY) : [];
  } catch {
    return [];
  }
}

export function recordActivity(entries: ActivityEntry[], message: string, level: ActivityLevel = "info", now = new Date()): ActivityEntry[] {
  return [...entries, { id: `${now.getTime()}-${Math.random().toString(16).slice(2)}`, time: now.toLocaleTimeString(), level, message }].slice(-MAX_ACTIVITY);
}

export function saveActivity(entries: ActivityEntry[], storage: Pick<Storage, "setItem"> = localStorage): void {
  try { storage.setItem(ACTIVITY_KEY, JSON.stringify(entries)); } catch { /* Storage can be unavailable in privacy mode. */ }
}

function storedSize(key: string, fallback: number, storage: Pick<Storage, "getItem"> = localStorage): number {
  const parsed = Number(storage.getItem(key));
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

export function inspectorWidth(viewport: number, requested = storedSize(INSPECTOR_KEY, 330)): number {
  return Math.round(Math.max(240, Math.min(requested, Math.min(400, viewport - 560))));
}

export function dockHeight(viewport: number, requested = storedSize(DOCK_KEY, 250)): number {
  return Math.round(Math.max(130, Math.min(requested, Math.min(520, viewport - 330))));
}

export function saveLayout(inspector: number, dock: number, storage: Pick<Storage, "setItem"> = localStorage): void {
  try {
    storage.setItem(INSPECTOR_KEY, String(inspector));
    storage.setItem(DOCK_KEY, String(dock));
  } catch { /* Resizing still works for the current page. */ }
}
