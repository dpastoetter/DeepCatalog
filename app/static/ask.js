import { api, escapeHtml, refreshHealth, toast } from "./api.js";
import { setHashQuery } from "./router.js";

export const ASK_HISTORY_MAX_TURNS = 6;
export const ASK_EXAMPLES_STORAGE_KEY = "dc-ask-examples";
export const ASK_EXAMPLES = [
  "What invoices did I receive this year?",
  "Find my most recent insurance documents",
  "Summarize letters from the last few months",
  "Which documents mention taxes or annual statements?",
];

const state = {
  turns: /** @type {AskTurn[]} */ ([]),
  busy: false,
  draft: "",
};

/** Browser preference — default on. */
export function areAskExamplesEnabled() {
  try {
    const stored = localStorage.getItem(ASK_EXAMPLES_STORAGE_KEY);
    if (stored === null) return true;
    return stored !== "0" && stored !== "false";
  } catch (_err) {
    return true;
  }
}

/** Persist preference and refresh the Ask empty state when idle. */
export function setAskExamplesEnabled(enabled) {
  try {
    localStorage.setItem(ASK_EXAMPLES_STORAGE_KEY, enabled ? "1" : "0");
  } catch (_err) {
    // private mode — preference won't persist
  }
  if (!state.turns.length) renderThread();
}

/** @typedef {{ id: string, question: string, reply?: string, sources?: any[], evidence?: string, status?: string, error?: string, loading?: boolean }} AskTurn */

export function stripSourcesSection(text) {
  return String(text || "")
    .replace(/\n*(?:#{1,3}\s*)?sources\b[\s\S]*$/i, "")
    .trim();
}

export function sanitizeDocumentOpenUrl(candidate, documentId) {
  const id = encodeURIComponent(documentId || "");
  const fallback = `/api/documents/${id}/file`;
  if (typeof candidate !== "string") return fallback;
  if (!candidate.startsWith("/api/documents/") || candidate.includes("://")) {
    return fallback;
  }
  return candidate;
}

export function historyPayloadFromTurns(turns, limit = ASK_HISTORY_MAX_TURNS) {
  const completed = (turns || []).filter((t) => t.reply && !t.loading && t.status !== "error");
  const slice = completed.slice(-Math.floor(limit / 2));
  const history = [];
  for (const turn of slice) {
    history.push({ role: "user", content: turn.question });
    history.push({ role: "assistant", content: stripSourcesSection(turn.reply) });
  }
  // Cap to limit turns (user+assistant pairs count as 2 each)
  if (history.length > limit) {
    return history.slice(-limit);
  }
  return history;
}

export function evidenceLabel(evidence, grounded) {
  if (evidence === "none" || grounded === false) {
    return "No relevant documents found in your archive.";
  }
  if (evidence === "weak") {
    return "Limited supporting evidence found in your archive.";
  }
  if (evidence === "strong") {
    return "";
  }
  return "";
}

export function classifyClientEvidence(data) {
  if (!data) return "none";
  if (data.evidence === "none" || data.evidence === "weak" || data.evidence === "strong") {
    return data.evidence;
  }
  if (data.grounded === false) return "none";
  if ((data.retrieval_count || 0) > 0) return "strong";
  if ((data.metadata_count || 0) > 0 || (data.sources || []).length) return "weak";
  return "none";
}

function announce(message) {
  const live = document.getElementById("ask-live");
  if (!live) return;
  live.textContent = "";
  window.requestAnimationFrame(() => {
    live.textContent = message || "";
  });
}

function sourceItemHtml(s) {
  const id = s.document_id;
  const name = escapeHtml(s.filename || "document");
  const openUrl = sanitizeDocumentOpenUrl(s.open_url, id);
  const badge = s.doc_type ? `<span class="doc-badge">${escapeHtml(s.doc_type)}</span>` : "";
  const date = s.doc_date ? `<span class="ask-source-date">${escapeHtml(s.doc_date)}</span>` : "";
  const snippet = s.snippet
    ? `<p class="ask-source-snippet">${escapeHtml(s.snippet)}</p>`
    : "";
  return `<li class="ask-source-item">
    <div class="ask-source-body">
      <div class="ask-source-title-row">${badge}<span class="ask-source-name">${name}</span>${date}</div>
      ${snippet}
    </div>
    <div class="ask-source-actions">
      <button type="button" class="btn ghost compact source-view" data-doc-id="${escapeHtml(id)}">View in Archive</button>
      <a class="source-link btn ghost compact" href="${escapeHtml(openUrl)}" data-open-url="${escapeHtml(openUrl)}">
        <svg class="icon" aria-hidden="true"><use href="#i-external" /></svg>
        Open
      </a>
      <button type="button" class="btn ghost compact source-reveal" data-doc-id="${escapeHtml(id)}">Reveal</button>
    </div>
  </li>`;
}

function turnHtml(turn) {
  const evidence = turn.evidence || "none";
  const note = evidenceLabel(evidence, turn.grounded);
  const noteHtml = note
    ? `<p class="ask-evidence-note" data-evidence="${escapeHtml(evidence)}">${escapeHtml(note)}</p>`
    : "";

  let body;
  if (turn.loading) {
    body = `<div class="ask-answer" data-state="searching">Searching archive…</div>`;
  } else if (turn.status === "error") {
    body = `<div class="ask-answer" data-state="error">${escapeHtml(turn.error || turn.reply || "Request failed")}</div>`;
  } else {
    const reply = escapeHtml(stripSourcesSection(turn.reply || ""));
    const sources = (turn.sources || []).filter((s) => s && s.document_id);
    const sourcesHtml = sources.length
      ? `<div class="ask-sources">
          <h3>Sources</h3>
          <ul>${sources.map(sourceItemHtml).join("")}</ul>
        </div>`
      : "";
    body = `${noteHtml}<div class="ask-answer" data-evidence="${escapeHtml(evidence)}">${reply}</div>${sourcesHtml}`;
  }

  return `<article class="ask-turn" data-evidence="${escapeHtml(evidence)}" id="ask-turn-${escapeHtml(turn.id)}">
    <h3 class="ask-turn-question">${escapeHtml(turn.question)}</h3>
    ${body}
  </article>`;
}

function examplesHtml() {
  if (!areAskExamplesEnabled()) {
    return `<div class="empty-state ask-empty" id="ask-empty">
      <svg class="icon" aria-hidden="true"><use href="#i-ask" /></svg>
      <p>Research your archive</p>
      <p class="fine">Ask a question below.</p>
    </div>`;
  }
  return `<div class="empty-state ask-empty" id="ask-empty">
    <svg class="icon" aria-hidden="true"><use href="#i-ask" /></svg>
    <p>Research your archive</p>
    <p class="fine">Try one of these:</p>
    <ul class="ask-examples">
      ${ASK_EXAMPLES.map(
        (q) =>
          `<li><button type="button" class="ask-example" data-question="${escapeHtml(q)}">${escapeHtml(q)}</button></li>`,
      ).join("")}
    </ul>
  </div>`;
}

export function syncAskComposerSize(el = document.getElementById("question")) {
  if (!el) return;
  el.style.height = "auto";
  const cap = Number.parseFloat(getComputedStyle(el).maxHeight);
  const maxPx = Number.isFinite(cap) && cap > 0 ? cap : 128;
  el.style.height = `${Math.min(el.scrollHeight, maxPx)}px`;
}

function renderThread() {
  const root = document.getElementById("ask-thread");
  const clearBtn = document.getElementById("ask-clear");
  if (!root) return;

  root.classList.toggle("ask-thread--empty", state.turns.length === 0);

  if (!state.turns.length) {
    root.innerHTML = examplesHtml();
    if (clearBtn) clearBtn.hidden = true;
    return;
  }

  root.innerHTML = state.turns.map(turnHtml).join("");
  if (clearBtn) clearBtn.hidden = false;
}

/** Seed the thread from mock/canned data (used by screenshot mode). */
export function renderAskResult(data) {
  const reply = stripSourcesSection(data?.reply || "");
  const question =
    document.getElementById("question")?.value.trim() ||
    "Which invoices did I get from Acme this quarter?";
  state.turns = [
    {
      id: "mock-1",
      question,
      reply,
      sources: data?.sources || [],
      evidence: classifyClientEvidence(data),
      grounded: data?.grounded !== false,
      status: data?.status === "error" ? "error" : "success",
    },
  ];
  renderThread();
}

export function clearAskConversation() {
  state.turns = [];
  state.busy = false;
  const input = document.getElementById("question");
  if (input) input.value = "";
  syncAskComposerSize(input);
  const submit = document.getElementById("ask-submit");
  if (submit) submit.disabled = false;
  renderThread();
  announce("Conversation cleared");
}

async function submitQuestion(rawQuestion) {
  const question = String(rawQuestion || "").trim();
  if (!question || state.busy) return;

  const input = document.getElementById("question");
  const submit = document.getElementById("ask-submit");
  state.draft = question;
  if (input) {
    input.value = "";
    syncAskComposerSize(input);
  }

  const turnId = `t-${Date.now()}`;
  const history = historyPayloadFromTurns(state.turns);
  state.turns.push({
    id: turnId,
    question,
    loading: true,
    evidence: "none",
  });
  state.busy = true;
  if (submit) submit.disabled = true;
  renderThread();
  announce("Searching archive");

  try {
    const data = await api("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, history }),
    });
    const turn = state.turns.find((t) => t.id === turnId);
    if (!turn) return;
    turn.loading = false;
    if (data.status === "error" || !data.reply) {
      turn.status = "error";
      turn.error = data.reply || data.error || "No answer returned";
      turn.evidence = classifyClientEvidence(data);
      if (input) {
        input.value = state.draft;
        syncAskComposerSize(input);
      }
      announce("Ask failed");
    } else {
      turn.status = "success";
      turn.reply = data.reply;
      turn.sources = data.sources || [];
      turn.grounded = data.grounded;
      turn.evidence = classifyClientEvidence(data);
      state.draft = "";
      announce("Answer ready");
    }
    renderThread();
    refreshHealth().catch(() => {});
  } catch (err) {
    const turn = state.turns.find((t) => t.id === turnId);
    if (turn) {
      turn.loading = false;
      turn.status = "error";
      turn.error = String(err.message || err);
      turn.evidence = "none";
    }
    if (input) {
      input.value = state.draft;
      syncAskComposerSize(input);
    }
    renderThread();
    announce("Ask failed");
  } finally {
    state.busy = false;
    if (submit) submit.disabled = false;
  }
}

/**
 * Fetch a same-origin document API URL into a blob object URL.
 * Callers must revoke via the returned `revoke()` (or rely on delayed cleanup).
 */
export async function fetchDocumentBlob(url) {
  const res = await fetch(url);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail || data.error || detail;
    } catch (_err) {
      // ignore
    }
    throw new Error(detail || "Could not open file");
  }
  const bytes = await res.arrayBuffer();
  const headerType = (res.headers.get("content-type") || "").split(";")[0].trim();
  const head = new Uint8Array(bytes, 0, Math.min(bytes.byteLength, 5));
  const isPdf =
    head.length >= 4 &&
    head[0] === 0x25 &&
    head[1] === 0x50 &&
    head[2] === 0x44 &&
    head[3] === 0x46; // %PDF
  const mime = isPdf ? "application/pdf" : headerType || "application/octet-stream";
  const objectUrl = URL.createObjectURL(new Blob([bytes], { type: mime }));
  return {
    objectUrl,
    bytes,
    mime,
    isPdf,
    revoke: () => {
      try {
        URL.revokeObjectURL(objectUrl);
      } catch (_err) {
        // ignore
      }
    },
  };
}

/** Map a document/review file URL to the OS-open API endpoint. */
export function documentOsOpenUrl(fileUrl) {
  if (typeof fileUrl !== "string") return null;
  const doc = fileUrl.match(/^\/api\/documents\/([^/?#]+)\/file\/?$/);
  if (doc) {
    return `/api/documents/${encodeURIComponent(decodeURIComponent(doc[1]))}/open`;
  }
  const review = fileUrl.match(/^\/api\/reviews\/([^/?#]+)\/file\/?$/);
  if (review) {
    return `/api/reviews/${encodeURIComponent(decodeURIComponent(review[1]))}/open`;
  }
  return null;
}

export async function openDocumentFile(url) {
  if (window.DC_MOCK?.enabled) {
    throw new Error("Mockup mode is on — files are demo data. Turn it off in Settings.");
  }
  // Prefer the OS handler: AppImage WebKit often blocks blob popups / secondary
  // windows, and host PDF viewers need a clean (non-WebKit) process env.
  const osUrl = documentOsOpenUrl(url);
  if (osUrl) {
    await api(osUrl, { method: "POST" });
    return;
  }
  const win = window.open("about:blank", "_blank");
  if (!win) {
    throw new Error("Popup blocked — allow popups for this site to open documents");
  }
  try {
    win.document.title = "Loading…";
  } catch (_err) {
    // ignore
  }
  try {
    const { objectUrl } = await fetchDocumentBlob(url);
    win.location.href = objectUrl;
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
  } catch (err) {
    try {
      win.close();
    } catch (_closeErr) {
      // ignore
    }
    throw err;
  }
}

export function askShellHtml() {
  return `<div class="ask-workspace">
          <div id="ask-thread" class="ask-thread ask-thread--empty" aria-label="Archive research thread"></div>
          <p id="ask-live" class="sr-only" aria-live="polite"></p>

          <form id="ask-form" class="ask-composer">
            <textarea id="question" rows="1" aria-label="Question" placeholder="Ask about invoices, policies, counterparties…"></textarea>
            <button type="submit" class="btn primary" id="ask-submit">Ask</button>
          </form>
        </div>`;
}

export function mountAskShell() {
  const host = document.getElementById("ask");
  if (!host || host.querySelector("#ask-form")) return host;
  host.innerHTML = askShellHtml();
  return host;
}

export function initAsk() {
  mountAskShell();
  renderThread();

  const form = document.getElementById("ask-form");
  const input = document.getElementById("question");
  const thread = document.getElementById("ask-thread");

  form?.addEventListener("submit", (e) => {
    e.preventDefault();
    submitQuestion(input?.value);
  });

  input?.addEventListener("input", () => syncAskComposerSize(input));
  syncAskComposerSize(input);

  input?.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    if (e.shiftKey) return;
    e.preventDefault();
    if (!state.busy) submitQuestion(input.value);
  });

  document.getElementById("ask-clear")?.addEventListener("click", () => {
    clearAskConversation();
  });

  thread?.addEventListener("click", async (e) => {
    const example = e.target.closest(".ask-example");
    if (example) {
      const q = example.dataset.question || example.textContent || "";
      await submitQuestion(q);
      return;
    }

    const viewBtn = e.target.closest(".source-view");
    if (viewBtn) {
      const docId = viewBtn.dataset.docId;
      if (docId) {
        setHashQuery("archive", { doc: docId }, { replace: false });
      }
      return;
    }

    const link = e.target.closest(".source-link");
    if (link) {
      e.preventDefault();
      const url = link.dataset.openUrl || link.getAttribute("href");
      if (!url) return;
      link.setAttribute("aria-busy", "true");
      try {
        await openDocumentFile(url);
      } catch (err) {
        toast(String(err.message || err), "error");
      } finally {
        link.removeAttribute("aria-busy");
      }
      return;
    }

    const btn = e.target.closest(".source-reveal");
    if (!btn) return;
    const docId = btn.dataset.docId;
    if (!docId) return;
    btn.disabled = true;
    try {
      await api(`/api/documents/${encodeURIComponent(docId)}/reveal`, { method: "POST" });
    } catch (err) {
      toast(String(err.message || err), "error");
    } finally {
      btn.disabled = false;
    }
  });
}
