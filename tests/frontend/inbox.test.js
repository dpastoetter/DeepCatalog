import { describe, expect, it } from "vitest";

import {
  UPLOAD_ACCEPT,
  UPLOAD_CONCURRENCY,
  appendFilesToStaging,
  fileInputAcceptAttribute,
  formatBytes,
  formatUploadSummary,
  inboxShellHtml,
  isSupportedUploadName,
  removeStagedFile,
  runPool,
  settleStagingAfterUpload,
  totalStagedBytes,
  uploadRequestInit,
} from "../../app/static/inbox.js";

function fakeFile(name, size = 1000, lastModified = 1) {
  return { name, size, lastModified, type: "application/pdf" };
}

describe("upload support and formatting", () => {
  it("accepts supported suffixes and rejects others", () => {
    expect(isSupportedUploadName("scan.PDF")).toBe(true);
    expect(isSupportedUploadName("photo.jpeg")).toBe(true);
    expect(isSupportedUploadName("notes.docx")).toBe(false);
    expect(isSupportedUploadName("noext")).toBe(false);
  });

  it("sends the file as a raw body so WebKitGTK keeps the CSRF header", () => {
    const file = fakeFile("handwritting.pdf");
    const init = uploadRequestInit(file);
    expect(init.body).toBe(file);
    expect(init.body).not.toBeInstanceOf(FormData);
    expect(init.headers["X-File-Name"]).toBe("handwritting.pdf");
    expect(init.headers["Content-Type"]).toBe("application/pdf");
  });

  it("does not use image/* so the GTK/GNOME picker can list PDFs", () => {
    expect(UPLOAD_ACCEPT).toContain("application/pdf");
    expect(UPLOAD_ACCEPT).toContain(".pdf");
    expect(UPLOAD_ACCEPT).not.toContain("image/*");
    expect(fileInputAcceptAttribute()).toBe(UPLOAD_ACCEPT);
    expect(inboxShellHtml()).toContain(`accept="${UPLOAD_ACCEPT}"`);
  });

  it("omits accept in the desktop shell so the file chooser is unfiltered", () => {
    document.documentElement.classList.add("dc-desktop");
    try {
      expect(fileInputAcceptAttribute()).toBe("");
      expect(inboxShellHtml()).not.toMatch(/id="file"[^>]*accept=/);
    } finally {
      document.documentElement.classList.remove("dc-desktop");
    }
  });

  it("formats byte sizes", () => {
    expect(formatBytes(500)).toBe("500 B");
    expect(formatBytes(2048)).toMatch(/KB/);
    expect(formatBytes(3 * 1024 * 1024)).toMatch(/MB/);
  });
});

describe("staging list", () => {
  it("appends multiple files and dedupes exact matches", () => {
    const first = appendFilesToStaging([], [fakeFile("a.pdf", 10, 1), fakeFile("b.jpg", 20, 2)]);
    expect(first.staged).toHaveLength(2);
    expect(first.rejected).toHaveLength(0);

    const second = appendFilesToStaging(first.staged, [
      fakeFile("a.pdf", 10, 1),
      fakeFile("c.pdf", 30, 3),
    ]);
    expect(second.duplicates).toContain("a.pdf");
    expect(second.staged).toHaveLength(3);
    expect(second.staged.map((s) => s.name)).toEqual(["a.pdf", "b.jpg", "c.pdf"]);
  });

  it("rejects unsupported types without adding them", () => {
    const result = appendFilesToStaging([], [fakeFile("x.pdf"), fakeFile("y.zip")]);
    expect(result.staged).toHaveLength(1);
    expect(result.rejected).toEqual(["y.zip"]);
  });

  it("removes one staged file by id", () => {
    const { staged } = appendFilesToStaging([], [fakeFile("a.pdf"), fakeFile("b.pdf")]);
    const next = removeStagedFile(staged, staged[0].id);
    expect(next).toHaveLength(1);
    expect(next[0].name).toBe("b.pdf");
  });

  it("totals staged bytes", () => {
    const { staged } = appendFilesToStaging([], [fakeFile("a.pdf", 100), fakeFile("b.pdf", 250)]);
    expect(totalStagedBytes(staged)).toBe(350);
  });
});

describe("upload settle helpers", () => {
  it("formats partial and full summaries", () => {
    expect(formatUploadSummary({ ok: 8, failed: 1 })).toBe("8 scans added · 1 failed");
    expect(formatUploadSummary({ ok: 1, failed: 0 })).toBe("1 scan added");
    expect(formatUploadSummary({ ok: 0, failed: 2 })).toBe("2 uploads failed");
  });

  it("keeps failed rows and drops uploaded ones", () => {
    const staged = [
      { id: "1", status: "uploaded", name: "a.pdf" },
      { id: "2", status: "error", name: "b.pdf", error: "boom" },
      { id: "3", status: "waiting", name: "c.pdf" },
    ];
    expect(settleStagingAfterUpload(staged).map((s) => s.id)).toEqual(["2"]);
  });
});

describe("runPool", () => {
  it("limits concurrency and preserves order", async () => {
    expect(UPLOAD_CONCURRENCY).toBe(3);
    let active = 0;
    let maxActive = 0;
    const items = [1, 2, 3, 4, 5];
    const results = await runPool(items, 2, async (n) => {
      active += 1;
      maxActive = Math.max(maxActive, active);
      await new Promise((r) => setTimeout(r, 15));
      active -= 1;
      return n * 10;
    });
    expect(maxActive).toBeLessThanOrEqual(2);
    expect(results).toEqual([10, 20, 30, 40, 50]);
  });
});
