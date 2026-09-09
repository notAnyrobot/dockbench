import { expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ClipboardPanel } from "./clipboard-panel";
import { DesktopClipboard } from "./clipboard";

it("renders separate editable outgoing and selectable incoming text with disabled disconnected actions", () => {
  const html = renderToStaticMarkup(createElement(ClipboardPanel, { clipboard: new DesktopClipboard() }));
  expect(html).toContain('aria-label="Outgoing clipboard text"');
  expect(html).toMatch(/<textarea[^>]*aria-label="Incoming clipboard text"[^>]*readonly=""/);
  expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Send to desktop/);
  expect(html).toContain("Copy to host");
});
