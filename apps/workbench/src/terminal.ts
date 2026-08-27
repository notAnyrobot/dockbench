/** A stale socket event must never close or dispose a replacement tab. */
export type ClosableTerminalSession = {
  id: string;
  socket: { close(): void };
  terminal: { dispose(): void };
};

export function closeTerminalSession<T extends ClosableTerminalSession>(
  sessions: Map<string, T>,
  session: T,
): boolean {
  if (sessions.get(session.id) !== session) return false;
  sessions.delete(session.id);
  session.socket.close();
  session.terminal.dispose();
  return true;
}

export type RestorableTerminalSession = {
  socket: { readyState: number; send(value: string): void };
  fit: { fit(): void };
  terminal: {
    cols: number;
    rows: number;
    refresh(start: number, end: number): void;
    focus(): void;
  };
};

export function resizeTerminalSurface(
  session: RestorableTerminalSession,
  openSocketState: number,
): boolean {
  try {
    session.fit.fit();
  } catch {
    return false;
  }
  if (session.socket.readyState === openSocketState) {
    session.socket.send(JSON.stringify({
      type: "resize",
      cols: session.terminal.cols,
      rows: session.terminal.rows,
    }));
  }
  return true;
}

/** Restore xterm after its browser tab or Dockbench pane becomes visible. */
export function restoreTerminalSurface(
  session: RestorableTerminalSession,
  openSocketState: number,
): void {
  if (!resizeTerminalSurface(session, openSocketState)) return;
  session.terminal.refresh(0, Math.max(0, session.terminal.rows - 1));
  session.terminal.focus();
}
