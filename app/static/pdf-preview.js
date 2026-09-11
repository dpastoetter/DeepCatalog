import { escapeHtml } from "./api.js";
import * as pdfjs from "./vendor/pdfjs/pdf.mjs";

pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  "./vendor/pdfjs/pdf.worker.mjs",
  import.meta.url,
).href;

/**
 * Render a PDF into `container` with PDF.js (fit-to-width canvases).
 * `source` is an ArrayBuffer/Uint8Array or same-origin URL string.
 * Returns an unmount function that cancels in-flight work and frees the doc.
 */
export function mountPdfPreview(container, source, options = {}) {
  if (!container) return () => {};

  const scrollClass = options.scrollClass || "pdf-preview-scroll";
  const pageClass = options.pageClass || "pdf-preview-page";

  // Copy bytes — PDF.js may transfer/detach the caller's ArrayBuffer.
  let docInit;
  if (source instanceof ArrayBuffer) {
    docInit = { data: source.slice(0) };
  } else if (source instanceof Uint8Array) {
    docInit = { data: source.slice(0) };
  } else {
    docInit = { url: source };
  }

  let cancelled = false;
  let debounceTimer = 0;
  let resizeObserver = null;
  /** @type {import("./vendor/pdfjs/pdf.mjs").PDFDocumentProxy | null} */
  let pdfDoc = null;
  let renderGeneration = 0;
  let lastRenderWidth = 0;
  let rendering = false;

  const scroll = document.createElement("div");
  scroll.className = scrollClass;
  scroll.setAttribute("role", "document");
  container.replaceChildren(scroll);
  scroll.innerHTML = `<div class="pdf-preview-loading">Loading preview…</div>`;

  const showError = (err) => {
    scroll.innerHTML = `<div class="review-preview-placeholder review-preview-error">
      <p>Could not render PDF preview</p>
      <p class="fine">${escapeHtml(String(err?.message || err || "Unknown error"))}</p>
    </div>`;
  };

  const render = async () => {
    const width = Math.floor(container.clientWidth);
    if (width < 48) return;
    // WebKit fires ResizeObserver when page canvases change height; ignore
    // height-only churn so we do not clear/redraw in a flicker loop.
    if (pdfDoc && width === lastRenderWidth) return;

    const generation = ++renderGeneration;
    rendering = true;

    const hadPages = Boolean(scroll.querySelector("canvas"));
    if (!hadPages) {
      scroll.innerHTML = `<div class="pdf-preview-loading">Loading preview…</div>`;
    }

    try {
      if (!pdfDoc) {
        const task = pdfjs.getDocument(docInit);
        pdfDoc = await task.promise;
      }
      if (cancelled || generation !== renderGeneration) return;

      const pageCount = pdfDoc.numPages;
      const next = document.createDocumentFragment();

      for (let pageNum = 1; pageNum <= pageCount; pageNum += 1) {
        if (cancelled || generation !== renderGeneration) return;

        const page = await pdfDoc.getPage(pageNum);
        const base = page.getViewport({ scale: 1 });
        const scale = Math.max(0.1, (width - 8) / base.width);
        const viewport = page.getViewport({ scale });

        const canvas = document.createElement("canvas");
        canvas.className = pageClass;
        canvas.width = Math.floor(viewport.width);
        canvas.height = Math.floor(viewport.height);
        canvas.setAttribute(
          "aria-label",
          pageCount > 1 ? `Page ${pageNum} of ${pageCount}` : "Document page",
        );

        const ctx = canvas.getContext("2d", { alpha: false });
        if (!ctx) throw new Error("Canvas is not available");

        await page.render({ canvasContext: ctx, viewport }).promise;
        if (cancelled || generation !== renderGeneration) return;
        next.appendChild(canvas);
      }

      if (cancelled || generation !== renderGeneration) return;
      scroll.replaceChildren(next);
      lastRenderWidth = width;
    } catch (err) {
      if (cancelled || generation !== renderGeneration) return;
      showError(err);
    } finally {
      if (generation === renderGeneration) {
        rendering = false;
      }
    }
  };

  const schedule = () => {
    if (cancelled) return;
    window.clearTimeout(debounceTimer);
    debounceTimer = window.setTimeout(() => {
      const width = Math.floor(container.clientWidth);
      if (width < 48) return;
      if (pdfDoc && width === lastRenderWidth) return;
      if (rendering && width === lastRenderWidth) return;
      render().catch((err) => {
        if (!cancelled) showError(err);
      });
    }, 120);
  };

  if (typeof ResizeObserver !== "undefined") {
    resizeObserver = new ResizeObserver(() => {
      // Ignore observations caused by our own canvas swaps while rendering.
      if (rendering) return;
      schedule();
    });
    resizeObserver.observe(container);
  }
  schedule();
  requestAnimationFrame(() => requestAnimationFrame(schedule));

  return () => {
    cancelled = true;
    renderGeneration += 1;
    window.clearTimeout(debounceTimer);
    resizeObserver?.disconnect();
    if (pdfDoc) {
      pdfDoc.destroy().catch(() => {});
      pdfDoc = null;
    }
    scroll.replaceChildren();
  };
}
