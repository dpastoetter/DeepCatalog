import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  applyDesktopShellClass,
  dropEventToUriPayload,
  initDesktopShell,
  ingestDesktopUriDrop,
  isBrowserChromeShortcut,
  isDesktopShell,
  isExternalHttpUrl,
  openExternalHttpUrl,
} from "../../app/static/desktop-shell.js";

function keyEvent(init) {
  return new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
}

describe("desktop shell detection", () => {
  afterEach(() => {
    document.documentElement.classList.remove("dc-desktop");
    window.history.replaceState(null, "", "/");
  });

  it("treats ?desktop=1 as the AppImage / --app shell", () => {
    window.history.replaceState(null, "", "/?desktop=1");
    expect(isDesktopShell()).toBe(true);
    expect(applyDesktopShellClass()).toBe(true);
    expect(document.documentElement.classList.contains("dc-desktop")).toBe(true);
  });

  it("does not mark a normal browser tab as the desktop shell", () => {
    window.history.replaceState(null, "", "/");
    expect(isDesktopShell()).toBe(false);
    expect(applyDesktopShellClass()).toBe(false);
    expect(document.documentElement.classList.contains("dc-desktop")).toBe(false);
  });
});

describe("browser chrome shortcuts", () => {
  it("blocks new-tab / address-bar / inspect keys", () => {
    expect(isBrowserChromeShortcut(keyEvent({ key: "t", ctrlKey: true }))).toBe(true);
    expect(isBrowserChromeShortcut(keyEvent({ key: "n", ctrlKey: true }))).toBe(true);
    expect(isBrowserChromeShortcut(keyEvent({ key: "l", ctrlKey: true }))).toBe(true);
    expect(isBrowserChromeShortcut(keyEvent({ key: "F12" }))).toBe(true);
    expect(isBrowserChromeShortcut(keyEvent({ key: "i", ctrlKey: true, shiftKey: true }))).toBe(
      true,
    );
  });

  it("leaves copy, undo, and in-app Ctrl+Enter alone", () => {
    expect(isBrowserChromeShortcut(keyEvent({ key: "c", ctrlKey: true }))).toBe(false);
    expect(isBrowserChromeShortcut(keyEvent({ key: "v", ctrlKey: true }))).toBe(false);
    expect(isBrowserChromeShortcut(keyEvent({ key: "z", ctrlKey: true }))).toBe(false);
    expect(isBrowserChromeShortcut(keyEvent({ key: "Enter", ctrlKey: true }))).toBe(false);
    expect(isBrowserChromeShortcut(keyEvent({ key: "r", ctrlKey: true }))).toBe(false);
  });
});

describe("initDesktopShell", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/?desktop=1");
    document.documentElement.classList.remove("dc-desktop");
  });

  afterEach(() => {
    window.history.replaceState(null, "", "/");
    document.documentElement.classList.remove("dc-desktop");
  });

  it("suppresses the page context menu outside fields", () => {
    initDesktopShell();
    const event = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });
    document.body.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
  });

  it("prevents WebKit from navigating when files are dropped on the window", () => {
    initDesktopShell();
    const over = new Event("dragover", { bubbles: true, cancelable: true });
    const drop = new Event("drop", { bubbles: true, cancelable: true });
    document.body.dispatchEvent(over);
    document.body.dispatchEvent(drop);
    expect(over.defaultPrevented).toBe(true);
    expect(drop.defaultPrevented).toBe(true);
  });
});

describe("file-manager drops", () => {
  it("reads Nautilus uri-list and gnome copied-files payloads", () => {
    const event = {
      dataTransfer: {
        getData: (type) => {
          if (type === "text/uri-list") return "file:///tmp/scan.pdf\r\n";
          if (type === "x-special/gnome-copied-files") return "copy\nfile:///tmp/scan.pdf";
          return "";
        },
      },
    };
    expect(dropEventToUriPayload(event)).toContain("file:///tmp/scan.pdf");
  });

  it("sends uri-list drops through the pywebview ingest bridge", async () => {
    window.history.replaceState(null, "", "/?desktop=1");
    const payloads = [];
    window.pywebview = {
      api: {
        ingest_drop: (payload) => {
          payloads.push(payload);
          return { ok: ["scan.pdf"], errors: [] };
        },
      },
    };
    const details = [];
    window.addEventListener("dc-desktop-drop", (event) => details.push(event.detail), {
      once: true,
    });
    const ok = await ingestDesktopUriDrop({
      dataTransfer: { getData: (type) => (type === "text/uri-list" ? "file:///tmp/scan.pdf" : "") },
    });
    expect(ok).toBe(true);
    expect(payloads[0]).toContain("file:///tmp/scan.pdf");
    expect(details[0]).toEqual({ ok: ["scan.pdf"], errors: [] });
    delete window.pywebview;
    window.history.replaceState(null, "", "/");
  });
});

describe("external http links", () => {
  it("treats ChatGPT / GitHub as external and loopback as in-app", () => {
    expect(isExternalHttpUrl("https://auth.openai.com/authorize")).toBe(true);
    expect(isExternalHttpUrl("https://github.com/dpastoetter/DeepCatalog")).toBe(true);
    expect(isExternalHttpUrl("http://127.0.0.1:8080/?desktop=1")).toBe(false);
    expect(isExternalHttpUrl("http://localhost:11434")).toBe(false);
    expect(isExternalHttpUrl("about:blank")).toBe(false);
  });

  it("sends external URLs through the pywebview bridge", () => {
    window.history.replaceState(null, "", "/?desktop=1");
    const opened = [];
    window.pywebview = {
      api: {
        open_url: (url) => {
          opened.push(url);
          return true;
        },
      },
    };
    expect(openExternalHttpUrl("https://ollama.com/download")).toBe(true);
    expect(opened).toEqual(["https://ollama.com/download"]);
    expect(openExternalHttpUrl("http://127.0.0.1:8080/")).toBe(false);
    delete window.pywebview;
    window.history.replaceState(null, "", "/");
  });

  it("routes window.open of https URLs to the desktop bridge", () => {
    window.history.replaceState(null, "", "/?desktop=1");
    const opened = [];
    window.pywebview = {
      api: {
        open_url: (url) => {
          opened.push(url);
          return true;
        },
      },
    };
    initDesktopShell();
    expect(window.open("https://auth.openai.com/x", "_blank", "noopener")).toBeNull();
    expect(opened).toEqual(["https://auth.openai.com/x"]);
    delete window.pywebview;
    window.history.replaceState(null, "", "/");
  });
});
