import { describe, expect, it } from "vitest";

import {
  ASK_EXAMPLES,
  ASK_EXAMPLES_STORAGE_KEY,
  ASK_HISTORY_MAX_TURNS,
  areAskExamplesEnabled,
  classifyClientEvidence,
  evidenceLabel,
  historyPayloadFromTurns,
  sanitizeDocumentOpenUrl,
  setAskExamplesEnabled,
  stripSourcesSection,
  syncAskComposerSize,
} from "../../app/static/ask.js";

describe("Ask helpers", () => {
  it("exposes example questions for the empty state", () => {
    expect(ASK_EXAMPLES.length).toBeGreaterThanOrEqual(4);
    expect(ASK_EXAMPLES.every((q) => typeof q === "string" && q.length > 8)).toBe(true);
    expect(ASK_EXAMPLES.some((q) => /invoice/i.test(q))).toBe(true);
  });

  it("defaults ask example suggestions on and persists the toggle", () => {
    localStorage.removeItem(ASK_EXAMPLES_STORAGE_KEY);
    expect(areAskExamplesEnabled()).toBe(true);
    setAskExamplesEnabled(false);
    expect(localStorage.getItem(ASK_EXAMPLES_STORAGE_KEY)).toBe("0");
    expect(areAskExamplesEnabled()).toBe(false);
    setAskExamplesEnabled(true);
    expect(areAskExamplesEnabled()).toBe(true);
  });

  it("strips trailing Sources sections from model replies", () => {
    expect(stripSourcesSection("Answer text\n\n## Sources\n- a.pdf")).toBe("Answer text");
  });

  it("sanitizes open_url to same-origin document file paths", () => {
    expect(sanitizeDocumentOpenUrl("/api/documents/abc/file", "abc")).toBe(
      "/api/documents/abc/file",
    );
    expect(sanitizeDocumentOpenUrl("https://evil.example/x", "abc")).toBe(
      "/api/documents/abc/file",
    );
    expect(sanitizeDocumentOpenUrl("javascript:alert(1)", "abc")).toBe(
      "/api/documents/abc/file",
    );
    expect(sanitizeDocumentOpenUrl("/api/documents/abc/file://x", "abc")).toBe(
      "/api/documents/abc/file",
    );
  });

  it("maps file URLs to OS-open endpoints", async () => {
    const { documentOsOpenUrl } = await import("../../app/static/ask.js");
    expect(documentOsOpenUrl("/api/documents/abc/file")).toBe("/api/documents/abc/open");
    expect(documentOsOpenUrl("/api/reviews/r1/file")).toBe("/api/reviews/r1/open");
    expect(documentOsOpenUrl("/api/other")).toBe(null);
  });

  it("builds bounded history from completed turns", () => {
    const turns = [
      { id: "1", question: "Q1", reply: "A1", status: "success" },
      { id: "2", question: "Q2", reply: "A2", status: "success" },
      { id: "3", question: "Q3", reply: "A3", status: "success" },
      { id: "4", question: "Q4", loading: true },
    ];
    const history = historyPayloadFromTurns(turns, ASK_HISTORY_MAX_TURNS);
    expect(history.every((h) => h.role === "user" || h.role === "assistant")).toBe(true);
    expect(history.length).toBeLessThanOrEqual(ASK_HISTORY_MAX_TURNS);
    expect(history.at(-2)).toEqual({ role: "user", content: "Q3" });
    expect(history.at(-1)).toEqual({ role: "assistant", content: "A3" });
    expect(history.some((h) => h.content === "Q4")).toBe(false);
  });

  it("classifies evidence and labels weak/none states", () => {
    expect(classifyClientEvidence({ grounded: false })).toBe("none");
    expect(classifyClientEvidence({ evidence: "weak" })).toBe("weak");
    expect(classifyClientEvidence({ evidence: "strong", retrieval_count: 2 })).toBe("strong");
    expect(evidenceLabel("weak", true)).toMatch(/Limited supporting evidence/i);
    expect(evidenceLabel("none", false)).toMatch(/No relevant documents/i);
    expect(evidenceLabel("strong", true)).toBe("");
  });

  it("sizes the composer to content up to the CSS max height", () => {
    expect(() => syncAskComposerSize(null)).not.toThrow();
    const el = document.createElement("textarea");
    el.style.maxHeight = "128px";
    Object.defineProperty(el, "scrollHeight", { configurable: true, value: 48 });
    document.body.appendChild(el);
    syncAskComposerSize(el);
    expect(el.style.height).toBe("48px");
    Object.defineProperty(el, "scrollHeight", { configurable: true, value: 400 });
    syncAskComposerSize(el);
    expect(el.style.height).toBe("128px");
    el.remove();
  });
});
