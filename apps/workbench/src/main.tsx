import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import RFB from "@novnc/novnc";
import { applyStatusToDesktop, desktopButtonLabel, disconnectState, fullscreenButtonLabel, passwordForConnection, workstationButtonLabel, type DesktopState } from "./connection";
import "./styles.css";

type State = DesktopState;
type Status = { state: State; desktop_ready: boolean; image: string; container_name: string; workspace: string; csrf_token: string; message?: string };

const api = async <T,>(path: string, csrf?: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(path, { credentials: "same-origin", ...init, headers: { "content-type": "application/json", ...(csrf ? { "x-csrf-token": csrf } : {}), ...init?.headers } });
  const data = await response.json();
  if (!response.ok) throw new Error(data.message || "Request failed");
  return data;
};

function App() {
  const canvas = useRef<HTMLDivElement>(null); const rfb = useRef<RFB | null>(null);
  const vncPassword = useRef<string | null>(null); const closingDesktop = useRef(false);
  const [status, setStatus] = useState<Status | null>(null); const [state, setState] = useState<State>("loading");
  const [message, setMessage] = useState("Checking workstation…"); const [passwordAction, setPasswordAction] = useState<"connect" | "reset" | null>(null);
  const [canResetPassword, setCanResetPassword] = useState(false);
  const [password, setPassword] = useState("");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  const sidebarCollapsedRef = useRef(false);
  const sidebarBeforeFullscreen = useRef(false);
  const fullscreenRef = useRef(false);

  const refresh = async () => {
    try {
      const next = await api<Status>("/api/workstation");
      setStatus((current) => current ? { ...current, ...next } : next);
      // Status polling must never hide a live canvas or replace its state.
      if (applyStatusToDesktop(state)) {
        setState(next.state); setMessage(next.message || (next.state === "running" ? "Workstation is ready" : ""));
      }
    }
    catch { setState("unavailable"); setMessage("Workbench is unavailable. Check that Docker is running."); }
  };
  useEffect(() => { void refresh(); return () => rfb.current?.disconnect(); }, []);
  useEffect(() => {
    const syncFullscreen = () => {
      const nextFullscreen = document.fullscreenElement !== null;
      setFullscreen(nextFullscreen);
      if (nextFullscreen === fullscreenRef.current) return;
      if (nextFullscreen) {
        sidebarBeforeFullscreen.current = sidebarCollapsedRef.current;
        sidebarCollapsedRef.current = true;
        setSidebarCollapsed(true);
      } else {
        sidebarCollapsedRef.current = sidebarBeforeFullscreen.current;
        setSidebarCollapsed(sidebarBeforeFullscreen.current);
      }
      fullscreenRef.current = nextFullscreen;
    };
    const fullscreenPoll = window.setInterval(syncFullscreen, 250);
    document.addEventListener("fullscreenchange", syncFullscreen);
    document.addEventListener("visibilitychange", syncFullscreen);
    window.addEventListener("pageshow", syncFullscreen);
    syncFullscreen();
    return () => {
      window.clearInterval(fullscreenPoll);
      document.removeEventListener("fullscreenchange", syncFullscreen);
      document.removeEventListener("visibilitychange", syncFullscreen);
      window.removeEventListener("pageshow", syncFullscreen);
    };
  }, []);

  const mutate = async (endpoint: "start" | "stop") => {
    if (!status) return;
    if (endpoint === "stop" && state === "connected" && !window.confirm("Stop the workstation and disconnect the desktop?")) return;
    try { setState("connecting"); setMessage(endpoint === "start" ? "Starting workstation…" : "Stopping workstation…"); const next = await api<Status>(`/api/workstation/${endpoint}`, status.csrf_token, { method: "POST" }); setStatus({ ...status, ...next }); setState(next.state); setMessage(next.state === "running" ? "Workstation is ready" : "Workstation stopped"); if (endpoint === "stop") { closingDesktop.current = true; rfb.current?.disconnect(); } }
    catch (error) { setState("failed"); setMessage(error instanceof Error ? error.message : "Action failed"); }
  };

  const openDesktop = async (suppliedPassword?: string) => {
    const connectionPassword = passwordForConnection(suppliedPassword ?? password, vncPassword.current);
    if (!status || !connectionPassword) return;
    try {
      setPasswordAction(null); setState("connecting"); setMessage("Preparing secure desktop…");
      const session = await api<{ session_id: string }>("/api/desktop/sessions", status.csrf_token, { method: "POST", body: JSON.stringify({ password: connectionPassword }) });
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      rfb.current?.disconnect();
      const client = new RFB(canvas.current!, `${scheme}://${location.host}/api/desktop/sessions/${session.session_id}/ws`, { credentials: { password: connectionPassword } });
      vncPassword.current = connectionPassword; setPassword(""); // Memory only; never browser storage or input state.
      let authenticationFailed = false;
      client.scaleViewport = true; client.viewOnly = false;
      client.addEventListener("connect", () => { setCanResetPassword(false); setState("connected"); setMessage("Desktop connected"); });
      client.addEventListener("securityfailure", () => { authenticationFailed = true; vncPassword.current = null; setCanResetPassword(true); setState("failed"); setMessage("VNC authentication failed. Enter the full-control password or reset it."); });
      client.addEventListener("disconnect", () => {
        const nextState = disconnectState(authenticationFailed, closingDesktop.current);
        closingDesktop.current = false;
        if (nextState === null) return;
        setState(nextState); setMessage(nextState === "stopped" ? "Workstation stopped" : "Desktop disconnected — reconnect when ready.");
      });
      rfb.current = client;
    } catch (error) { setState("failed"); setMessage(error instanceof Error ? error.message : "Desktop connection failed"); }
  };

  const resetDesktopPassword = async () => {
    if (!status || !password) return;
    const replacement = password;
    try {
      setPasswordAction(null); setState("connecting"); setMessage("Resetting VNC password…");
      const next = await api<Status>("/api/desktop/password", status.csrf_token, { method: "POST", body: JSON.stringify({ password: replacement }) });
      setStatus({ ...status, ...next }); vncPassword.current = replacement; setPassword(""); setCanResetPassword(false);
      await openDesktop(replacement);
    } catch (error) { setState("failed"); setMessage(error instanceof Error ? error.message : "Password reset failed"); }
  };

  const closeDesktop = () => {
    rfb.current?.disconnect(); rfb.current = null;
    setState("running"); setMessage("Desktop closed. The workstation is still running.");
  };

  const toggleFullscreen = async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await document.documentElement.requestFullscreen();
    } catch { setMessage("Fullscreen is unavailable in this browser."); }
  };

  const label = state === "connected" ? "Connected" : state === "connecting" ? "Connecting" : state === "running" ? "Ready" : state === "loading" ? "Checking" : state;
  const workstationRunning = status?.state === "running";
  const workstationActionLabel = workstationButtonLabel(workstationRunning);
  const desktopActionLabel = desktopButtonLabel(state);
  const fullscreenActionLabel = fullscreenButtonLabel(fullscreen);
  const desktopAction = () => state === "connected" ? closeDesktop() : vncPassword.current ? void openDesktop() : setPasswordAction("connect");
  const toggleSidebar = () => setSidebarCollapsed((collapsed) => {
    sidebarCollapsedRef.current = !collapsed;
    return !collapsed;
  });
  return <main className={[sidebarCollapsed ? "sidebar-collapsed" : "", fullscreen ? "fullscreen-active" : ""].filter(Boolean).join(" ")}>
    <header className="app-header"><button className="sidebar-toggle quiet" aria-label={sidebarCollapsed ? "Expand controls" : "Collapse controls"} title={sidebarCollapsed ? "Expand controls" : "Collapse controls"} onClick={toggleSidebar}>☰</button><div className="brand"><strong>Docker Workbench</strong><span>{status?.container_name || "docker-ws"}</span></div></header>
    {fullscreen && !sidebarCollapsed && <button className="fullscreen-sidebar-toggle quiet" aria-label="Collapse controls" title="Collapse controls" onClick={toggleSidebar}>☰</button>}
    {sidebarCollapsed && <nav className="floating-controls" aria-label="Quick controls">
      <button className="quiet" aria-label="Expand controls" title="Expand controls" onClick={toggleSidebar}>☰</button>
      <button className={`floating-status ${state}`} onClick={() => void refresh()} title={`Refresh status — ${label}`} aria-label={`Refresh status — ${label}`}><i /><span>↻</span></button>
      <button className={workstationRunning ? "toggle-on" : "primary"} onClick={() => void mutate(workstationRunning ? "stop" : "start")} disabled={!status || state === "connecting"} title={workstationActionLabel} aria-label={workstationActionLabel} aria-pressed={workstationRunning}>{workstationRunning ? "■" : "▶"}</button>
      <button className={state === "connected" ? "toggle-on" : "primary"} onClick={desktopAction} disabled={!status || state === "connecting" || !workstationRunning} title={desktopActionLabel} aria-label={desktopActionLabel} aria-pressed={state === "connected"}>{state === "connected" ? "×" : "▣"}</button>
      <button className={fullscreen ? "toggle-on" : "quiet"} onClick={() => void toggleFullscreen()} title={fullscreenActionLabel} aria-label={fullscreenActionLabel} aria-pressed={fullscreen}>⛶</button>
    </nav>}
    <aside className="sidebar" aria-label="Workbench controls"><div className={`status ${state}`}><i /> <span>{label}</span><button className="status-refresh" onClick={() => void refresh()} title="Refresh status" aria-label="Refresh status">↻</button></div><nav className="actions">
      <button className={workstationRunning ? "toggle-on" : "primary"} onClick={() => void mutate(workstationRunning ? "stop" : "start")} disabled={!status || state === "connecting"} title={workstationActionLabel} aria-pressed={workstationRunning}><b>{workstationRunning ? "■" : "▶"}</b><span>{workstationActionLabel}</span></button>
      <button className={state === "connected" ? "toggle-on" : "primary"} onClick={desktopAction} disabled={!status || state === "connecting" || !workstationRunning} title={desktopActionLabel} aria-pressed={state === "connected"}><b>{state === "connected" ? "×" : "▣"}</b><span>{desktopActionLabel}</span></button>
      <button className={fullscreen ? "toggle-on" : "quiet"} onClick={() => void toggleFullscreen()} title={fullscreenActionLabel} aria-pressed={fullscreen}><b>⛶</b><span>{fullscreenActionLabel}</span></button>
    </nav></aside>
    <section className="desktop"><div ref={canvas} className="canvas" />{state !== "connected" && <div className="empty"><h1>{state === "stopped" || state === "absent" ? "Your desktop is off" : "Desktop not connected"}</h1><p>{message || "Start the workstation, then open the desktop."}</p>{(state === "stopped" || state === "absent") && <button className="primary" onClick={() => void mutate("start")}>Start workstation</button>}{canResetPassword && <button className="quiet" onClick={() => setPasswordAction("reset")}>Reset VNC password</button>}</div>}</section>
    {passwordAction && <div className="modal" role="dialog" aria-modal="true"><form onSubmit={(event) => { event.preventDefault(); passwordAction === "reset" ? void resetDesktopPassword() : void openDesktop(); }}><h2>{passwordAction === "reset" ? "Reset VNC password" : "Connect desktop"}</h2><p>{passwordAction === "reset" ? "Choose a new 6–8 character full-control password. This replaces the existing VNC credential and restarts the desktop." : "Enter the full-control VNC password. It is used only for this connection and is never saved by the browser."}</p><input autoFocus type="password" autoComplete={passwordAction === "reset" ? "new-password" : "current-password"} minLength={passwordAction === "reset" ? 6 : undefined} maxLength={passwordAction === "reset" ? 8 : undefined} value={password} onChange={(event) => setPassword(event.target.value)} required /><div>{passwordAction === "connect" && <button type="button" className="quiet" onClick={() => { setPassword(""); setPasswordAction("reset"); }}>Reset password</button>}<button type="button" className="quiet" onClick={() => { setPassword(""); setPasswordAction(null); }}>Cancel</button><button className="primary" type="submit">{passwordAction === "reset" ? "Reset and connect" : "Connect"}</button></div></form></div>}
  </main>;
}
createRoot(document.getElementById("root")!).render(<App />);
