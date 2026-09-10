import { setBackgroundInert } from "./keyboard.js";

export function formatApiError(data, fallback = "Request failed") {
  const detail = data?.detail ?? data?.error;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => {
        if (typeof item === "string") return item;
        if (item && typeof item.msg === "string") return item.msg;
        return null;
      })
      .filter(Boolean);
    if (parts.length) return parts.join("; ");
  }
  if (detail && typeof detail === "object") {
    if (typeof detail.message === "string" && detail.message.trim()) return detail.message;
    if (typeof detail.error === "string" && detail.error.trim()) return detail.error;
    if (typeof detail.msg === "string" && detail.msg.trim()) return detail.msg;
  }
  return fallback;
}

export function errorMessage(err, fallback = "Something went wrong") {
  if (err == null) return fallback;
  if (typeof err === "string") return err.trim() || fallback;
  if (typeof err.message === "string") {
    const msg = err.message.trim();
    if (msg && msg !== "[object Object]") return msg;
  }
  if (typeof err.detail !== "undefined") {
    return formatApiError({ detail: err.detail }, fallback);
  }
  if (typeof err.error !== "undefined") {
    return formatApiError({ error: err.error }, fallback);
  }
  try {
    const encoded = JSON.stringify(err);
    if (encoded && encoded !== "{}" && encoded !== "[]") return encoded;
  } catch (_err) {
    // ignore
  }
  return fallback;
}

export function displayText(value, fallback = "") {
  if (value == null || value === "") return fallback;
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return errorMessage(value, fallback);
}

export async function api(path, options = {}) {
  // Mockup mode (Settings → Appearance): serve canned demo data, block writes.
  if (window.DC_MOCK?.enabled) {
    return window.DC_MOCK.respond(path, options);
  }
  const headers = new Headers(options.headers || {});
  // Custom header required by the server on mutating routes — blocks CSRF
  // from cross-site form posts (browsers cannot attach it without CORS).
  headers.set("X-Requested-With", "DeepCatalog");
  // Auth is the HttpOnly deepcatalog_session cookie (set via POST /api/auth/session or
  // the desktop one-time bootstrap). Never put DEEPCATALOG_API_TOKEN in JS / sessionStorage.
  let body = options.body;
  if (
    body != null &&
    typeof body === "object" &&
    !(body instanceof FormData) &&
    !(body instanceof URLSearchParams) &&
    !(body instanceof Blob) &&
    !(body instanceof ArrayBuffer)
  ) {
    body = JSON.stringify(body);
    headers.set("Content-Type", "application/json");
  } else if (typeof body === "string" && body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  // WebKitGTK drops a Headers instance on multipart FormData; a plain object is kept.
  const headerInit = Object.fromEntries(headers.entries());
  const res = await fetch(path, { ...options, headers: headerInit, body, credentials: "same-origin" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(formatApiError(data, res.statusText || "Request failed"));
  }
  if (data && data.status === "error") {
    throw new Error(formatApiError(data, "Request failed"));
  }
  return data;
}

export async function ensureBrowserSession() {
  /** If API auth is required and no session cookie yet, show the unlock panel. */
  try {
    const status = await api("/api/auth/session/status");
    if (!status.auth_required || status.authenticated) {
      hideSessionUnlock();
      return true;
    }
    showSessionUnlock();
    return false;
  } catch (_err) {
    // Fail open for local use; never leave a stuck unlock overlay / inert shell.
    hideSessionUnlock();
    return true;
  }
}

function showSessionUnlock() {
  const panel = document.getElementById("session-unlock");
  if (!panel) return;
  panel.hidden = false;
  panel.classList.remove("hidden");
  const input = document.getElementById("session-unlock-token");
  if (input) input.disabled = false;
  setBackgroundInert(true, { mode: "shell" });
  window.requestAnimationFrame(() => {
    input?.focus();
  });
}

function hideSessionUnlock() {
  const panel = document.getElementById("session-unlock");
  if (panel) {
    panel.hidden = true;
    panel.classList.add("hidden");
  }
  const input = document.getElementById("session-unlock-token");
  if (input) {
    input.disabled = true;
    input.value = "";
  }
  // Always clear shell inert — even if the panel was already hidden.
  setBackgroundInert(false, { mode: "shell" });
}

export function initSessionUnlock() {
  // Ensure the unlock overlay cannot block local use before session check finishes.
  hideSessionUnlock();
  const form = document.getElementById("session-unlock-form");
  if (!form) return;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = document.getElementById("session-unlock-token");
    const errEl = document.getElementById("session-unlock-error");
    const token = (input?.value || "").trim();
    if (!token) return;
    try {
      if (errEl) errEl.textContent = "";
      await api("/api/auth/session", { method: "POST", body: { token } });
      if (input) input.value = "";
      hideSessionUnlock();
      window.location.reload();
    } catch (err) {
      if (errEl) errEl.textContent = String(err.message || err);
    }
  });
}

export function setText(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

export function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

const FINANCIAL_DOC_TYPES = new Set([
  "invoice",
  "receipt",
  "bank",
  "tax",
  "utility",
  "insurance",
]);

export function isFinancialDocType(docType) {
  return FINANCIAL_DOC_TYPES.has(String(docType || "other").toLowerCase());
}

export function referenceIdsToString(ids) {
  if (Array.isArray(ids)) {
    return ids.map((item) => String(item).trim()).filter(Boolean).join(", ");
  }
  if (typeof ids === "string") return ids.trim();
  return "";
}

export function setStatus(state, label) {
  const chip = document.getElementById("status-chip");
  const health = document.getElementById("health");
  if (chip) chip.dataset.state = state;
  if (health) health.textContent = label;
}

function formatCompactCount(value) {
  const n = Number(value) || 0;
  if (n >= 1_000_000) {
    const m = n / 1_000_000;
    return `${m >= 10 ? Math.round(m) : m.toFixed(1).replace(/\.0$/, "")}M`;
  }
  if (n >= 1_000) {
    const k = n / 1_000;
    return `${k >= 10 ? Math.round(k) : k.toFixed(1).replace(/\.0$/, "")}k`;
  }
  return String(n);
}

function formatUsageMetrics(usage) {
  if (!usage) return "";
  const requests = Number(usage.requests) || 0;
  const tokens = Number(usage.total_tokens) || 0;
  const reqLabel = `${formatCompactCount(requests)} req`;
  if (tokens <= 0) return reqLabel;
  return `${reqLabel} · ${formatCompactCount(tokens)} tok`;
}

export function renderUsageMetrics(usage) {
  const el = document.getElementById("usage-metrics");
  if (!el) return;
  const text = formatUsageMetrics(usage);
  if (!text) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.textContent = text;
  el.title = usage
    ? [
        `Requests: ${usage.requests || 0}`,
        `Prompt tokens: ${usage.prompt_tokens || 0}`,
        `Completion tokens: ${usage.completion_tokens || 0}`,
        `Total tokens: ${usage.total_tokens || 0}`,
      ].join("\n")
    : "";
}

/* ————— Toasts ————— */

export function toast(message, tone = "info", timeout = 4200) {
  const stack = document.getElementById("toast-stack");
  if (!stack) return;
  const el = document.createElement("div");
  el.className = "toast";
  el.dataset.tone = tone;
  const text = displayText(message, "Something went wrong");
  const icon =
    tone === "error" || tone === "warn"
      ? "i-alert"
      : tone === "ok"
        ? "i-check"
        : "i-file";
  el.innerHTML = `<svg class="icon" aria-hidden="true"><use href="#${icon}" /></svg><span>${escapeHtml(text)}</span>`;
  stack.appendChild(el);
  window.setTimeout(() => {
    el.dataset.leaving = "true";
    window.setTimeout(() => el.remove(), 240);
  }, timeout);
}

/* Two-step destructive confirm: first click arms the button, second confirms. */
const armedTimers = new WeakMap();

export function armedConfirm(btn, armedLabel) {
  if (btn.dataset.armed === "true") {
    window.clearTimeout(armedTimers.get(btn));
    delete btn.dataset.armed;
    btn.textContent = btn.dataset.originalLabel || btn.textContent;
    return true;
  }
  btn.dataset.originalLabel = btn.textContent.trim();
  btn.dataset.armed = "true";
  btn.textContent = armedLabel;
  armedTimers.set(
    btn,
    window.setTimeout(() => {
      delete btn.dataset.armed;
      btn.textContent = btn.dataset.originalLabel;
    }, 4000),
  );
  return false;
}

/* ————— Provider / auth status ————— */

export let lastProvider = "";
export let cloudDisclaimerAccepted = false;
/** When true, Settings is showing the Remote Ollama panel (documents leave this machine). */
export let ollamaRemoteMode = false;

function setCloudAuthLocked(locked) {
  const panel = document.getElementById("cloud-auth-panel");
  const checkbox = document.getElementById("cloud-disclaimer-accept");
  if (panel) panel.dataset.locked = locked ? "true" : "false";
  if (checkbox) checkbox.checked = !locked;
  for (const id of ["oauth-start", "auth-toggle"]) {
    const el = document.getElementById(id);
    if (el) el.disabled = locked;
  }
  const apiKey = document.getElementById("api-key");
  const apiKeyForm = document.getElementById("api-key-form");
  const saveBtn = apiKeyForm?.querySelector('button[type="submit"]');
  if (apiKey) apiKey.disabled = locked;
  if (saveBtn) saveBtn.disabled = locked;
  if (locked) {
    const details = document.getElementById("auth-details");
    const toggle = document.getElementById("auth-toggle");
    if (details) details.classList.add("hidden");
    if (toggle) toggle.textContent = "More options";
  }
}

export function applyCloudDisclaimerStatus(status) {
  cloudDisclaimerAccepted = Boolean(status?.accepted);
  const box = document.getElementById("cloud-disclaimer");
  if (box) box.dataset.accepted = cloudDisclaimerAccepted ? "true" : "false";
  setCloudAuthLocked(!cloudDisclaimerAccepted);
  return cloudDisclaimerAccepted;
}

export async function refreshCloudDisclaimer() {
  try {
    const data = await api("/api/privacy/cloud-disclaimer");
    return applyCloudDisclaimerStatus(data.cloud_disclaimer);
  } catch {
    setCloudAuthLocked(true);
    return false;
  }
}

export function setProviderUi(provider, { remoteOllama = false } = {}) {
  lastProvider = provider || "";
  ollamaRemoteMode = provider === "ollama" && Boolean(remoteOllama);
  const section =
    document.getElementById("auth-section") || document.getElementById("settings-ai");
  const ollamaPanel = document.getElementById("ollama-panel");
  const cloudPanel = document.getElementById("cloud-auth-panel");
  const remoteFields = document.getElementById("ollama-remote-fields");
  const cloudBtn = document.getElementById("provider-cloud");
  const ollamaBtn = document.getElementById("provider-ollama");
  const remoteBtn = document.getElementById("provider-ollama-remote");
  const isOllama = provider === "ollama";

  if (section) section.dataset.provider = provider || "";
  if (ollamaPanel) ollamaPanel.classList.toggle("hidden", !isOllama);
  // Remote Ollama reuses the cloud privacy disclaimer (documents leave this machine).
  if (cloudPanel) cloudPanel.classList.toggle("hidden", isOllama && !ollamaRemoteMode);
  if (remoteFields) remoteFields.classList.toggle("hidden", !ollamaRemoteMode);
  if (cloudBtn) {
    cloudBtn.dataset.active = !isOllama ? "true" : "false";
    cloudBtn.setAttribute("aria-pressed", !isOllama ? "true" : "false");
  }
  if (ollamaBtn) {
    ollamaBtn.dataset.active = isOllama && !ollamaRemoteMode ? "true" : "false";
    ollamaBtn.setAttribute("aria-pressed", isOllama && !ollamaRemoteMode ? "true" : "false");
  }
  if (remoteBtn) {
    remoteBtn.dataset.active = ollamaRemoteMode ? "true" : "false";
    remoteBtn.setAttribute("aria-pressed", ollamaRemoteMode ? "true" : "false");
  }

  const enableBtn = document.getElementById("ollama-enable");
  if (enableBtn) enableBtn.textContent = ollamaRemoteMode ? "Use remote Ollama" : "Use Ollama";
  const startBtn = document.getElementById("ollama-start");
  const restartBtn = document.getElementById("ollama-restart");
  if (startBtn) startBtn.classList.toggle("hidden", ollamaRemoteMode);
  if (restartBtn) restartBtn.classList.toggle("hidden", ollamaRemoteMode);
}

function formatOllamaCompute(ollama) {
  const label = ollama?.compute_label;
  if (!label || label === "idle") return "";
  return ` · ${label}`;
}

export function renderOllamaStatus(ollama) {
  const statusEl = document.getElementById("ollama-status");
  const hintEl = document.getElementById("ollama-hint");
  const section =
    document.getElementById("auth-section") || document.getElementById("settings-ai");
  const pullBtn = document.getElementById("ollama-pull");
  const startBtn = document.getElementById("ollama-start");
  if (!ollama) {
    if (statusEl) statusEl.textContent = "Ollama status unavailable";
    if (startBtn) startBtn.disabled = true;
    return;
  }

  const offline = !ollama.reachable || ollama.listening === false;
  if (startBtn) {
    startBtn.disabled = !(offline && ollama.can_start);
    startBtn.classList.toggle("hidden", !offline);
  }

  if (offline) {
    if (statusEl) {
      statusEl.textContent =
        ollama.error || "Ollama is not running on this machine";
      statusEl.dataset.tone = "warn";
    }
    if (hintEl) {
      hintEl.textContent =
        ollama.install_hint ||
        (ollama.can_start
          ? "Click Start Ollama to launch the local daemon."
          : "Install Ollama, start it (`ollama serve`), then click Use Ollama.");
    }
    if (section) section.dataset.ready = "false";
    if (pullBtn) pullBtn.disabled = true;
    setStatus("need-auth", "ollama offline");
    return;
  }

  if (pullBtn) pullBtn.disabled = !(ollama.missing_models || []).length;

  if (ollama.missing_models?.length) {
    if (statusEl) {
      statusEl.textContent = `Ollama is running — missing models: ${ollama.missing_models.join(", ")}`;
      statusEl.dataset.tone = "warn";
    }
    if (hintEl) {
      hintEl.textContent = ollama.pull_command
        ? `Run: ${ollama.pull_command}`
        : "Pull the required models, then refresh.";
    }
    if (section) section.dataset.ready = ollama.active ? "false" : section.dataset.ready;
    setStatus("need-auth", "ollama · models needed");
    return;
  }

  const chatLabel = ollama.resolved_chat_model || ollama.chat_model;
  const embedLabel = ollama.resolved_embedding_model || ollama.embedding_model;
  const computeLabel = formatOllamaCompute(ollama);
  const isLocal = ollama.is_local !== false;
  const urlInput = document.getElementById("ollama-base-url");
  if (urlInput && ollama.base_url && !urlInput.value) {
    urlInput.value = ollama.base_url;
  }
  if (statusEl) {
    statusEl.textContent = ollama.active
      ? `Using ${isLocal ? "local" : "remote"} Ollama · ${chatLabel} + ${embedLabel}${computeLabel}`
      : `Ollama ready · ${chatLabel} + ${embedLabel}${computeLabel}`;
    statusEl.dataset.tone = "ok";
  }
  if (hintEl) {
    const idleCompute =
      ollama.compute === "idle"
        ? " Processor shows CPU/GPU once a model is loaded."
        : "";
    if (!isLocal) {
      hintEl.textContent = ollama.active
        ? `Documents are processed on ${ollama.base_url || "the remote Ollama host"} — not kept on-device.${idleCompute}`
        : `Remote Ollama requires the privacy disclaimer. Documents leave this machine.${idleCompute}`;
    } else {
      hintEl.textContent = ollama.active
        ? `Documents stay on this machine — no cloud sign-in needed.${idleCompute}`
        : `Click Use Ollama to switch the app to local models.${idleCompute}`;
    }
  }
  if (ollama.active) {
    const authLine = document.getElementById("auth-status");
    if (authLine) {
      authLine.textContent = `${isLocal ? "Local" : "Remote"} Ollama · ${chatLabel}${computeLabel}`;
      authLine.dataset.tone = isLocal ? "ok" : "warn";
    }
    if (section) section.dataset.ready = "true";
    setStatus("ready", `ollama · ${ollama.chat_model || "local"}${computeLabel}`);
  }
}

export function renderAuthStatus(auth) {
  if (lastProvider === "ollama") {
    // Cloud auth panel is inactive while Ollama is selected.
    return;
  }
  const section =
    document.getElementById("auth-section") || document.getElementById("settings-ai");
  const el = document.getElementById("auth-status");
  if (!auth) {
    if (el) el.textContent = "Auth status unavailable";
    if (section) section.dataset.ready = "false";
    setStatus("offline", "offline");
    return;
  }
  if (auth.auth_mode === "chatgpt_oauth") {
    const who = [auth.chatgpt_email, auth.chatgpt_plan].filter(Boolean).join(" · ");
    if (el) {
      el.textContent = who ? `Signed in as ${who}` : "Signed in with ChatGPT";
      el.dataset.tone = "ok";
    }
    if (section) section.dataset.ready = "true";
    setStatus("ready", "chatgpt oauth");
    return;
  }
  if (auth.auth_mode === "api_key") {
    if (el) {
      el.textContent = "Using OpenAI API key";
      el.dataset.tone = "ok";
    }
    if (section) section.dataset.ready = "true";
    setStatus("ready", "api key");
    return;
  }
  if (el) {
    el.textContent = "Not signed in — connect ChatGPT or an API key, or switch to Local Ollama";
    delete el.dataset.tone;
  }
  if (section) section.dataset.ready = "false";
  setStatus("need-auth", "auth needed");
}

export async function refreshAuth() {
  if (lastProvider === "ollama") {
    return null;
  }
  const data = await api("/api/auth/status");
  if (data.cloud_disclaimer) {
    applyCloudDisclaimerStatus(data.cloud_disclaimer);
  }
  renderAuthStatus(data);
  setText("auth-out", {
    auth_mode: data.auth_mode,
    openai_ready: data.openai_ready,
    chatgpt_email: data.chatgpt_email,
    chatgpt_plan: data.chatgpt_plan,
    codex_home: data.codex_home,
  });
  return data;
}

export async function refreshOllamaStatus() {
  const data = await api("/api/ollama/status");
  renderOllamaStatus(data.ollama);
  return data.ollama;
}

export async function refreshHealth() {
  try {
    const data = await api("/api/diagnostics");
    setProviderUi(data.llm_provider || "", {
      remoteOllama: data.llm_provider === "ollama" && data.ollama?.is_local === false,
    });
    if (data.cloud_disclaimer) {
      applyCloudDisclaimerStatus(data.cloud_disclaimer);
    }
    renderUsageMetrics(data.usage);
    if (data.llm_provider === "ollama") {
      if (data.ollama) {
        renderOllamaStatus(data.ollama);
      } else {
        await refreshOllamaStatus();
      }
      return data;
    }
    if (data.auth) {
      renderAuthStatus(data.auth);
    } else {
      setStatus("ready", data.status === "ok" ? "online" : "degraded");
    }
    return data;
  } catch {
    setStatus("offline", "offline");
    return null;
  }
}

const HEALTH_POLL_MS = 15000;
let healthPollTimer = null;

export function startHealthPolling() {
  if (healthPollTimer || window.DC_MOCK?.enabled) return;
  healthPollTimer = window.setInterval(() => {
    refreshHealth().catch(() => {});
  }, HEALTH_POLL_MS);
}
