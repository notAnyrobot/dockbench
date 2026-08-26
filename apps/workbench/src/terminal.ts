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
