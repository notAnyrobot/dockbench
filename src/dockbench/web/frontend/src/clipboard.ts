interface ClipboardConnection extends EventTarget {
  clipboardPasteFrom(text: string): void;
}

export class DesktopClipboard {
  private connection: ClipboardConnection | null = null;
  private state = { connected: false, outgoing: "", incoming: "", message: "" };
  private observers = new Set<() => void>();
  snapshot = () => this.state;
  subscribe = (observer: () => void) => {
    this.observers.add(observer);
    return () => { this.observers.delete(observer); };
  };
  private update(patch: Partial<typeof this.state>) {
    this.state = { ...this.state, ...patch };
    this.observers.forEach((observer) => observer());
  }
  private connected = () => this.update({ connected: true });
  private received = (event: Event) => {
    if (this.state.connected) {
      this.update({ incoming: (event as CustomEvent<{ text: string }>).detail.text, message: "" });
    }
  };
  attach(connection: ClipboardConnection) {
    this.detach();
    this.connection = connection;
    connection.addEventListener("connect", this.connected);
    connection.addEventListener("clipboard", this.received);
    connection.addEventListener("disconnect", this.detach);
    connection.addEventListener("securityfailure", this.detach);
  }
  detach = () => {
    this.connection?.removeEventListener("connect", this.connected);
    this.connection?.removeEventListener("clipboard", this.received);
    this.connection?.removeEventListener("disconnect", this.detach);
    this.connection?.removeEventListener("securityfailure", this.detach);
    this.connection = null;
    this.update({ connected: false, outgoing: "", incoming: "", message: "" });
  };
  async copy(host?: Pick<Clipboard, "writeText">) {
    if (!this.state.connected) return;
    const connection = this.connection;
    try {
      if (!host) throw new Error("unavailable");
      await host.writeText(this.state.incoming);
      if (connection === this.connection) this.update({ message: "Copied to host." });
    } catch {
      if (connection === this.connection) this.update({ message: "Could not copy automatically; select the incoming text and copy it manually." });
    }
  }
  edit(outgoing: string) { this.update({ outgoing }); }
  send() {
    if (!this.state.connected) return;
    this.connection?.clipboardPasteFrom(this.state.outgoing);
    this.update({ message: "Sent. Use Paste in the remote application." });
  }
}
