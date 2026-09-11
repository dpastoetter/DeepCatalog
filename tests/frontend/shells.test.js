import { describe, expect, it } from "vitest";

import { askShellHtml } from "../../app/static/ask.js";
import { archiveDrawerHtml, archiveShellHtml } from "../../app/static/documents.js";
import { inboxShellHtml } from "../../app/static/inbox.js";
import { settingsShellHtml } from "../../app/static/settings.js";

describe("view shells", () => {
  it("inbox shell includes upload, process, and workflow ids", () => {
    const html = inboxShellHtml();
    for (const id of [
      "upload-form",
      "file",
      "drop-zone",
      "staging-list",
      "process-inbox",
      "refresh-inbox",
      "clear-inbox",
      "workflow",
      "pipeline",
      "job-queue",
      "inbox-out",
      "process-out",
    ]) {
      expect(html).toContain(`id="${id}"`);
    }
  });

  it("archive shell includes search and list ids; drawer is a view sibling", () => {
    const html = archiveShellHtml();
    for (const id of [
      "search-form",
      "search-q",
      "search-type",
      "archive-filters-panel",
      "docs",
      "archive-load-more",
    ]) {
      expect(html).toContain(`id="${id}"`);
    }
    expect(html).toContain('class="archive-toolbar-actions"');
    expect(html).toContain('archive-filter-date');

    const drawer = archiveDrawerHtml();
    expect(drawer).toContain(`id="archive-drawer"`);
    expect(drawer).toContain(`id="archive-drawer-backdrop"`);
    expect(drawer).toContain(`id="archive-drawer-close"`);
  });

  it("ask shell includes thread and composer ids", () => {
    const html = askShellHtml();
    for (const id of ["ask-thread", "ask-live", "ask-form", "question", "ask-submit"]) {
      expect(html).toContain(`id="${id}"`);
    }
    expect(html).toContain('aria-label="Question"');
    expect(html).not.toContain("rows=\"5\"");
  });

  it("settings shell includes provider, filing, and mock ids", () => {
    const html = settingsShellHtml();
    for (const id of [
      "auth-section",
      "setup-section",
      "setup-require-approval",
      "setup-save",
      "theme-grid",
      "mock-toggle",
      "ask-examples-toggle",
      "update-check",
      "clear-all-data",
      "ollama-chat-model",
      "ollama-embed-model",
    ]) {
      expect(html).toContain(`id="${id}"`);
    }
  });
});
