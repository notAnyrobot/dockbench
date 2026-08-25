export type DesktopState = "loading" | "absent" | "stopped" | "running" | "unavailable" | "connecting" | "connected" | "failed";

export function applyStatusToDesktop(state: DesktopState): boolean {
  return state !== "connected" && state !== "connecting";
}

export function passwordForConnection(input: string, pageSessionPassword: string | null): string | null {
  return input || pageSessionPassword;
}

export function desktopButtonLabel(state: DesktopState): "Open desktop" | "Close desktop" {
  return state === "connected" ? "Close desktop" : "Open desktop";
}

export function workstationButtonLabel(running: boolean): "Start workstation" | "Stop workstation" {
  return running ? "Stop workstation" : "Start workstation";
}

/** An existing stopped container can restart; an absent one requires launch setup. */
export function workstationActionEnabled(state: DesktopState): boolean {
  return state !== "absent" && state !== "loading" && state !== "unavailable";
}

export function fullscreenButtonLabel(active: boolean): "Enter fullscreen" | "Exit fullscreen" {
  return active ? "Exit fullscreen" : "Enter fullscreen";
}

/** Fullscreen hides the header, so an expanded sidebar needs its own collapse control. */
export function fullscreenRecollapseVisible(fullscreen: boolean, sidebarCollapsed: boolean): boolean {
  return fullscreen && !sidebarCollapsed;
}

export function disconnectState(authenticationFailed: boolean, intentionalStop: boolean): DesktopState | null {
  if (authenticationFailed) return null;
  return intentionalStop ? "stopped" : "running";
}
