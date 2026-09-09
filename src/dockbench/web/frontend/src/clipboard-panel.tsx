import { useSyncExternalStore } from "react";
import { DesktopClipboard } from "./clipboard";

export function ClipboardPanel({ clipboard }: { clipboard: DesktopClipboard }) {
  const state = useSyncExternalStore(clipboard.subscribe, clipboard.snapshot, clipboard.snapshot);
  return <section id="desktop-clipboard" className="clipboard-panel" aria-label="Desktop clipboard">
    <h2>Clipboard</h2>
    <label>Outgoing text
      <textarea aria-label="Outgoing clipboard text" value={state.outgoing}
        disabled={!state.connected} onChange={(event) => clipboard.edit(event.target.value)} />
    </label>
    <button disabled={!state.connected} onClick={() => clipboard.send()}>Send to desktop</button>
    <p>Sending sets the remote clipboard. Use Paste in the remote application when ready.</p>
    <label>Incoming remote text
      <textarea aria-label="Incoming clipboard text" readOnly value={state.incoming} />
    </label>
    <button disabled={!state.connected} onClick={() => void clipboard.copy(navigator.clipboard)}>Copy to host</button>
    <p>You can also select the incoming text and copy it manually.</p>
    <p role="status">{state.message}</p>
  </section>;
}
