import {
  api,
  armedConfirm,
  errorMessage,
  escapeHtml,
  setText,
  toast,
} from "./api.js";
import { ingestDesktopUriDrop } from "./desktop-shell.js";
import { hooks, workflowState } from "./state.js";
import { patchQueueRow } from "./events.js";

export const UPLOAD_CONCURRENCY = 3;
export const SUPPORTED_UPLOAD_SUFFIXES = new Set([
  ".pdf",
  ".png",
  ".jpg",
  ".jpeg",
  ".webp",
  ".tif",
  ".tiff",
  ".bmp",
]);
/** File picker types. Avoid `image/*` — WebKitGTK/GNOME portals then hide PDFs. */
export const UPLOAD_ACCEPT =
  "application/pdf,.pdf,image/png,image/jpeg,image/webp,image/tiff,image/bmp,.png,.jpg,.jpeg,.webp,.tif,.tiff,.bmp";

export function uploadRequestInit(file) {
  const filename = String(file?.name || "scan.bin");
  return {
    method: "POST",
    headers: {
      "X-File-Name": encodeURIComponent(filename),
      "Content-Type": file?.type || "application/octet-stream",
    },
    body: file,
  };
}

export function fileInputAcceptAttribute() {
  if (typeof document !== "undefined" && document.documentElement.classList.contains("dc-desktop")) {
    return "";
  }
  return UPLOAD_ACCEPT;
}

/** @typedef {"waiting"|"uploading"|"uploaded"|"error"} StageStatus */
/** @typedef {{ id: string, file: File, name: string, size: number, type: string, lastModified: number, status: StageStatus, error?: string }} StagedFile */

const state = {
  staged: /** @type {StagedFile[]} */ ([]),
  uploading: false,
  processBusy: false,
  inboxCount: 0,
  dragDepth: 0,
  nextId: 1,
};

export function inboxShellHtml() {
  const accept = fileInputAcceptAttribute();
  const acceptAttr = accept ? ` accept="${accept}"` : "";
  return `<div class="inbox-grid">
          <section class="card inbox-add">
            <h2 class="card-title">
              <svg class="icon" aria-hidden="true"><use href="#i-upload" /></svg>
              Add scans
            </h2>
            <form id="upload-form" class="upload" aria-label="Add scans to inbox">
              <div class="drop-zone" id="drop-zone" data-state="idle">
                <input type="file" id="file"${acceptAttr} multiple />
                <label class="drop-visual" for="file">
                  <span class="drop-mark" aria-hidden="true">+</span>
                  <span class="drop-cue">Drop scans or browse</span>
                  <span class="drop-sub">PDF or image · multiple files OK</span>
                </label>
              </div>

              <div id="staging-panel" class="staging-panel" hidden>
                <div class="staging-head">
                  <p id="staging-meta" class="staging-totals"></p>
                  <div class="staging-head-actions">
                    <button type="button" class="btn ghost compact" id="clear-staged">Clear staged</button>
                    <button type="button" class="btn ghost compact" id="retry-failed" hidden>Retry failed</button>
                  </div>
                </div>
                <ul id="staging-list" class="staging-list" aria-label="Staged scans"></ul>
                <button type="button" class="btn secondary stretch" id="upload-staged">Upload scans</button>
              </div>
              <p id="upload-live" class="sr-only" aria-live="polite"></p>
            </form>

            <p id="inbox-summary" class="status-line">Inbox: checking…</p>
            <p id="process-status" class="status-line" aria-live="polite"></p>
            <p id="process-review-link" class="process-review-link" hidden>
              <a class="btn primary compact" href="#/review">Review documents</a>
            </p>

            <div class="inbox-actions">
              <button id="process-inbox" type="button" class="btn primary stretch" disabled>
                <svg class="icon" aria-hidden="true"><use href="#i-play" /></svg>
                Process inbox
              </button>
              <div class="inbox-actions-secondary">
                <button id="refresh-inbox" type="button" class="btn ghost">
                  <svg class="icon" aria-hidden="true"><use href="#i-refresh" /></svg>
                  Refresh
                </button>
                <button id="clear-inbox" type="button" class="btn ghost danger" disabled>Remove all</button>
              </div>
            </div>
          </section>

          <section class="card workflow" id="workflow" data-state="idle">
            <header class="workflow-head">
              <div>
                <p class="section-kicker">Workflow</p>
                <p id="workflow-title" class="workflow-title">Waiting for files…</p>
              </div>
              <p id="workflow-count" class="workflow-count"></p>
            </header>
            <p id="workflow-now" class="workflow-now" data-idle="true">
              <strong class="workflow-now-step"></strong><span class="workflow-now-detail"></span><span class="workflow-now-elapsed"></span>
            </p>
            <ol id="pipeline" class="pipeline" aria-label="Ingest pipeline steps"></ol>
            <div class="job-queue-wrap">
              <p class="section-kicker">Queue</p>
              <ul id="job-queue" class="job-queue">
                <li class="queue-empty">No active job</li>
              </ul>
            </div>
          </section>
        </div>

        <details class="debug card">
          <summary>Console</summary>
          <pre id="inbox-out" class="out"></pre>
          <pre id="process-out" class="out"></pre>
        </details>`;
}

export function mountInboxShell() {
  const host = document.getElementById("inbox");
  if (!host || host.querySelector("#upload-form")) return host;
  host.innerHTML = inboxShellHtml();
  return host;
}

export function setProcessStatus(message, tone = "") {
  const el = document.getElementById("process-status");
  if (!el) return;
  el.textContent = message || "";
  if (tone) el.dataset.tone = tone;
  else delete el.dataset.tone;
}

export function announceUploadOutcome(message) {
  const live = document.getElementById("upload-live");
  if (!live) return;
  live.textContent = "";
  window.requestAnimationFrame(() => {
    live.textContent = message || "";
  });
}

export function fileSuffix(name) {
  const base = String(name || "").toLowerCase();
  const i = base.lastIndexOf(".");
  return i >= 0 ? base.slice(i) : "";
}

export function isSupportedUploadName(name) {
  return SUPPORTED_UPLOAD_SUFFIXES.has(fileSuffix(name));
}

export function formatBytes(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10 * 1024 ? 1 : 0)} KB`;
  return `${(n / (1024 * 1024)).toFixed(n < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}

export function typeLabelForName(name) {
  const suffix = fileSuffix(name).replace(".", "").toUpperCase() || "FILE";
  return suffix;
}

export function stageKey(fileLike) {
  return `${fileLike.name}::${fileLike.size}::${fileLike.lastModified}`;
}

export function appendFilesToStaging(existing, incoming) {
  const staged = [...existing];
  const seen = new Set(staged.map((s) => stageKey(s)));
  const rejected = [];
  const duplicates = [];
  let nextId = staged.reduce((max, s) => Math.max(max, Number(s.id) || 0), 0) + 1;

  for (const file of incoming || []) {
    if (!file || typeof file.name !== "string") continue;
    if (!isSupportedUploadName(file.name)) {
      rejected.push(file.name);
      continue;
    }
    const key = stageKey(file);
    if (seen.has(key)) {
      duplicates.push(file.name);
      continue;
    }
    seen.add(key);
    staged.push({
      id: String(nextId++),
      file,
      name: file.name,
      size: file.size,
      type: typeLabelForName(file.name),
      lastModified: file.lastModified,
      status: "waiting",
    });
  }
  return { staged, rejected, duplicates };
}

export function removeStagedFile(staged, id) {
  return staged.filter((s) => s.id !== id);
}

export function settleStagingAfterUpload(staged) {
  return staged.filter((s) => s.status === "error");
}

export function formatUploadSummary({ ok, failed }) {
  const okN = Number(ok) || 0;
  const failN = Number(failed) || 0;
  if (okN && failN) return `${okN} scan${okN === 1 ? "" : "s"} added · ${failN} failed`;
  if (okN) return `${okN} scan${okN === 1 ? "" : "s"} added`;
  if (failN) return `${failN} upload${failN === 1 ? "" : "s"} failed`;
  return "No files uploaded";
}

export function totalStagedBytes(staged) {
  return staged.reduce((sum, s) => sum + (Number(s.size) || 0), 0);
}

/**
 * Run async work over items with a fixed concurrency limit.
 * @template T, R
 * @param {T[]} items
 * @param {number} concurrency
 * @param {(item: T, index: number) => Promise<R>} worker
 * @returns {Promise<R[]>}
 */
export async function runPool(items, concurrency, worker) {
  const limit = Math.max(1, Math.min(concurrency, items.length || 1));
  const results = new Array(items.length);
  let next = 0;

  async function pump() {
    while (next < items.length) {
      const index = next++;
      results[index] = await worker(items[index], index);
    }
  }

  const runners = Array.from({ length: Math.min(limit, items.length) }, () => pump());
  await Promise.all(runners);
  return results;
}

function renderInboxSummary(data) {
  const el = document.getElementById("inbox-summary");
  if (!el) return;
  const files = data.files || [];
  const count = data.count ?? files.length;
  state.inboxCount = count;
  delete el.dataset.tone;

  const stagedWaiting = state.staged.filter((s) => s.status === "waiting" || s.status === "error").length;
  if (!count && !stagedWaiting) {
    el.textContent = "Inbox is empty — add scans above";
    updateActionHierarchy();
    return;
  }
  if (!count && stagedWaiting) {
    el.textContent = `${stagedWaiting} file${stagedWaiting === 1 ? "" : "s"} staged — upload to add them to the inbox`;
    el.dataset.tone = "warn";
    updateActionHierarchy();
    return;
  }
  const names = files.map((f) => f.name).join(", ");
  el.textContent = `${count} file${count === 1 ? "" : "s"} in inbox — ready to process${names ? `: ${names}` : ""}`;
  el.dataset.tone = "ok";
  updateActionHierarchy();
}

function updateActionHierarchy() {
  const processBtn = document.getElementById("process-inbox");
  const clearBtn = document.getElementById("clear-inbox");
  if (processBtn) {
    const emphasize = state.inboxCount > 0 && !state.uploading && !state.processBusy;
    processBtn.disabled = state.processBusy || state.uploading || state.inboxCount <= 0;
    processBtn.classList.toggle("primary", emphasize);
    processBtn.classList.toggle("secondary", !emphasize);
    if (state.inboxCount <= 0) {
      processBtn.title = "Upload scans to the inbox first";
    } else {
      processBtn.removeAttribute("title");
    }
  }
  if (clearBtn) {
    clearBtn.disabled = state.inboxCount <= 0 || state.uploading || state.processBusy;
  }
}

export function setProcessInboxBusy(busy) {
  state.processBusy = Boolean(busy);
  updateActionHierarchy();
}

export async function refreshInbox() {
  const data = await api("/api/inbox");
  setText("inbox-out", data);
  renderInboxSummary(data);
  return data;
}

function dropZoneState() {
  const zone = document.getElementById("drop-zone");
  if (!zone) return;
  let mode = "idle";
  if (state.uploading) mode = "uploading";
  else if (state.staged.some((s) => s.status === "error") && state.staged.every((s) => s.status === "error" || s.status === "waiting")) {
    if (state.staged.some((s) => s.status === "error")) mode = "partial";
  }
  if (!state.uploading && state.staged.length) {
    if (state.staged.some((s) => s.status === "error")) mode = "partial";
    else mode = "staged";
  }
  if (state.dragDepth > 0 && !state.uploading) mode = "drag-over";
  zone.dataset.state = mode;
}

function renderStaging() {
  const list = document.getElementById("staging-list");
  const meta = document.getElementById("staging-meta");
  const uploadBtn = document.getElementById("upload-staged");
  const retryBtn = document.getElementById("retry-failed");
  const clearStagedBtn = document.getElementById("clear-staged");
  const panel = document.getElementById("staging-panel");

  if (panel) panel.hidden = state.staged.length === 0;

  if (list) {
    if (!state.staged.length) {
      list.innerHTML = "";
    } else {
      list.innerHTML = state.staged
        .map((s) => {
          const statusLabel =
            s.status === "uploading"
              ? "Uploading…"
              : s.status === "uploaded"
                ? "Uploaded"
                : s.status === "error"
                  ? s.error || "Failed"
                  : "Waiting";
          return `<li class="staging-row" data-id="${escapeHtml(s.id)}" data-status="${escapeHtml(s.status)}">
            <div class="staging-main">
              <span class="staging-name">${escapeHtml(s.name)}</span>
              <span class="staging-meta-line">
                <span class="staging-type">${escapeHtml(s.type)}</span>
                <span class="staging-size">${escapeHtml(formatBytes(s.size))}</span>
                <span class="staging-status">${escapeHtml(statusLabel)}</span>
              </span>
            </div>
            <button type="button" class="btn ghost compact staging-remove" data-id="${escapeHtml(s.id)}" aria-label="Remove ${escapeHtml(s.name)}" ${state.uploading ? "disabled" : ""}>Remove</button>
          </li>`;
        })
        .join("");
    }
  }

  const waiting = state.staged.filter((s) => s.status === "waiting");
  const failed = state.staged.filter((s) => s.status === "error");
  if (meta) {
    if (!state.staged.length) meta.textContent = "";
    else {
      meta.textContent = `${state.staged.length} staged · ${formatBytes(totalStagedBytes(state.staged))}`;
    }
  }
  if (uploadBtn) {
    const n = waiting.length;
    uploadBtn.disabled = state.uploading || n === 0;
    uploadBtn.textContent = n ? `Upload ${n} scan${n === 1 ? "" : "s"}` : "Upload scans";
  }
  if (retryBtn) {
    retryBtn.hidden = failed.length === 0;
    retryBtn.disabled = state.uploading || failed.length === 0;
  }
  if (clearStagedBtn) {
    clearStagedBtn.disabled = state.uploading || state.staged.length === 0;
  }
  dropZoneState();
  updateActionHierarchy();
}

function ingestFileList(fileList) {
  const incoming = Array.from(fileList || []);
  if (!incoming.length) return;
  const result = appendFilesToStaging(state.staged, incoming);
  state.staged = result.staged;
  if (result.rejected.length) {
    toast(
      `Skipped unsupported: ${result.rejected.slice(0, 3).join(", ")}${result.rejected.length > 3 ? "…" : ""}`,
      "warn",
    );
  }
  renderStaging();
  // Keep inbox summary in sync when only staging changes
  if (state.inboxCount === 0) {
    const el = document.getElementById("inbox-summary");
    if (el && state.staged.length) {
      el.textContent = `${state.staged.length} file${state.staged.length === 1 ? "" : "s"} staged — upload to add them to the inbox`;
      el.dataset.tone = "warn";
    }
  }
}

async function uploadOne(item) {
  item.status = "uploading";
  item.error = undefined;
  renderStaging();
  try {
    await api("/api/upload", uploadRequestInit(item.file));
    item.status = "uploaded";
  } catch (err) {
    item.status = "error";
    item.error = String(err.message || err);
  }
  renderStaging();
  return item;
}

async function uploadStaged({ onlyFailed = false } = {}) {
  if (state.uploading) return;
  const targets = state.staged.filter((s) =>
    onlyFailed ? s.status === "error" : s.status === "waiting" || s.status === "error",
  );
  if (!targets.length) {
    setProcessStatus("Stage PDF or image scans first", "warn");
    return;
  }
  for (const item of targets) {
    if (item.status === "error") item.status = "waiting";
  }
  state.uploading = true;
  setProcessInboxBusy(true);
  setProcessStatus("Uploading scans…");
  renderStaging();

  try {
    await runPool(targets, UPLOAD_CONCURRENCY, (item) => uploadOne(item));
    const ok = targets.filter((s) => s.status === "uploaded").length;
    const failed = targets.filter((s) => s.status === "error").length;
    const summary = formatUploadSummary({ ok, failed });
    announceUploadOutcome(summary);
    state.staged = settleStagingAfterUpload(state.staged);
    renderStaging();
    await refreshInbox();
    if (failed && ok) {
      setProcessStatus(summary, "warn");
      toast(summary, "warn");
    } else if (failed) {
      setProcessStatus(summary, "error");
      toast(summary, "error");
    } else {
      setProcessStatus(summary, "ok");
      toast(summary, "ok");
    }
  } finally {
    state.uploading = false;
    setProcessInboxBusy(false);
    renderStaging();
  }
}

function setProcessOutcomeUi({ pendingCount, filedNames, errors, processed }) {
  const reviewLink = document.getElementById("process-review-link");
  if (reviewLink) reviewLink.hidden = true;

  if (errors?.length) {
    setProcessStatus(
      `Processed ${processed} file(s), ${errors.length} failed. ${errors.map((e) => e.error).join(" | ")}`,
      "error",
    );
    toast(`${errors.length} file(s) failed`, "error");
    return;
  }
  if (pendingCount > 0) {
    const filedNote = filedNames?.length ? ` ${filedNames.length} filed automatically.` : "";
    setProcessStatus(
      `${pendingCount} document${pendingCount === 1 ? "" : "s"} waiting in Review.${filedNote}`,
      "ok",
    );
    toast(`${pendingCount} document(s) ready for review`, "ok");
    if (reviewLink) reviewLink.hidden = false;
    return;
  }
  if (filedNames?.length) {
    setProcessStatus(
      `Filed automatically: ${filedNames.join(", ")}`,
      "ok",
    );
    toast(`Filed ${filedNames.length} document(s)`, "ok");
    return;
  }
  setProcessStatus(`Processed ${processed} file(s)`, "ok");
  toast(`Processed ${processed} file(s)`, "ok");
}

export function initInbox() {
  mountInboxShell();
  const fileInput = document.getElementById("file");
  const dropZone = document.getElementById("drop-zone");

  renderStaging();

  fileInput?.addEventListener("change", () => {
    if (fileInput.files?.length) {
      ingestFileList(fileInput.files);
      fileInput.value = "";
    }
  });

  if (dropZone) {
    dropZone.addEventListener("dragenter", (e) => {
      e.preventDefault();
      state.dragDepth += 1;
      dropZoneState();
    });
    dropZone.addEventListener("dragover", (e) => {
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    });
    dropZone.addEventListener("dragleave", (e) => {
      e.preventDefault();
      state.dragDepth = Math.max(0, state.dragDepth - 1);
      dropZoneState();
    });
    dropZone.addEventListener("drop", (e) => {
      e.preventDefault();
      e.stopPropagation();
      state.dragDepth = 0;
      dropZoneState();
      if (e.dataTransfer?.files?.length) ingestFileList(e.dataTransfer.files);
      else ingestDesktopUriDrop(e).catch(() => {});
    });
  }

  document.getElementById("upload-form")?.addEventListener("submit", (e) => {
    e.preventDefault();
    uploadStaged({ onlyFailed: false });
  });

  document.getElementById("upload-staged")?.addEventListener("click", () => {
    uploadStaged({ onlyFailed: false });
  });

  document.getElementById("retry-failed")?.addEventListener("click", () => {
    uploadStaged({ onlyFailed: true });
  });

  document.getElementById("clear-staged")?.addEventListener("click", () => {
    if (state.uploading) return;
    state.staged = [];
    renderStaging();
    refreshInbox().catch(() => {});
  });

  document.getElementById("staging-list")?.addEventListener("click", (e) => {
    const btn = e.target.closest(".staging-remove");
    if (!btn || state.uploading) return;
    state.staged = removeStagedFile(state.staged, btn.dataset.id);
    renderStaging();
  });

  document.getElementById("refresh-inbox")?.addEventListener("click", () => {
    refreshInbox().catch((err) => {
      const msg = String(err.message || err);
      setText("inbox-out", msg);
      setProcessStatus(msg, "error");
    });
  });

  window.addEventListener("dc-desktop-drop", (event) => {
    const detail = event.detail || {};
    const ok = Array.isArray(detail.ok) ? detail.ok : [];
    const errors = Array.isArray(detail.errors) ? detail.errors : [];
    if (ok.length) {
      toast(`Added ${ok.length} scan${ok.length === 1 ? "" : "s"} to inbox`, "ok");
      refreshInbox().catch(() => {});
    }
    if (errors.length) {
      toast(errors.slice(0, 2).join(" · "), "warn");
    }
  });

  document.getElementById("clear-inbox")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    if (!armedConfirm(btn, "Really remove?")) return;
    try {
      const data = await api("/api/inbox", { method: "DELETE" });
      setText("process-out", data);
      const count = data.removed_count ?? 0;
      setProcessStatus(
        count ? `Removed ${count} file${count === 1 ? "" : "s"} from inbox` : "Inbox already empty",
        count ? "ok" : "warn",
      );
      await refreshInbox();
    } catch (err) {
      const msg = String(err.message || err);
      setText("process-out", msg);
      setProcessStatus(msg, "error");
      toast(msg, "error");
    }
  });

  document.getElementById("job-queue")?.addEventListener("click", async (ev) => {
    const cancelBtn = ev.target.closest(".queue-cancel");
    if (cancelBtn) {
      const fileId = cancelBtn.dataset.fileId;
      if (!fileId) return;
      cancelBtn.disabled = true;
      const prevLabel = cancelBtn.textContent;
      cancelBtn.textContent = "Cancelling…";
      try {
        await api("/api/process/cancel", {
          method: "POST",
          body: { file_id: fileId },
        });
        toast("Cancellation requested", "ok");
        const row = workflowState.queue.find((q) => q.file_id === fileId);
        if (row) {
          row.stepLabel = "Cancelling…";
          patchQueueRow(fileId);
        }
      } catch (err) {
        cancelBtn.disabled = false;
        cancelBtn.textContent = prevLabel;
        toast(errorMessage(err, "Could not cancel this file"), "error");
      }
      return;
    }
    const retryBtn = ev.target.closest(".queue-retry");
    if (retryBtn) {
      const path = retryBtn.dataset.path;
      if (!path) return;
      retryBtn.disabled = true;
      try {
        const data = await api("/api/process/retry", {
          method: "POST",
          body: { path },
        });
        if (data.cancelled) {
          toast(data.message || "Cancelled — retry again when the job finishes", "warn");
        } else {
          toast("Retry started", "ok");
          setProcessInboxBusy(true);
        }
        await refreshInbox();
      } catch (err) {
        toast(String(err.message || err), "error");
      } finally {
        retryBtn.disabled = false;
      }
    }
  });

  document.getElementById("process-inbox")?.addEventListener("click", async () => {
    setProcessInboxBusy(true);
    setProcessStatus("Processing inbox… watch the workflow.");
    setText("process-out", "Processing inbox…");
    const reviewLink = document.getElementById("process-review-link");
    if (reviewLink) reviewLink.hidden = true;
    try {
      const data = await api("/api/process-inbox", { method: "POST" });
      setText("process-out", data);
      if (data.status === "empty" || data.processed === 0) {
        setProcessStatus(data.message || "Inbox is empty — upload a scan first", "warn");
      } else {
        const results = data.results || [];
        const errors = results.filter((r) => r.error);
        const pending = results.filter((r) => r.result?.status === "pending_review");
        const filed = results
          .filter((r) => !r.error && r.result?.result?.filename)
          .map((r) => r.result.result.filename);
        setProcessOutcomeUi({
          pendingCount: pending.length,
          filedNames: filed,
          errors,
          processed: data.processed,
        });
      }
      await refreshInbox();
      await hooks.refreshDocs();
      await hooks.refreshReviews().catch(() => {});
    } catch (err) {
      const msg = String(err.message || err);
      setText("process-out", msg);
      setProcessStatus(msg, "error");
      toast(msg, "error");
    } finally {
      setProcessInboxBusy(false);
    }
  });
}
