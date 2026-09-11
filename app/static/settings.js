import {
  api,
  applyCloudDisclaimerStatus,
  armedConfirm,
  cloudDisclaimerAccepted,
  escapeHtml,
  lastProvider,
  ollamaRemoteMode,
  refreshAuth,
  refreshCloudDisclaimer,
  refreshHealth,
  refreshOllamaStatus,
  renderOllamaStatus,
  setProviderUi,
  setText,
  toast,
} from "./api.js";
import { areAskExamplesEnabled, setAskExamplesEnabled } from "./ask.js";
import { hooks, setKnownCategories, workflowState } from "./state.js";
import { renderWorkflow } from "./events.js";

let oauthState = null;
let oauthPollTimer = null;

function stopOauthPoll() {
  if (oauthPollTimer) {
    clearInterval(oauthPollTimer);
    oauthPollTimer = null;
  }
}

function showAuthDetails(show) {
  const details = document.getElementById("auth-details");
  const toggle = document.getElementById("auth-toggle");
  if (!details || !toggle) return;
  details.classList.toggle("hidden", !show);
  toggle.textContent = show ? "Hide options" : "More options";
}

async function selectCloudProvider() {
  setProviderUi("openai");
  await refreshCloudDisclaimer();
  if (!cloudDisclaimerAccepted) {
    document.getElementById("cloud-disclaimer-accept")?.focus();
    toast("Approve the cloud processing disclaimer to use ChatGPT or an API key", "warn");
    return;
  }
  try {
    await api("/api/llm/provider", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: "openai" }),
    });
    toast("Switched to ChatGPT / OpenAI", "ok");
    await refreshHealth();
    await refreshAuth();
  } catch (err) {
    toast(String(err.message || err), "error");
  }
}

async function selectOllamaProvider({ enable = true, pullMissing = false, remote = false } = {}) {
  setProviderUi("ollama", { remoteOllama: remote });
  if (remote) {
    await refreshCloudDisclaimer();
    if (!cloudDisclaimerAccepted) {
      document.getElementById("cloud-disclaimer-accept")?.focus();
      toast("Approve the privacy disclaimer before using Remote Ollama", "warn");
      return;
    }
  }
  try {
    if (enable) {
      const payload = { pull_missing: pullMissing };
      const chatModel = document.getElementById("ollama-chat-model")?.value?.trim();
      const embedModel = document.getElementById("ollama-embed-model")?.value?.trim();
      if (chatModel) payload.chat_model = chatModel;
      if (embedModel) payload.embedding_model = embedModel;
      if (remote) {
        const url = document.getElementById("ollama-base-url")?.value?.trim();
        if (!url) {
          toast("Enter a remote Ollama base URL", "warn");
          document.getElementById("ollama-base-url")?.focus();
          return;
        }
        payload.base_url = url;
        payload.allow_remote = true;
      } else {
        payload.base_url = "http://localhost:11434";
        payload.allow_remote = false;
      }
      const data = await api("/api/ollama/enable", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      renderOllamaStatus(data.ollama);
      if (data.ollama?.ready) {
        toast(remote ? "Using remote Ollama" : "Using local Ollama", "ok");
      } else if (data.ollama?.reachable && data.ollama?.missing_models?.length) {
        toast("Ollama enabled — pull the required models next", "warn");
      } else if (!data.ollama?.reachable) {
        toast(data.ollama?.error || "Ollama is not reachable", "warn");
      } else {
        toast("Ollama enabled", "ok");
      }
    } else {
      await refreshOllamaStatus();
    }
    await refreshHealth();
  } catch (err) {
    toast(String(err.message || err), "error");
  }
}

function setSetupStatus(message, tone = "") {
  const el = document.getElementById("setup-status");
  if (!el) return;
  el.textContent = message || "";
  if (tone) el.dataset.tone = tone;
  else delete el.dataset.tone;
}

function setDangerStatus(message, tone = "") {
  const el = document.getElementById("danger-status");
  if (!el) return;
  el.textContent = message || "";
  if (tone) el.dataset.tone = tone;
  else delete el.dataset.tone;
}

function categoryRowHtml(cat = { name: "", folder: "" }) {
  return `<div class="cat-row">
    <label class="field">
      <span>Name</span>
      <input type="text" class="cat-name" value="${escapeHtml(cat.name || "")}" placeholder="invoice" />
    </label>
    <label class="field grow">
      <span>Folder</span>
      <input type="text" class="cat-folder" value="${escapeHtml(cat.folder || "")}" placeholder="/path/to/archive/invoice" />
    </label>
    <button type="button" class="btn ghost compact cat-remove">Remove</button>
  </div>`;
}

function renderCategories(categories) {
  const root = document.getElementById("setup-categories");
  if (!root) return;
  const list = categories && categories.length ? categories : [{ name: "other", folder: "" }];
  root.innerHTML = list.map((c) => categoryRowHtml(c)).join("");
}

function collectSetupPayload() {
  const source_dir = document.getElementById("setup-source").value.trim();
  const rows = [...document.querySelectorAll("#setup-categories .cat-row")];
  const categories = rows
    .map((row) => ({
      name: row.querySelector(".cat-name")?.value.trim() || "",
      folder: row.querySelector(".cat-folder")?.value.trim() || "",
    }))
    .filter((c) => c.name || c.folder);
  const poll_interval_seconds = Number(
    document.getElementById("setup-poll-interval").value || 0,
  );
  const require_approval = document.getElementById("setup-require-approval").checked;
  const ocr_mode = document.getElementById("setup-ocr-mode")?.value || "balanced";
  return {
    source_dir,
    categories,
    batch: { poll_interval_seconds },
    review: { require_approval },
    ocr: { mode: ocr_mode },
  };
}

function applySettingsToForm(settings) {
  document.getElementById("setup-source").value = settings.source_dir || "";
  renderCategories(settings.categories || []);
  setKnownCategories((settings.categories || []).map((c) => c.name).filter(Boolean));
  const batch = settings.batch || {};
  const interval = batch.poll_interval_seconds ?? batch.delay_seconds ?? 30;
  document.getElementById("setup-poll-interval").value = interval;
  document.getElementById("setup-require-approval").checked =
    (settings.review || {}).require_approval ?? true;
  const ocrMode = (settings.ocr || {}).mode || "balanced";
  const ocrSelect = document.getElementById("setup-ocr-mode");
  if (ocrSelect) ocrSelect.value = ocrMode;
  setSetupStatus(setupStatusLine(settings), "ok");
}

export function setupStatusLine(settings) {
  const batch = settings.batch || {};
  const interval = batch.poll_interval_seconds ?? batch.delay_seconds ?? 30;
  const catCount = (settings.categories || []).length;
  const scan =
    Number(interval) > 0 ? `scans every ${interval}s` : "manual scan only";
  const ocrMode = (settings.ocr || {}).mode || "balanced";
  const reviewMode =
    (settings.review || {}).require_approval === false
      ? "auto-file"
      : "review required";
  return `Source: ${settings.source_dir || "—"} · ${catCount} categor${catCount === 1 ? "y" : "ies"} · ${scan} · OCR ${ocrMode} · ${reviewMode}`;
}

export async function refreshSetup() {
  const data = await api("/api/settings");
  applySettingsToForm(data.settings || {});
  return data.settings;
}

const THEME_PRESETS = ["graphite", "carbon", "slate", "paper"];

function applyTheme(name) {
  const theme = THEME_PRESETS.includes(name) ? name : "graphite";
  if (theme === "graphite") {
    delete document.documentElement.dataset.theme;
  } else {
    document.documentElement.dataset.theme = theme;
  }
  try {
    localStorage.setItem("dc-theme", theme);
  } catch (_err) {
    // private mode — theme just won't persist
  }
  for (const btn of document.querySelectorAll(".theme-card")) {
    btn.classList.toggle("active", btn.dataset.themePreset === theme);
  }
}

export function initTheme() {
  const param = new URLSearchParams(window.location.search).get("theme");
  let stored = null;
  try {
    stored = localStorage.getItem("dc-theme");
  } catch (_err) {
    // ignore
  }
  applyTheme(param || stored || "graphite");
}

export function applyMockScene() {
  // Frozen mid-run workflow so the inbox screenshot shows the pipeline alive.
  const scene = window.DC_MOCK.workflow;
  workflowState.activeFilename = scene.activeFilename;
  workflowState.activeFileId = scene.activeFileId || "demo-f2";
  workflowState.jobTotal = scene.jobTotal;
  workflowState.stepStatus = { ...scene.stepStatus };
  workflowState.stepDetail = { ...scene.stepDetail };
  workflowState.stepStartedAt = {};
  if (scene.stepStatus?.extract === "running") {
    workflowState.stepStartedAt.extract = performance.now() - 47_000;
  }
  workflowState.queue = scene.queue.map((q) => ({ ...q }));
  renderWorkflow();

  // Prefill the Ask view with a canned question and answer.
  const question = document.getElementById("question");
  if (question) {
    question.value = "Which invoices did I get from Acme this quarter?";
    question.dispatchEvent(new Event("input"));
  }
  hooks.renderAskResult(window.DC_MOCK.ask);
}

function setUpdateStatus(message, tone = "") {
  const el = document.getElementById("update-status");
  if (!el) return;
  el.textContent = message || "";
  if (tone) el.dataset.tone = tone;
  else delete el.dataset.tone;
}

function showUpdateButtons({ apply = false, restart = false } = {}) {
  document.getElementById("update-apply").classList.toggle("hidden", !apply);
  document.getElementById("update-restart").classList.toggle("hidden", !restart);
}

export function updateApplyAllowed(data) {
  return Boolean(
    data?.update_available && data?.signed && data?.verifiable && data?.installable !== false,
  );
}

export async function refreshUpdateVersion() {
  const data = await api("/api/update/status");
  setUpdateStatus(`DeepCatalog v${data.current_version}`);
  const link = document.getElementById("update-repo-link");
  if (link && data.repo) link.href = `https://github.com/${data.repo}`;
}

async function waitForServerBack(timeoutMs = 45000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    await new Promise((r) => setTimeout(r, 1200));
    try {
      const res = await fetch("/api/health", { cache: "no-store" });
      if (res.ok) return true;
    } catch (_err) {
      // still restarting
    }
  }
  return false;
}

function renderAutostartStatus(autostart) {
  const section = document.getElementById("autostart-section");
  const statusEl = document.getElementById("autostart-status");
  const hintEl = document.getElementById("autostart-hint");
  const toggle = document.getElementById("autostart-toggle");
  if (!section || !statusEl || !toggle) return;

  if (!autostart?.supported) {
    section.classList.add("hidden");
    return;
  }
  section.classList.remove("hidden");

  toggle.disabled = Boolean(autostart.error);
  toggle.checked = Boolean(autostart.enabled);

  if (autostart.error) {
    statusEl.textContent = autostart.error;
    statusEl.dataset.tone = "warn";
    if (hintEl) {
      hintEl.textContent =
        autostart.install_hint ||
        "Fix the install path or virtualenv, then try again.";
    }
    return;
  }

  delete statusEl.dataset.tone;
  const runState = autostart.active ? "running" : "stopped";
  const bootState = autostart.enabled ? "enabled at boot" : "not enabled at boot";
  statusEl.textContent = `Service ${runState} · ${bootState} · ${autostart.url || ""}`;

  if (hintEl) {
    const lingerBit = autostart.linger
      ? "User lingering is enabled so the service can start before login."
      : "Enabling autostart also turns on user lingering for boot-time startup.";
    hintEl.textContent = `${lingerBit} Unit file: ${autostart.unit_path || "—"}`;
  }
}

export async function refreshAutostart() {
  const data = await api("/api/autostart/status");
  renderAutostartStatus(data.autostart);
  return data.autostart;
}

export function settingsShellHtml() {
  return `<section class="card auth" id="auth-section" data-ready="false" data-provider="">
          <div class="auth-bar">
            <div>
              <p class="section-kicker">AI</p>
              <h2>AI provider</h2>
              <p id="auth-status" class="auth-line">Checking provider…</p>
            </div>
            <div class="actions provider-switch" role="group" aria-label="AI provider">
              <button id="provider-cloud" type="button" class="btn secondary" data-active="false">Cloud (ChatGPT)</button>
              <button id="provider-ollama" type="button" class="btn secondary" data-active="false">Local Ollama</button>
              <button id="provider-ollama-remote" type="button" class="btn secondary" data-active="false">Remote Ollama</button>
            </div>
          </div>

          <div id="ollama-panel" class="auth-details ollama-panel hidden">
            <p id="ollama-status" class="auth-line">Checking local Ollama…</p>
            <p id="ollama-hint" class="fine"></p>
            <div id="ollama-remote-fields" class="ollama-remote-fields hidden">
              <label class="field">
                <span>Ollama base URL</span>
                <input id="ollama-base-url" type="url" placeholder="http://192.168.1.10:11434" autocomplete="off" />
              </label>
              <p class="fine">
                Remote Ollama sends page images and text to that host — same privacy rules as cloud AI.
                Prefer a literal IP. Hostnames must also be listed in
                <code>DEEPCATALOG_OLLAMA_ALLOWED_HOSTS</code> so DNS cannot retarget the connection.
                Approve the disclaimer below before enabling.
              </p>
            </div>
            <div class="ollama-model-fields">
              <label class="field">
                <span>Chat model (vision)</span>
                <select id="ollama-chat-model"></select>
              </label>
              <label class="field">
                <span>Embedding model</span>
                <select id="ollama-embed-model"></select>
              </label>
            </div>
            <div class="actions ollama-actions">
              <button id="ollama-start" type="button" class="btn secondary" disabled>Start Ollama</button>
              <button id="ollama-enable" type="button" class="btn primary">Use Ollama</button>
              <button id="ollama-pull" type="button" class="btn secondary">Pull required models</button>
              <button id="ollama-unload" type="button" class="btn ghost">Unload model</button>
              <button id="ollama-restart" type="button" class="btn ghost">Restart Ollama</button>
              <button id="ollama-refresh" type="button" class="btn ghost">Refresh</button>
            </div>
            <p class="fine">
              Chat must be multimodal (page images for OCR). Changing the embedding model
              marks the search index stale so it rebuilds on next use.
              <a class="text-link" href="https://ollama.com/download" target="_blank" rel="noopener">Download Ollama</a>
            </p>
          </div>

          <div id="cloud-auth-panel" class="cloud-auth-panel" data-locked="true">
            <div id="cloud-disclaimer" class="cloud-disclaimer" data-accepted="false">
              <div class="disclaimer-copy">
                <p class="section-kicker">Before you continue</p>
                <p class="disclaimer-lead">Cloud AI sends your documents off this machine.</p>
                <p class="disclaimer-body">
                  Page images and extracted text go to ChatGPT, OpenAI, or Gemini for OCR,
                  filing, and Ask. Storage can stay local — processing does not.
                  Use <span class="disclaimer-em">Local Ollama</span> to keep AI on-device.
                  <span class="disclaimer-em">Remote Ollama</span> also leaves this machine.
                </p>
              </div>
              <label class="disclaimer-check">
                <input type="checkbox" id="cloud-disclaimer-accept" />
                <span class="disclaimer-check-box" aria-hidden="true"></span>
                <span class="disclaimer-check-label">I approve cloud processing for my documents</span>
              </label>
            </div>

            <div class="auth-bar cloud-auth-bar">
              <div class="actions">
                <button id="oauth-start" type="button" class="btn primary" disabled>Sign in with ChatGPT</button>
                <button id="auth-toggle" type="button" class="btn ghost" disabled>More options</button>
                <button id="auth-logout" type="button" class="btn ghost">Log out</button>
              </div>
            </div>

            <div id="auth-details" class="auth-details hidden">
              <div id="oauth-panel" class="oauth hidden">
                <p id="oauth-hint" class="fine"></p>
                <a id="oauth-link" class="text-link" href="#" target="_blank" rel="noopener">Open ChatGPT login</a>
                <label class="field">
                  <span>Paste callback URL if redirect fails</span>
                  <textarea id="oauth-paste" rows="2" placeholder="http://localhost:1455/auth/callback?code=…"></textarea>
                </label>
                <button id="oauth-complete" type="button" class="btn primary">Complete sign-in</button>
              </div>

              <form id="api-key-form" class="key-row">
                <label class="field grow">
                  <span>OpenAI API key</span>
                  <input type="password" id="api-key" placeholder="sk-…" autocomplete="off" disabled />
                </label>
                <button type="submit" class="btn secondary" disabled>Save key</button>
                <button id="auth-refresh" type="button" class="btn ghost">Refresh</button>
              </form>
              <details class="debug">
                <summary>Auth details</summary>
                <pre id="auth-out" class="out"></pre>
              </details>
            </div>
          </div>
        </section>

        <section class="card auth setup" id="setup-section">
          <div class="auth-bar">
            <div>
              <p class="section-kicker">Filing</p>
              <h2>Filing &amp; scanning</h2>
              <p id="setup-status" class="auth-line">Configure source folder, categories, and inbox scanning.</p>
            </div>
            <div class="actions">
              <button id="setup-save" type="button" class="btn primary">Save setup</button>
            </div>
          </div>

          <div id="setup-details" class="auth-details">
            <label class="field">
              <span>Source folder (inbox)</span>
              <input type="text" id="setup-source" placeholder="/path/to/inbox" autocomplete="off" />
            </label>

            <div class="setup-cats">
              <div class="setup-cats-head">
                <h3>Document categories</h3>
                <button id="setup-add-category" type="button" class="btn ghost compact">Add category</button>
              </div>
              <p class="fine">Map each category name to an archive folder. An <code>other</code> category is required.</p>
              <div id="setup-categories" class="cat-list"></div>
            </div>

            <div class="setup-batch">
              <h3>Inbox scanning</h3>
              <p class="fine">How often the agent should look for new files in the source folder and process them automatically.</p>
              <div class="batch-row">
                <label class="field narrow">
                  <span>Poll interval (seconds)</span>
                  <input type="number" id="setup-poll-interval" min="0" step="1" value="30" />
                </label>
                <p class="fine batch-hint">Use <code>0</code> for manual processing only (Process inbox button).</p>
              </div>
            </div>

            <div class="setup-batch">
              <h3>OCR accuracy</h3>
              <p class="fine">
                How aggressively to reuse a PDF’s embedded text versus calling AI vision OCR.
                Scanned pages without a usable text layer still use vision in every mode.
              </p>
              <label class="field">
                <span>Mode</span>
                <select id="setup-ocr-mode">
                  <option value="fast">Fast — use embedded text when present; vision only if nearly empty</option>
                  <option value="balanced" selected>Balanced — use good embedded text; vision for weak/garbled pages</option>
                  <option value="maximum">Maximum — always vision OCR every page</option>
                </select>
              </label>
            </div>

            <div class="setup-batch">
              <h3>Human review</h3>
              <label class="check-field">
                <input type="checkbox" id="setup-require-approval" checked />
                <span>Require approval before filing — every proposal waits in Review until you approve it</span>
              </label>
              <p class="fine">When off, documents are filed automatically. Suspected duplicates always stop for review.</p>
            </div>
          </div>
        </section>

        <section class="card auth hidden" id="autostart-section">
          <div class="auth-bar">
            <div>
              <p class="section-kicker">System</p>
              <h2>Autostart</h2>
              <p id="autostart-status" class="auth-line">Checking autostart…</p>
            </div>
          </div>
          <label class="check-field">
            <input type="checkbox" id="autostart-toggle" />
            <span>Start DeepCatalog Studio when the system boots</span>
          </label>
          <p id="autostart-hint" class="fine">
            Installs a systemd user service, enables it across reboots, and keeps the web UI available at boot.
          </p>
        </section>

        <section class="card auth" id="appearance-section">
          <div class="auth-bar">
            <div>
              <p class="section-kicker">Appearance</p>
              <h2>Look &amp; feel</h2>
              <p class="auth-line">Pick a preset. Applies instantly and is remembered on this computer.</p>
            </div>
          </div>
          <div class="theme-grid" id="theme-grid">
            <button type="button" class="theme-card" data-theme-preset="graphite">
              <span class="theme-preview" data-preview="graphite" aria-hidden="true">
                <span class="tp-side"></span>
                <span class="tp-body">
                  <span class="tp-accent"></span>
                  <span class="tp-line"></span>
                  <span class="tp-line short"></span>
                </span>
              </span>
              <span class="theme-name">Graphite</span>
              <span class="theme-desc">Dark · steel cyan</span>
            </button>
            <button type="button" class="theme-card" data-theme-preset="carbon">
              <span class="theme-preview" data-preview="carbon" aria-hidden="true">
                <span class="tp-side"></span>
                <span class="tp-body">
                  <span class="tp-accent"></span>
                  <span class="tp-line"></span>
                  <span class="tp-line short"></span>
                </span>
              </span>
              <span class="theme-name">Carbon</span>
              <span class="theme-desc">Black · amber</span>
            </button>
            <button type="button" class="theme-card" data-theme-preset="slate">
              <span class="theme-preview" data-preview="slate" aria-hidden="true">
                <span class="tp-side"></span>
                <span class="tp-body">
                  <span class="tp-accent"></span>
                  <span class="tp-line"></span>
                  <span class="tp-line short"></span>
                </span>
              </span>
              <span class="theme-name">Slate</span>
              <span class="theme-desc">Light · teal</span>
            </button>
            <button type="button" class="theme-card" data-theme-preset="paper">
              <span class="theme-preview" data-preview="paper" aria-hidden="true">
                <span class="tp-side"></span>
                <span class="tp-body">
                  <span class="tp-accent"></span>
                  <span class="tp-line"></span>
                  <span class="tp-line short"></span>
                </span>
              </span>
              <span class="theme-name">Paper</span>
              <span class="theme-desc">Warm · terracotta</span>
            </button>
          </div>

          <div class="mock-block">
            <label class="check-field">
              <input type="checkbox" id="ask-examples-toggle" checked />
              <span>Show example questions on the Ask empty state</span>
            </label>
            <p class="fine">Suggested prompts under Ask when the thread is empty. Remembered on this computer.</p>
          </div>

          <div class="mock-block">
            <label class="check-field">
              <input type="checkbox" id="mock-toggle" />
              <span>Mockup mode — fill every view with demo data for screenshots</span>
            </label>
            <p class="fine">Client-side only: nothing is read from or written to your archive while enabled. Also available via <code>?mock=1</code> in the URL.</p>
          </div>
        </section>

        <section class="card auth" id="update-section">
          <div class="auth-bar">
            <div>
              <p class="section-kicker">Updates</p>
              <h2>Software update</h2>
              <p id="update-status" class="auth-line">Version —</p>
            </div>
            <div class="actions">
              <button id="update-check" type="button" class="btn secondary">
                <svg class="icon" aria-hidden="true"><use href="#i-refresh" /></svg>
                Check for updates
              </button>
              <button id="update-apply" type="button" class="btn primary hidden">Download &amp; install</button>
              <button id="update-restart" type="button" class="btn primary hidden">Restart now</button>
            </div>
          </div>
          <p class="fine">Updates are downloaded from <a id="update-repo-link" class="text-link" href="#" target="_blank" rel="noopener">GitHub</a> and installed only when a signed release manifest matches this build's verify key. Your documents, settings, and credentials are kept.</p>
          <pre id="update-notes" class="out hidden"></pre>
        </section>

        <section class="card auth danger-zone">
          <div class="auth-bar">
            <div>
              <p class="section-kicker">Danger</p>
              <h2>Danger zone</h2>
              <p class="auth-line">Delete tracked archive files (inside configured archive folders only), supported inbox scans, metadata, and the RAG index. Category folders are never wiped recursively. Setup settings are kept.</p>
            </div>
            <div class="actions">
              <button id="clear-all-data" type="button" class="btn ghost danger">
                Remove all stored data
              </button>
            </div>
          </div>
          <p id="danger-status" class="status-line" aria-live="polite"></p>
        </section>`;
}

export function mountSettingsShell() {
  const host = document.getElementById("settings");
  if (!host || host.querySelector("#auth-section")) return host;
  host.innerHTML = settingsShellHtml();
  return host;
}

export function initSettings() {
  mountSettingsShell();
  document.getElementById("cloud-disclaimer-accept")?.addEventListener("change", async (event) => {
    const checked = Boolean(event.target?.checked);
    try {
      const data = await api("/api/privacy/cloud-disclaimer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ accepted: checked }),
      });
      applyCloudDisclaimerStatus(data.cloud_disclaimer);
      if (checked) {
        toast("Cloud processing approved — you can sign in or save an API key", "ok");
        // Persist cloud provider only after explicit approval.
        if (lastProvider !== "ollama") {
          await api("/api/llm/provider", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ provider: "openai" }),
          }).catch(() => {});
          await refreshHealth();
          await refreshAuth();
        }
      } else {
        toast("Cloud sign-in and API keys are locked until you approve again", "warn");
        showAuthDetails(false);
      }
    } catch (err) {
      event.target.checked = !checked;
      toast(String(err.message || err), "error");
    }
  });

  document.getElementById("provider-cloud")?.addEventListener("click", () => {
    selectCloudProvider();
  });

  document.getElementById("provider-ollama")?.addEventListener("click", () => {
    selectOllamaProvider({ enable: true, pullMissing: false, remote: false });
  });

  document.getElementById("provider-ollama-remote")?.addEventListener("click", () => {
    selectOllamaProvider({ enable: false, remote: true });
  });

  document.getElementById("ollama-enable")?.addEventListener("click", () => {
    selectOllamaProvider({ enable: true, pullMissing: false, remote: ollamaRemoteMode });
  });

  document.getElementById("ollama-start")?.addEventListener("click", async () => {
    const startBtn = document.getElementById("ollama-start");
    const statusEl = document.getElementById("ollama-status");
    try {
      if (startBtn) startBtn.disabled = true;
      if (statusEl) {
        statusEl.textContent = "Starting Ollama…";
        statusEl.dataset.tone = "warn";
      }
      const data = await api("/api/ollama/start", { method: "POST" });
      renderOllamaStatus(data.ollama);
      if (data.already_running) {
        toast("Ollama was already running", "ok");
      } else if (data.ollama?.reachable) {
        toast("Ollama started", "ok");
        if (data.ollama?.missing_models?.length) {
          toast("Pull the required models next", "warn");
        }
      } else {
        toast("Start finished, but Ollama is still unreachable", "warn");
      }
      await refreshHealth();
    } catch (err) {
      toast(String(err.message || err), "error");
      refreshOllamaStatus().catch(() => {});
    }
  });

  document.getElementById("ollama-pull")?.addEventListener("click", async () => {
    const pullBtn = document.getElementById("ollama-pull");
    const statusEl = document.getElementById("ollama-status");
    try {
      if (pullBtn) pullBtn.disabled = true;
      if (statusEl) {
        statusEl.textContent = "Pulling models via Ollama — this can take a few minutes…";
        statusEl.dataset.tone = "warn";
      }
      // Ensure provider is ollama first, then pull whatever is still missing.
      const payload = { pull_missing: true };
      const chatModel = document.getElementById("ollama-chat-model")?.value?.trim();
      const embedModel = document.getElementById("ollama-embed-model")?.value?.trim();
      if (chatModel) payload.chat_model = chatModel;
      if (embedModel) payload.embedding_model = embedModel;
      if (ollamaRemoteMode) {
        const url = document.getElementById("ollama-base-url")?.value?.trim();
        if (!url) {
          toast("Enter a remote Ollama base URL", "warn");
          return;
        }
        payload.base_url = url;
        payload.allow_remote = true;
      } else {
        payload.base_url = "http://localhost:11434";
      }
      const enabled = await api("/api/ollama/enable", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      renderOllamaStatus(enabled.ollama);
      if (enabled.ollama?.ready) {
        toast("Required Ollama models are ready", "ok");
      } else if (enabled.ollama?.missing_models?.length) {
        toast(`Still missing: ${enabled.ollama.missing_models.join(", ")}`, "warn");
      } else {
        toast("Model pull finished", "ok");
      }
      await refreshHealth();
    } catch (err) {
      toast(String(err.message || err), "error");
      refreshOllamaStatus().catch(() => {});
    } finally {
      if (pullBtn) pullBtn.disabled = false;
    }
  });

  document.getElementById("ollama-refresh")?.addEventListener("click", () => {
    refreshOllamaStatus().catch((err) => toast(String(err.message || err), "error"));
  });

  document.getElementById("ollama-unload")?.addEventListener("click", async () => {
    const btn = document.getElementById("ollama-unload");
    try {
      if (btn) btn.disabled = true;
      const data = await api("/api/ollama/unload", { method: "POST" });
      toast(
        data.unloaded?.length
          ? `Unloaded: ${data.unloaded.join(", ")}`
          : "Unload requested",
        "ok",
      );
      await refreshOllamaStatus();
    } catch (err) {
      toast(String(err.message || err), "error");
    } finally {
      if (btn) btn.disabled = false;
    }
  });

  document.getElementById("ollama-restart")?.addEventListener("click", async () => {
    const btn = document.getElementById("ollama-restart");
    if (!window.confirm("Restart Ollama? Active processing will be blocked unless you force-cancel first.")) {
      return;
    }
    try {
      if (btn) btn.disabled = true;
      const data = await api("/api/ollama/restart", {
        method: "POST",
        body: JSON.stringify({ force: false }),
      });
      toast(data.method ? `Ollama restarted (${data.method})` : "Ollama restarted", "ok");
      await refreshOllamaStatus();
    } catch (err) {
      const msg = String(err.message || err);
      if (msg.includes("being processed") && window.confirm("Cancel the active file and restart Ollama?")) {
        try {
          const forced = await api("/api/ollama/restart", {
            method: "POST",
            body: JSON.stringify({ force: true }),
          });
          toast(forced.method ? `Ollama restarted (${forced.method})` : "Ollama restarted", "ok");
          await refreshOllamaStatus();
        } catch (forceErr) {
          toast(String(forceErr.message || forceErr), "error");
        }
      } else {
        toast(msg, "error");
      }
    } finally {
      if (btn) btn.disabled = false;
    }
  });

  document.getElementById("auth-toggle").addEventListener("click", () => {
    const details = document.getElementById("auth-details");
    showAuthDetails(details.classList.contains("hidden"));
  });

  document.getElementById("oauth-start").addEventListener("click", async () => {
    if (!cloudDisclaimerAccepted) {
      toast("Approve the cloud processing disclaimer first", "warn");
      return;
    }
    stopOauthPoll();
    showAuthDetails(true);
    setText("auth-out", "Starting ChatGPT OAuth…");
    try {
      const data = await api("/api/auth/openai/start", { method: "POST" });
      oauthState = data.state;
      document.getElementById("oauth-panel").classList.remove("hidden");
      document.getElementById("oauth-hint").textContent = data.hint || "";
      const link = document.getElementById("oauth-link");
      const authorizeUrl = data.authorize_url || "";
      if (!/^https:\/\//i.test(authorizeUrl)) {
        throw new Error("OAuth authorize URL rejected (expected https)");
      }
      link.href = authorizeUrl;
      window.open(authorizeUrl, "_blank", "noopener");
      setText("auth-out", {
        message: "Browser login opened. Waiting for callback…",
        callback_ready: data.callback_ready,
        callback_error: data.callback_error,
        redirect_uri: data.redirect_uri,
      });
      oauthPollTimer = setInterval(async () => {
        if (!oauthState) return;
        try {
          const poll = await api(
            `/api/auth/openai/poll?state=${encodeURIComponent(oauthState)}`,
          );
          if (poll.status === "success") {
            stopOauthPoll();
            document.getElementById("oauth-panel").classList.add("hidden");
            setText("auth-out", poll);
            toast("Signed in with ChatGPT", "ok");
            await refreshAuth();
          } else if (poll.status === "error") {
            stopOauthPoll();
            setText("auth-out", poll);
          }
        } catch (err) {
          stopOauthPoll();
          setText("auth-out", String(err.message || err));
        }
      }, 1500);
    } catch (err) {
      setText("auth-out", String(err.message || err));
      toast(String(err.message || err), "error");
    }
  });

  document.getElementById("oauth-complete").addEventListener("click", async () => {
    if (!oauthState) {
      setText("auth-out", "Start sign-in first.");
      return;
    }
    const callback = document.getElementById("oauth-paste").value.trim();
    if (!callback) {
      setText("auth-out", "Paste the callback URL or code first.");
      return;
    }
    try {
      const data = await api("/api/auth/openai/complete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ state: oauthState, callback }),
      });
      stopOauthPoll();
      document.getElementById("oauth-panel").classList.add("hidden");
      setText("auth-out", data);
      toast("Signed in with ChatGPT", "ok");
      await refreshAuth();
    } catch (err) {
      setText("auth-out", String(err.message || err));
      toast(String(err.message || err), "error");
    }
  });

  document.getElementById("auth-refresh").addEventListener("click", () => {
    refreshAuth().catch((err) => setText("auth-out", String(err.message || err)));
  });

  document.getElementById("auth-logout").addEventListener("click", async () => {
    stopOauthPoll();
    try {
      const data = await api("/api/auth/logout", { method: "POST" });
      setText("auth-out", data);
      toast("Logged out", "ok");
      await refreshAuth();
    } catch (err) {
      setText("auth-out", String(err.message || err));
      toast(String(err.message || err), "error");
    }
  });

  document.getElementById("api-key-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!cloudDisclaimerAccepted) {
      toast("Approve the cloud processing disclaimer first", "warn");
      return;
    }
    const api_key = document.getElementById("api-key").value.trim();
    if (!api_key) return;
    try {
      const data = await api("/api/auth/api-key", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key }),
      });
      document.getElementById("api-key").value = "";
      setText("auth-out", data);
      toast("API key saved", "ok");
      await refreshAuth();
    } catch (err) {
      setText("auth-out", String(err.message || err));
      toast(String(err.message || err), "error");
    }
  });

  document.getElementById("setup-add-category").addEventListener("click", () => {
    const root = document.getElementById("setup-categories");
    root.insertAdjacentHTML("beforeend", categoryRowHtml({ name: "", folder: "" }));
  });

  document.getElementById("setup-categories").addEventListener("click", (e) => {
    const btn = e.target.closest(".cat-remove");
    if (!btn) return;
    const rows = document.querySelectorAll("#setup-categories .cat-row");
    if (rows.length <= 1) {
      setSetupStatus("Keep at least one category (including other).", "warn");
      return;
    }
    btn.closest(".cat-row")?.remove();
  });

  document.getElementById("setup-require-approval")?.addEventListener("change", async (e) => {
    const box = e.target;
    const wanted = Boolean(box?.checked);
    try {
      const payload = collectSetupPayload();
      const data = await api("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      applySettingsToForm(data.settings || payload);
      toast(
        wanted
          ? "New scans wait in Review"
          : "New scans will file automatically",
        "ok",
      );
    } catch (err) {
      if (box) box.checked = !wanted;
      const msg = String(err.message || err);
      setSetupStatus(msg, "err");
      toast(msg, "error");
    }
  });

  document.getElementById("setup-save").addEventListener("click", async () => {
    const btn = document.getElementById("setup-save");
    btn.disabled = true;
    setSetupStatus("Saving setup…");
    try {
      const payload = collectSetupPayload();
      const data = await api("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      applySettingsToForm(data.settings || payload);
      toast("Setup saved", "ok");
    } catch (err) {
      setSetupStatus(String(err.message || err), "err");
      toast(String(err.message || err), "error");
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("theme-grid").addEventListener("click", (e) => {
    const btn = e.target.closest(".theme-card");
    if (!btn) return;
    applyTheme(btn.dataset.themePreset);
  });

  const askExamplesToggle = document.getElementById("ask-examples-toggle");
  if (askExamplesToggle) {
    askExamplesToggle.checked = areAskExamplesEnabled();
    askExamplesToggle.addEventListener("change", (e) => {
      setAskExamplesEnabled(e.target.checked);
    });
  }

  document.getElementById("mock-toggle").addEventListener("change", (e) => {
    window.DC_MOCK?.setEnabled(e.target.checked);
    const url = new URL(window.location.href);
    if (url.searchParams.has("mock")) {
      // Drop the ?mock= override so the stored preference takes effect.
      url.searchParams.delete("mock");
      window.location.href = url.toString();
    } else {
      // Same-URL navigation with a hash would not reload; force it.
      window.location.reload();
    }
  });

  document.getElementById("update-check").addEventListener("click", async () => {
    const btn = document.getElementById("update-check");
    btn.disabled = true;
    setUpdateStatus("Checking GitHub for updates…");
    showUpdateButtons({});
    document.getElementById("update-notes").classList.add("hidden");
    try {
      const data = await api("/api/update/status?check=true");
      if (data.status !== "success") {
        setUpdateStatus(data.error || "Update check failed", "err");
        return;
      }
      if (data.update_available) {
        if (updateApplyAllowed(data)) {
          const kind = data.artifact_kind === "appimage" ? "AppImage" : "release";
          setUpdateStatus(
            `Update available: v${data.current_version} → v${data.latest_version} ` +
              `(signed ${kind} verified)`,
            "warn",
          );
          showUpdateButtons({ apply: true });
        } else if (data.installable === false && data.appimage) {
          const link = data.appimage_url || data.html_url;
          setUpdateStatus(
            data.verification_error ||
              (`Update available: v${data.current_version} → v${data.latest_version}. ` +
                "This AppImage cannot be replaced here" +
                (link ? ` — download from GitHub (${link})` : "") +
                "."),
            "warn",
          );
          showUpdateButtons({});
        } else {
          setUpdateStatus(
            data.verification_error ||
              `v${data.latest_version} is available but is not signed — install refused`,
            "err",
          );
          showUpdateButtons({});
        }
        if (data.notes) {
          const notes = document.getElementById("update-notes");
          notes.textContent = data.notes;
          notes.classList.remove("hidden");
        }
      } else {
        setUpdateStatus(
          data.message || `DeepCatalog v${data.current_version} — up to date`,
          "ok",
        );
      }
    } catch (err) {
      setUpdateStatus(String(err.message || err), "err");
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("update-apply").addEventListener("click", async () => {
    const btn = document.getElementById("update-apply");
    btn.disabled = true;
    setUpdateStatus("Downloading and installing update…");
    try {
      const data = await api("/api/update/apply", { method: "POST" });
      setUpdateStatus(
        `Installed v${data.installed_version} (${data.updated_count} files, signed) — restart to finish`,
        "ok",
      );
      toast(`Updated to v${data.installed_version}`, "ok");
      showUpdateButtons({ restart: true });
    } catch (err) {
      setUpdateStatus(String(err.message || err), "err");
      toast(String(err.message || err), "error");
      btn.disabled = false;
    }
  });

  document.getElementById("update-restart").addEventListener("click", async () => {
    const btn = document.getElementById("update-restart");
    btn.disabled = true;
    setUpdateStatus("Restarting DeepCatalog…");
    try {
      await api("/api/update/restart", { method: "POST" });
    } catch (_err) {
      // The connection may drop mid-request while the process re-execs.
    }
    const back = await waitForServerBack();
    if (back) {
      toast("Restarted — reloading", "ok");
      window.location.reload();
    } else {
      setUpdateStatus("Server did not come back — restart it manually", "err");
      btn.disabled = false;
    }
  });

  document.getElementById("clear-all-data").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    if (!armedConfirm(btn, "Really delete everything?")) return;
    btn.disabled = true;
    try {
      const data = await api("/api/data", {
        method: "DELETE",
        body: { confirmation: "DELETE ALL DEEPCATALOG DATA" },
      });
      setDangerStatus(data.message || "All stored data removed.", "ok");
      toast("All stored data removed", "ok");
      hooks.refreshDocs().catch(() => {});
      hooks.refreshInbox().catch(() => {});
    } catch (err) {
      const msg = String(err.message || err);
      setDangerStatus(msg, "error");
      toast(msg, "error");
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("autostart-toggle")?.addEventListener("change", async (e) => {
    const toggle = e.target;
    const enabled = toggle.checked;
    toggle.disabled = true;
    try {
      const data = await api("/api/autostart", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      renderAutostartStatus(data.autostart);
      toast(
        enabled
          ? "DeepCatalog Studio will start automatically at boot."
          : "Boot autostart disabled.",
        "success",
      );
    } catch (err) {
      toggle.checked = !enabled;
      toast(String(err.message || err), "error");
      await refreshAutostart().catch(() => {});
    } finally {
      toggle.disabled = false;
    }
  });
}

export { setSetupStatus };
