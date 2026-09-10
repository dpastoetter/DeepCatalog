import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  api,
  displayText,
  errorMessage,
  escapeHtml,
  formatApiError,
  isFinancialDocType,
  referenceIdsToString,
} from "../../app/static/api.js";

describe("formatApiError", () => {
  it("prefers string detail", () => {
    expect(formatApiError({ detail: " nope " })).toBe(" nope ");
  });

  it("joins validation array details", () => {
    expect(
      formatApiError({
        detail: [{ msg: "a" }, "b", { msg: "c" }],
      }),
    ).toBe("a; b; c");
  });

  it("reads nested object messages", () => {
    expect(formatApiError({ detail: { message: "nested" } })).toBe("nested");
    expect(formatApiError({ error: { error: "inner" } })).toBe("inner");
  });

  it("falls back when empty", () => {
    expect(formatApiError({}, "fallback")).toBe("fallback");
  });
});

describe("errorMessage / displayText", () => {
  it("handles strings and Error objects", () => {
    expect(errorMessage(" boom ")).toBe("boom");
    expect(errorMessage(new Error("fail"))).toBe("fail");
    expect(errorMessage(null, "x")).toBe("x");
  });

  it("formats detail payloads", () => {
    expect(errorMessage({ detail: "denied" })).toBe("denied");
    expect(displayText(42)).toBe("42");
    expect(displayText(null, "-")).toBe("-");
  });
});

describe("escapeHtml / helpers", () => {
  it("escapes HTML entities", () => {
    expect(escapeHtml(`<a href="x">&'`)).toBe(
      "&lt;a href=&quot;x&quot;&gt;&amp;&#39;",
    );
  });

  it("detects financial doc types and reference ids", () => {
    expect(isFinancialDocType("Invoice")).toBe(true);
    expect(isFinancialDocType("letter")).toBe(false);
    expect(referenceIdsToString([" A ", "", "B"])).toBe("A, B");
    expect(referenceIdsToString("  x  ")).toBe("x");
  });
});

describe("api()", () => {
  beforeEach(() => {
    window.DC_MOCK = undefined;
    window.PA_API_TOKEN = undefined;
    sessionStorage.clear();
    vi.restoreAllMocks();
  });

  it("sets CSRF header and returns JSON on success", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      statusText: "OK",
      json: async () => ({ status: "success", value: 1 }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const data = await api("/api/ping", { method: "GET" });
    expect(data.value).toBe(1);
    const [, opts] = fetchMock.mock.calls[0];
    expect(opts.headers["x-requested-with"] || opts.headers["X-Requested-With"]).toBe(
      "DeepCatalog",
    );
  });

  it("keeps the CSRF header on FormData uploads as a plain object", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      statusText: "OK",
      json: async () => ({ status: "success" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.append("file", new Blob(["x"]), "a.pdf");
    await api("/api/upload", { method: "POST", body });
    const headers = fetchMock.mock.calls[0][1].headers;
    expect(headers).not.toBeInstanceOf(Headers);
    expect(headers["x-requested-with"] || headers["X-Requested-With"]).toBe("DeepCatalog");
    expect(fetchMock.mock.calls[0][1].body).toBe(body);
  });

  it("throws formatted errors on HTTP failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        statusText: "Bad Request",
        json: async () => ({ detail: "bad ask" }),
      }),
    );
    await expect(api("/api/ask", { method: "POST", body: { question: "x" } })).rejects.toThrow(
      "bad ask",
    );
  });

  it("does not attach bearer tokens from window globals", async () => {
    window.PA_API_TOKEN = "secret-token";
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      statusText: "OK",
      json: async () => ({ status: "success" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await api("/api/health");
    const headers = fetchMock.mock.calls[0][1].headers;
    expect(headers.Authorization || headers.authorization).toBeUndefined();
    expect(fetchMock.mock.calls[0][1].credentials).toBe("same-origin");
  });
});
