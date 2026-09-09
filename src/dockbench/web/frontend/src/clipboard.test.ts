import { expect, it } from "vitest";
import { DesktopClipboard } from "./clipboard";

class FakeRFB extends EventTarget {
  sent: string[] = [];
  clipboardPasteFrom(text: string) { this.sent.push(text); }
  sendKey() { throw new Error("Clipboard must never inject keys"); }
  sendCtrlAltDel() { throw new Error("Clipboard must never inject shortcuts"); }
}

it("sends exact outgoing text only after an explicit send on a connected desktop", () => {
  const clipboard = new DesktopClipboard();
  const rfb = new FakeRFB();
  clipboard.attach(rfb);
  rfb.dispatchEvent(new Event("connect"));
  const text = "  echo '中文 😀'\n\t" + "long command ".repeat(1000) + "\n";
  clipboard.edit(text);
  expect(rfb.sent).toEqual([]);
  clipboard.send();
  expect(rfb.sent).toEqual([text]);
  expect(clipboard.snapshot().message).toContain("Paste");
});

function receive(rfb: FakeRFB, text: string) {
  rfb.dispatchEvent(new CustomEvent("clipboard", { detail: { text } }));
}

it("shows remote text and copies it to the host only on request", async () => {
  const clipboard = new DesktopClipboard();
  const rfb = new FakeRFB();
  clipboard.attach(rfb);
  rfb.dispatchEvent(new Event("connect"));
  const writes: string[] = [];
  receive(rfb, "\t中文 😀\n  command\n");
  expect(clipboard.snapshot().incoming).toBe("\t中文 😀\n  command\n");
  expect(writes).toEqual([]);
  await clipboard.copy({ writeText: async (text: string) => { writes.push(text); } });
  expect(writes).toEqual(["\t中文 😀\n  command\n"]);
});

it("keeps incoming text selectable when host clipboard access is absent or denied", async () => {
  const clipboard = new DesktopClipboard();
  const rfb = new FakeRFB();
  clipboard.attach(rfb);
  rfb.dispatchEvent(new Event("connect"));
  receive(rfb, "keep this text");
  await clipboard.copy(undefined);
  expect(clipboard.snapshot().message).toContain("select");
  await clipboard.copy({ writeText: async () => { throw new Error("denied"); } });
  expect(clipboard.snapshot().incoming).toBe("keep this text");
  expect(clipboard.snapshot().message).toContain("select");
});

it("clears disconnected and replaced sessions and ignores their late events after cleanup", () => {
  const clipboard = new DesktopClipboard();
  const first = new FakeRFB(), second = new FakeRFB();
  clipboard.attach(first);
  rfbConnect(first);
  clipboard.edit("private outgoing");
  receive(first, "private incoming");
  first.dispatchEvent(new Event("disconnect"));
  expect(clipboard.snapshot()).toEqual({ connected: false, outgoing: "", incoming: "", message: "" });
  clipboard.send();
  expect(first.sent).toEqual([]);
  clipboard.attach(second);
  rfbConnect(second);
  receive(first, "stale");
  expect(clipboard.snapshot().incoming).toBe("");
  receive(second, "new session");
  expect(clipboard.snapshot().incoming).toBe("new session");
  clipboard.detach();
  rfbConnect(second);
  receive(second, "after cleanup");
  expect(clipboard.snapshot()).toEqual({ connected: false, outgoing: "", incoming: "", message: "" });
});
function rfbConnect(rfb: FakeRFB) { rfb.dispatchEvent(new Event("connect")); }

it("does not show an old copy result in a replacement session", async () => {
  const clipboard = new DesktopClipboard();
  const first = new FakeRFB();
  clipboard.attach(first);
  rfbConnect(first);
  receive(first, "old text");
  let finish!: () => void;
  const pending = clipboard.copy({ writeText: () => new Promise<void>((resolve) => { finish = resolve; }) });
  clipboard.attach(new FakeRFB());
  finish();
  await pending;
  expect(clipboard.snapshot().message).toBe("");
});
