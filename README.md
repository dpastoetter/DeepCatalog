# DeepCatalog

Deep document intelligence, local-first.

Drop scanned PDFs and photos into an inbox, and DeepCatalog recovers the text with AI vision OCR, extracts structured metadata, proposes a meaningful filename, and files everything into your folder structure — with you in the loop before anything is written. Metadata lands in SQLite, content is indexed for RAG so you can ask questions about your archive in natural language.

The desktop and web workspace is **DeepCatalog Studio** — your local workspace for archive discovery and automation.

Built with [Google ADK](https://adk.dev/) (Python) + FastAPI. Works with **OpenAI (ChatGPT OAuth or API key)**, **Gemini**, or **Ollama**.

Your files, database, and search index are **always stored on this machine**. That is not the same as fully local processing: if you use ChatGPT or Gemini, document pages and text are also sent to that provider for OCR, metadata extraction, and Ask. **Only local Ollama is truly fully local.** See [Privacy](#privacy-local-storage-vs-cloud-ai).

![Inbox workbench with a live ingest pipeline](docs/screenshots/inbox.png)

## Privacy: local storage vs cloud AI

DeepCatalog never uploads your archive to a DeepCatalog cloud. Inbox folders, filed PDFs, `data/deepcatalog.db` (SQLite), and `data/chroma/` (vectors) stay on disk (or under `~/.local/share/deepcatalog` for the AppImage).

| Mode | Where files live | Where document content goes for AI |
| --- | --- | --- |
| **Local Ollama** | This machine | This machine only. OCR, extraction, embeddings, and Ask run on local models. This is the only fully local setup. |
| **ChatGPT / Gemini** | This machine | **Also to OpenAI or Google.** Page images and text leave the machine so the cloud model can transcribe, extract metadata, propose names, and answer Ask. You must accept the cloud processing disclaimer in **Settings → AI provider** first. |
| **Remote Ollama** | This machine | **Also to the Ollama host you configured.** Same document payloads as local Ollama, but on another computer. Requires the remote-Ollama privacy disclaimer. |

Switching to a cloud provider does not move your filed archive off-disk. It only sends the pages/text needed for that processing step, then writes results back locally.

## Features

- **Ingest pipeline**: Open file → Transcribe → Find details → Name file → Save → Review → Make searchable, with a live workflow strip (Server-Sent Events) and hover descriptions on each step
- **Adaptive OCR**: per-page text-layer vs AI vision (`fast` / `balanced` / `maximum`); cloud providers run vision pages concurrently
- **Per-file cancel & retry**: stop a stuck or slow file mid-pipeline; retry failed or cancelled items from the inbox queue (readable error toasts for API failures)
- **Human-in-the-loop review**: proposed filings wait in a review queue where you can correct filename, category, date, parties, reference IDs, amount (financial docs only), and summary before approving — nothing is written until you say so. Turn this off under **Settings → Filing** (the checkbox saves immediately); suspected duplicates still always stop for review
- **Smart metadata extraction**: category-aware fields — `subject`, `parties`, and `reference_ids` for all document types; amount/currency only for invoices, receipts, bank/tax/utility/insurance documents. Missing `doc_type` defaults to `other`; common aliases (`document_type`, `category`) are accepted
- **Duplicate detection**: SHA-256 file checksums, normalized content hashes, and text-similarity matching flag re-scans and near-duplicates before they are filed
- **Ask your archive**: grounded RAG + FTS5 keyword search with source citations, a chat-style composer, and optional example questions (no “recent docs” padding when retrieval misses)
- **Local storage**: inbox and per-category archive folders, `data/deepcatalog.db` (SQLite + FTS5), `data/chroma/` (vectors) — always on this machine; cloud AI does not replace this store
- **Inbox polling**: automatic processing of new scans on a configurable interval
- **Local Ollama tooling**: start the daemon, pull models, show CPU/GPU usage for loaded models, unload or restart Ollama from Settings
- **Boot autostart (Linux)**: optional systemd user service so the web UI comes up after login or reboot
- **Web app**: full-height workbench for Inbox, Review, Archive, Ask, and Settings; keyboard shortcuts (`?` help, `/` Archive search); PDF preview in Review and Archive; four theme presets; toasts; mockup mode for screenshots
- **Linux AppImage**: download a single `x86_64` binary from GitHub Releases (bundles Python and Poppler; data stays in `~/.local/share/deepcatalog`)
- **Self-update**: check and install new releases from GitHub directly from Settings
- **CI & coverage**: `./scripts/ci.sh` runs format, lint, mypy, Vitest, and pytest with a coverage floor

## Screenshots

All five shots use **mockup mode** (Settings → Look & feel) with the **Slate** theme — demo documents only, nothing is read from or written to a real archive. Open the same scene in a browser with `http://127.0.0.1:8080/?mock=1&theme=slate`.

The hero shot above is **Inbox**: drop zone on the left, live workflow strip (Open → Transcribe → Find details → Name → Save → Review → Make searchable) and per-file queue on the right.

| Review workbench | Archive |
| --- | --- |
| ![Review queue, preview, and metadata editor](docs/screenshots/review.png) | ![Archive list with search and filters](docs/screenshots/archive.png) |

| Ask | Settings |
| --- | --- |
| ![Ask thread with cited sources and chat composer](docs/screenshots/ask.png) | ![Settings: AI provider and filing](docs/screenshots/settings.png) |

- **Review** — queue, document preview, and the editor (filename, category, date, parties, amount, summary). Approving files the document; rejecting removes the scan.
- **Archive** — full-height list with search, category, counterparty, and date filters.
- **Ask** — chat-style question and grounded answer with source documents.
- **Settings** — AI provider (ChatGPT / local or remote Ollama) and filing & scanning (inbox path, categories, OCR mode, human review).

Regenerate with `node scripts/capture-screenshots.mjs` while the app is on port 8080 (see [Project layout](#project-layout) for Playwright setup).

Product deck (open in a browser): [`docs/deck/index.html`](docs/deck/index.html) — arrow keys or Space to advance.

## Install

Cloning the repo alone is not enough — Python dependencies must be installed into a local virtualenv (`.venv`). Without that step you will see missing activate scripts and/or `ModuleNotFoundError: No module named 'fastapi'` when starting the app (often because a system-wide `uvicorn` is used instead of the venv one).

The recommended Linux desktop install is the **AppImage** (no Python/venv setup). The source installers download the **latest verified GitHub Release** tarball (same artifact as the in-app updater), sync into the install directory (preserving `.env`, `.venv`, `data/`, and `.git`), create `.venv`, install dependencies, and write a starter `.env`.

### Linux AppImage

Download the `x86_64` AppImage from [GitHub Releases](https://github.com/dpastoetter/DeepCatalog/releases/latest) (glibc 2.35+ — Ubuntu 22.04, Fedora 36, Debian 12, and newer). Poppler and a **private GTK + WebKitGTK window** (plus the Adwaita theme) are bundled. On first launch the AppImage installs a `.desktop` entry (`StartupWMClass=DeepCatalog`) so the window groups under its own icon. The Studio UI opens in that native window (title **DeepCatalog Studio**), not in Brave or Chrome. If WebKit cannot start, it falls back to a Chromium `--app` window (Chromium/Chrome before Brave), then to your default browser.

```bash
# Optional (source installs): native GTK window via host WebKitGTK
# Fedora / RHEL
sudo dnf install webkit2gtk4.1 python3-gobject
# Debian / Ubuntu
sudo apt install gir1.2-webkit2-4.1 python3-gi
```

```bash
# Pick the DeepCatalog-<version>-x86_64.AppImage asset from
# https://github.com/dpastoetter/DeepCatalog/releases/latest
chmod +x DeepCatalog-*-x86_64.AppImage
./DeepCatalog-*-x86_64.AppImage
```

The release asset is versioned (`DeepCatalog-<version>-x86_64.AppImage`). User data, `.env`, and the SQLite/Chroma stores live in `~/.local/share/deepcatalog` — not inside the image.

**In-app updates:** keep the AppImage on a writable path (for example `~/Applications/DeepCatalog.AppImage`). **Settings → Software update** downloads the signed AppImage from the release manifest, verifies SHA-256, replaces that file on disk, and relaunches. It does not rewrite the mounted squashfs. If the path is not writable, Settings still shows the newer version and links the GitHub asset for a manual replace.

Autostart from Settings writes a systemd user unit that launches this AppImage with `--headless`.

**Uninstall:** delete the AppImage and, if you want to drop local data, `rm -rf ~/.local/share/deepcatalog`. Disable Settings → Autostart first (or `systemctl --user disable deepcatalog.service`).

### Linux (source + venv)

Prerequisites: **Python 3.10+** (with `venv`), **curl**, **tar**, and **Poppler** (`pdftoppm`) for PDF OCR:

```bash
# Fedora / RHEL
sudo dnf install poppler-utils
# Debian / Ubuntu
sudo apt install python3-venv poppler-utils
```

```bash
curl -fsSL https://github.com/dpastoetter/DeepCatalog/releases/latest/download/install.sh | bash
```

```bash
cd ~/deepcatalog
source .venv/bin/activate
uvicorn app.main:app --port 8080
```

Optional install location / developer git mode:

```bash
DEEPCATALOG_DIR=~/apps/deepcatalog curl -fsSL \
  https://github.com/dpastoetter/DeepCatalog/releases/latest/download/install.sh | bash

DEEPCATALOG_INSTALL_SOURCE=git curl -fsSL \
  https://github.com/dpastoetter/DeepCatalog/releases/latest/download/install.sh | bash
```

**Uninstall** (app code, `.venv`, local `data/`, `.env`):

```bash
rm -rf "${DEEPCATALOG_DIR:-$HOME/deepcatalog}"
```

This does not delete ChatGPT/OpenAI credentials in `~/.codex/auth.json` or archive/inbox folders outside the install directory. If you enabled Settings → Autostart, disable it first or run `systemctl --user disable deepcatalog.service` (systemd autostart is Linux-only).

### macOS

Prerequisites via Homebrew:

```bash
brew install python poppler
```

Same installer as Linux:

```bash
curl -fsSL https://github.com/dpastoetter/DeepCatalog/releases/latest/download/install.sh | bash
```

```bash
cd ~/deepcatalog
source .venv/bin/activate
uvicorn app.main:app --port 8080
```

**Uninstall:**

```bash
rm -rf "${DEEPCATALOG_DIR:-$HOME/deepcatalog}"
```

Does not remove `~/.codex/auth.json` or external archive folders. Boot autostart (systemd) is **not available** on macOS — use Settings → Autostart only on Linux.

### Windows

Prerequisites: **Python 3.10+** from [python.org](https://www.python.org/downloads/) (or `winget install Python.Python.3.12`) and **Poppler** so `pdftoppm` is on `PATH` (for example a [poppler-windows](https://github.com/oschwartz10612/poppler-windows/releases) build, or search with `winget search poppler`).

In PowerShell:

```powershell
irm https://github.com/dpastoetter/DeepCatalog/releases/latest/download/install.ps1 | iex
```

```powershell
cd $env:USERPROFILE\deepcatalog
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

If `Activate.ps1` is blocked, run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

Optional: `$env:DEEPCATALOG_DIR = "$env:USERPROFILE\apps\deepcatalog"` before the installer. WSL can use the Linux `install.sh` flow instead.

**Uninstall:**

```powershell
Remove-Item -Recurse -Force "$env:USERPROFILE\deepcatalog"
```

Does not remove `%USERPROFILE%\.codex\auth.json` or archive folders outside the install directory. Boot autostart is **not supported** on Windows.

### Desktop window (optional)

From a venv install, after `pip install -e '.[desktop]' -c constraints.txt` (needs WebKitGTK on Linux):

```bash
python -m deepcatalog.desktop
```

The desktop wrapper respects `DEEPCATALOG_HOST`, `DEEPCATALOG_PORT`, and `DEEPCATALOG_LOG_LEVEL`. Pass `--headless` to keep the server in the foreground without a window (used by the AppImage systemd unit). On Linux it installs `~/.local/share/applications/deepcatalog.desktop` on launch. The AppImage ships WebKitGTK so this is a real GTK window. Source installs use host WebKitGTK + PyGObject; if that is missing, Chromium `--app` is the fallback, then a normal browser tab.

### Manual setup

Use this if you cloned with `git` and skipped the installer (Unix shell shown; on Windows use `py -3 -m venv .venv` and `.\.venv\Scripts\Activate.ps1`):

```bash
git clone https://github.com/dpastoetter/DeepCatalog.git
cd DeepCatalog
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e . -c constraints.txt
cp .env.example .env   # skip if .env already exists
chmod 600 .env         # owner-only; same posture as ~/.codex/auth.json
```

(`requirements.txt` is a thin wrapper around the same constrained install for the OS installers.)

If you already have a clone but no `.venv`, run the `venv` / `pip install` steps above (or re-run the OS installer) before starting the server.

## AI providers

Pick a provider in **Settings → AI provider**. Storage stays local in every mode; see [Privacy](#privacy-local-storage-vs-cloud-ai).

**ChatGPT and Gemini are cloud AI.** Page images and document text are sent to that provider for OCR, extraction, naming, and Ask. Those providers stay locked until you approve the **cloud processing disclaimer**.

**Local Ollama is the only fully local option** — no cloud account, no document data to an AI company. Remote Ollama still sends document content to the other host (separate disclaimer).

### OpenAI / ChatGPT

In the web UI, open **Settings → AI provider → Cloud (ChatGPT)** and choose one:

1. **Sign in with ChatGPT** — Codex-compatible OAuth (PKCE). Tokens are stored in `~/.codex/auth.json` and model calls use the Codex Responses backend against your ChatGPT subscription.
2. **Save API key** — stores a Platform `sk-…` key in the same auth file (usage-based billing).

You can also set `OPENAI_API_KEY` in `.env`. Auth resolution prefers an API key when present, otherwise ChatGPT OAuth tokens.

```bash
DEEPCATALOG_LLM_PROVIDER=openai
DEEPCATALOG_MODEL=gpt-5.6-luna
# Optional override for ChatGPT OAuth (defaults to gpt-5.6-luna):
# DEEPCATALOG_CODEX_MODEL=gpt-5.6-terra
DEEPCATALOG_EMBEDDING_MODEL=text-embedding-3-small
```

ChatGPT OAuth only supports Codex models (e.g. `gpt-5.6-luna` / `terra` / `sol`). Platform IDs like `gpt-4.1` are rejected — the app auto-falls back to `gpt-5.6-luna` in that case. ChatGPT OAuth cannot call Platform embedding APIs, so RAG uses a **local ONNX sentence embedding model** (`all-MiniLM-L6-v2` via Chroma). For OpenAI-hosted embeddings, save an API key. Fully local installs should use the Ollama provider with `nomic-embed-text`.

**Find details (metadata extract)** uses the Codex Responses streaming API. The pipeline asks for JSON only and parses fenced or partially wrapped replies; empty model output is reported distinctly from invalid JSON. Platform API-key mode can additionally request JSON object mode on chat completions.

### Gemini (cloud)

Google’s API is a cloud provider: document pages and text are sent to Google for OCR, extraction, and Ask, same as ChatGPT. Storage of the archive remains local.

```bash
DEEPCATALOG_LLM_PROVIDER=gemini
GOOGLE_API_KEY=...
DEEPCATALOG_MODEL=gemini-flash-latest
DEEPCATALOG_EMBEDDING_MODEL=text-embedding-004
```

### Ollama (fully local)

This is the only mode where **nothing leaves the machine** for AI. No cloud account, no API key. OCR, metadata extraction, embeddings, and Ask all go through Ollama on this computer. Local Ollama does not require the cloud processing disclaimer.

Remote Ollama (a server on another host) is **not** fully local — document data is sent to that host.

1. Install [Ollama](https://ollama.com/download) and make sure it is running (`ollama serve`), or use **Start Ollama** in Settings when the CLI is installed but the daemon is down.
2. Open the app → **Settings → AI provider → Local Ollama**.
3. Pick a **chat** model and an **embedding** model from the curated lists (defaults: multimodal `gemma3` + `nomic-embed-text`), then click **Pull required models**.

That writes the provider and model names into `.env` and switches the running app — no manual restart for the provider change. You can still configure it by hand:

```bash
ollama pull gemma3            # or llama3.2-vision, qwen2.5vl, minicpm-v, llava, moondream
ollama pull nomic-embed-text  # or mxbai-embed-large, bge-m3, all-minilm
```

```bash
DEEPCATALOG_LLM_PROVIDER=ollama
DEEPCATALOG_MODEL=gemma3
DEEPCATALOG_EMBEDDING_MODEL=nomic-embed-text
# OLLAMA_BASE_URL=http://localhost:11434   (default; loopback only)
# Remote Ollama (another host) needs Settings → Remote Ollama (privacy disclaimer)
# or DEEPCATALOG_ALLOW_REMOTE_OLLAMA=1 with a non-loopback OLLAMA_BASE_URL.
# Remote hostnames also need DEEPCATALOG_OLLAMA_ALLOWED_HOSTS=gpu.lan
# (literal IPs such as http://192.168.1.10:11434 do not rebind and skip the list
# unless the allowlist is set, in which case the IP must be listed too).
```

**User-local install (no sudo):** on Linux you can install the Ollama binary under `~/.local/bin` and libraries under `~/.local/lib/ollama`. If you use a user-local build, ensure `LD_LIBRARY_PATH` includes `~/.local/lib/ollama` when starting `ollama serve` (DeepCatalog’s systemd autostart unit sets this automatically when that directory exists).

While Ollama is the active provider, DeepCatalog checks that the daemon is reachable and listening (and that required models are present) before chat, OCR, or embedding work. Health reports `degraded` when the local instance is offline or models are missing. Settings shows:

- **CPU / GPU / CPU + GPU** — inferred from Ollama’s `/api/ps` (`size_vram` on loaded models)
- **Start Ollama** — launch the daemon (systemd unit when available, otherwise `ollama serve`)
- **Unload model** — free RAM/VRAM after a long OCR run
- **Restart Ollama** — recover from a stuck daemon (blocked while a file is processing unless you cancel first)

If `DEEPCATALOG_LLM_PROVIDER` is unset, the app auto-selects OpenAI when a key/Codex login is available, else Gemini when `GOOGLE_API_KEY` is set, else a **running local Ollama** when one responds on `OLLAMA_BASE_URL`. Set `DEEPCATALOG_SKIP_OLLAMA_PROBE=1` to skip the localhost probe during auto-detect (useful in tests or air-gapped installs).

## Run the web app

Always activate the project venv first so you use its packages, not the system ones:

```bash
cd ~/deepcatalog   # or your install directory
source .venv/bin/activate
python -m deepcatalog.serve --host 127.0.0.1 --port 8080
```

That owned entry records `DEEPCATALOG_HOST` / `DEEPCATALOG_PORT` and starts uvicorn with the same values, so the bind-address security check cannot drift from the socket. `uvicorn app.main:app --host …` still works; lifespan reads uvicorn’s real `--host` (not a defaulted env var) and refuses a network bind unless network mode is fully configured.

| Symptom | Fix |
| --- | --- |
| `.venv/bin/activate: No such file or directory` | Re-run the installer. On Debian/Ubuntu also install `python3-venv`, then remove a broken leftover with `rm -rf ~/deepcatalog/.venv` and install again |
| `ModuleNotFoundError: No module named 'fastapi'` | Same — dependencies were never installed into `.venv`, or the venv was not activated |
| Port already in use | Stop the other instance: `pkill -f "deepcatalog.serve"` / `pkill -f "uvicorn app.main:app"`, or pick another port with `--port 8081` and `DEEPCATALOG_PORT=8081` |
| Ollama OCR times out on CPU | Raise `DEEPCATALOG_OLLAMA_OCR_PAGE_TIMEOUT` (default 900s) or lower `DEEPCATALOG_OLLAMA_OCR_MAX_IMAGE_PX` (default 1024); see [OCR tuning](#ocr-and-long-documents) |
| Find details fails with “invalid JSON” | Usually empty/malformed model output — retry the file; with ChatGPT OAuth confirm a Codex model is selected and you are signed in |

Uvicorn should bind to `127.0.0.1` (the default). **Loopback is not an authentication boundary** — any local process or OS account can connect to `127.0.0.1:8080`. On first launch the app generates `DEEPCATALOG_API_TOKEN` and stores it in `DATA_DIR/.env` (mode `0600`). Every `/api/*` route except health and session/desktop bootstrap requires `Authorization: Bearer …` or an HttpOnly `deepcatalog_session` cookie. The desktop window bootstraps through a one-time nonce (not by treating localhost as logged-in). A browser on a shared machine uses the unlock panel with that token. Mutating routes also require a custom header the UI sends (CSRF hardening).

**Local mode** (default): `DEEPCATALOG_HOST=127.0.0.1` / `::1` — plain HTTP is fine on loopback, but **API auth is still required**. Set `DEEPCATALOG_SINGLE_USER=1` only on a dedicated machine if you explicitly want the old “anyone on loopback is trusted” behavior.

**Network mode** (bind `0.0.0.0`, a LAN IP, etc.): refused unless you set **all** of:

1. `DEEPCATALOG_ALLOW_REMOTE=1` (explicit opt-in — a token alone is not enough)
2. `DEEPCATALOG_API_TOKEN=…`
3. `DEEPCATALOG_SSL_CERTFILE` + `DEEPCATALOG_SSL_KEYFILE` pointing at existing PEM files (HTTPS on the uvicorn process)

A token over plain HTTP on a LAN can be intercepted; network mode therefore requires TLS on the app itself. Prefer keeping the app on loopback and terminating TLS on a reverse proxy instead (see [Network access (TLS reverse proxy)](#network-access-tls-reverse-proxy)). Credentials from non-loopback clients are refused unless the request is HTTPS (`X-Forwarded-Proto` is honored only for `DEEPCATALOG_TRUSTED_PROXIES`).

With a token configured (always, after first launch), every `/api/*` route except health/session/desktop bootstrap requires either `Authorization: Bearer <DEEPCATALOG_API_TOKEN>` (machine clients) or an HttpOnly `deepcatalog_session` cookie. Browser sessions are **random ids** created by `POST /api/auth/session` or by the desktop window’s one-time bootstrap URL. Loopback browsers do **not** auto-login unless `DEEPCATALOG_SINGLE_USER=1`. The long-lived API secret is never injected into JavaScript, reused as the cookie value, or accepted in a URL query string (`?token=` is ignored). Session creation and failed token checks are rate-limited per client IP (stricter on `POST /api/auth/session`); repeated failures are logged without the secret. Uvicorn access logs omit query strings and desktop-bootstrap path secrets so they cannot land in log files. Host headers are allowlisted (`DEEPCATALOG_ALLOWED_HOSTS`, defaulting to localhost / loopback) to harden against DNS rebinding. `X-Forwarded-*` is used only to recover the client IP / HTTPS scheme from a listed `DEEPCATALOG_TRUSTED_PROXIES` hop; it is never an authentication signal.

```bash
# First launch writes a random token into DATA_DIR/.env (0600). To set your own:
python -c "from deepcatalog.local_security import generate_api_token; print(generate_api_token())"
# → put the value in .env as DEEPCATALOG_API_TOKEN=…
# Optional: DEEPCATALOG_SESSION_TTL_SECONDS=86400  (default 24h)
# Dedicated single-user machine only (restores loopback auto-login — not for shared hosts):
# DEEPCATALOG_SINGLE_USER=1
```

Open [http://localhost:8080](http://localhost:8080).

### Network access (TLS reverse proxy)

Recommended pattern: keep DeepCatalog in **local mode** on loopback, and put Caddy / nginx / Traefik in front with HTTPS. The proxy speaks TLS to clients; the app still uses HTTP only on `127.0.0.1`.

1. Set a token and allow the public hostname in the Host allowlist:

```bash
DEEPCATALOG_HOST=127.0.0.1
DEEPCATALOG_PORT=8080
DEEPCATALOG_API_TOKEN=…          # required so LAN clients must authenticate
DEEPCATALOG_ALLOWED_HOSTS=deepcatalog.example.com,localhost,127.0.0.1
DEEPCATALOG_TRUSTED_PROXIES=127.0.0.1,::1
```

2. Example **Caddy** snippet:

```caddy
deepcatalog.example.com {
  reverse_proxy 127.0.0.1:8080
}
```

3. Example **nginx** location:

```nginx
server {
  listen 443 ssl http2;
  server_name deepcatalog.example.com;
  # ssl_certificate / ssl_certificate_key …
  location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    # Overwrite (do not append) so clients cannot spoof X-Forwarded-For.
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
  }
}
```

4. Example **Traefik** (label-style): route HTTPS entrypoint → `http://127.0.0.1:8080`, and only then set `DEEPCATALOG_TRUSTED_PROXIES` to the Traefik container/host IP.

Do **not** set `DEEPCATALOG_TRUSTED_PROXIES=0.0.0.0/0` (or `::/0`); those catch-alls are ignored. Only list the proxy addresses that terminate TLS. Public/proxied browsers always use the session unlock panel (or Bearer token); loopback auto-login is off unless `DEEPCATALOG_SINGLE_USER=1`, and it never applies behind a proxy.

If you truly need uvicorn itself on a non-loopback address, use network mode with app-level TLS:

```bash
DEEPCATALOG_HOST=0.0.0.0
DEEPCATALOG_ALLOW_REMOTE=1
DEEPCATALOG_API_TOKEN=…
DEEPCATALOG_SSL_CERTFILE=/path/to/fullchain.pem
DEEPCATALOG_SSL_KEYFILE=/path/to/privkey.pem
python -m deepcatalog.serve --host 0.0.0.0 --port 8443
```

### Autostart at boot (Linux + systemd)

**Settings → Autostart → Start DeepCatalog Studio when the system boots** installs a user systemd unit at `~/.config/systemd/user/deepcatalog.service`, runs `loginctl enable-linger` so the service can start without an interactive login, and starts the unit if port 8080 is free.

The unit reads your install’s `.env`, sets `DATA_DIR`, and starts `python -m deepcatalog.serve` from the project venv (same host/port as `DEEPCATALOG_HOST` / `DEEPCATALOG_PORT`). Customize bind address/port in `.env` before enabling autostart.

Manual control:

```bash
systemctl --user status deepcatalog.service
systemctl --user restart deepcatalog.service
journalctl --user -u deepcatalog.service -f
```

Autostart requires Linux with `systemctl --user`. It is hidden on other platforms.

## Typical flow

1. Upload a scan in **Inbox** (or copy files into your source folder)
2. Click **Process inbox** — or let the poller pick it up automatically
3. Watch the live workflow strip; use **Cancel** on the active file if needed, then **Retry** after it stops
4. Check **Review**: correct the proposal if needed, then **Approve & file** (duplicates are flagged with a match score)
5. Find the filed document in **Archive**, or ask questions in **Ask**

Filing rules (source folder, category → folder mapping, poll interval, OCR accuracy, review requirement) live in **Settings → Filing & scanning** and are stored in `data/settings.json`. The **Require human review** checkbox applies as soon as you toggle it (folders and poll interval still use **Save setup**). Older settings files missing a `review` or `ocr` key are filled in on load.

**Danger zone → Remove all stored data** deletes only (1) files tracked in the metadata DB whose paths still lie inside a configured archive root, (2) supported inbox scan files (PDF/images), and (3) the app-owned SQLite + Chroma stores under `DATA_DIR`. It never recursively wipes a category folder or the inbox directory. The API requires the confirmation phrase `DELETE ALL DEEPCATALOG DATA` (UI two-click confirm is not enough). Settings also refuse dangerous roots (`/`, `$HOME`, `~/Downloads`, `~/Documents`, the project root, system paths, and `DATA_DIR` itself).

### Processing controls

| Action | Where | Behavior |
| --- | --- | --- |
| **Cancel** | Inbox queue (active file) | Cooperative cancel — in-flight LLM/Ollama HTTP calls are aborted; button shows “Cancelling…” until the job ends |
| **Retry** | Inbox queue (error/cancelled) | Re-queues one file; if that file is still active, cancel is requested first |
| **Unload model** | Settings → Local Ollama | Drops loaded Ollama weights to free memory |
| **Restart Ollama** | Settings → Local Ollama | Restarts the daemon; blocked while processing unless the active file is cancelled |

Progress updates stream over `GET /api/process/events` (SSE). The workflow UI mounts once and patches in place to avoid flicker during rapid updates. Hover a pipeline step for a short description of what that stage does (and live detail while it is running). The strip stays on one row (labels wrap inside equal-height boxes; no horizontal scrollbar).

LLM prompts wrap OCR and retrieved snippets in **per-request** `BEGIN_UNTRUSTED_*_<token>` markers. Delimiter lookalikes inside the document (`BEGIN_UNTRUSTED_DOCUMENT`, `END_UNTRUSTED_DOCUMENT`, nested `_hex` fakes) are rewritten before wrapping so they cannot close the region. Extracted metadata and Ask replies are length-clamped in code; model output is treated as untrusted.

### Metadata & review fields

During **Extract**, the LLM returns JSON with:

| Field | Description |
| --- | --- |
| `doc_type` | One of your configured categories (built-in types include invoice, receipt, bank, tax, utility, insurance, letter, contract, certificate, employment, medical, id, education, travel, other). Defaults to `other` when omitted; aliases like `document_type` / `category` are normalized |
| `doc_date` | Document date when present |
| `subject` | Short topic or title |
| `parties` | Sender, recipient, issuer, etc. |
| `reference_ids` | Policy numbers, invoice numbers, case refs (list) |
| `amount` / `currency` | Only kept for financial categories |
| `summary` | Short plain-language summary |

The review form shows amount/currency only when the selected category is financial. Reference IDs are editable as a comma-separated list.

## OCR and long documents

Text recovery is **adaptive**. For each PDF page the agent assesses the embedded text layer first:

- **Fast** — use embedded text when anything usable is present; vision only if the page is nearly empty
- **Balanced** (default) — use embedded text when quality heuristics pass; vision for weak/garbled/scanned pages
- **Maximum** — always run AI vision OCR on every page (closest to the old always-vision behavior)

Image files always use vision. Change the mode under **Settings → Filing & scanning → OCR accuracy**, or set `DEEPCATALOG_OCR_MODE`.

By default, **all PDF pages** are considered (up to 128). Cloud providers run needed vision pages with **bounded concurrency** (`DEEPCATALOG_OCR_CONCURRENCY`, default 4). Local Ollama stays serial by default (`DEEPCATALOG_OCR_CONCURRENCY_OLLAMA=1`). Page images are downscaled before vision OCR.

Tune via `.env` (see `.env.example` for the full list):

| Variable | Default | Purpose |
| --- | --- | --- |
| `DEEPCATALOG_OCR_MODE` | `balanced` | `fast` \| `balanced` \| `maximum` (overrides Settings) |
| `DEEPCATALOG_OCR_CONCURRENCY` | `4` | Parallel vision pages for cloud providers |
| `DEEPCATALOG_OCR_CONCURRENCY_OLLAMA` | `1` | Parallel vision pages for local Ollama |
| `DEEPCATALOG_OCR_MAX_PAGES` | `0` (all) | Cap pages per document; `0` = all up to safety max |
| `DEEPCATALOG_OCR_SAFETY_MAX_PAGES` | `128` | Hard ceiling on page count |
| `DEEPCATALOG_OCR_DPI` | `200` | PDF render resolution |
| `DEEPCATALOG_OCR_PAGE_TIMEOUT` | `180` | Seconds per page (cloud providers) |
| `DEEPCATALOG_OLLAMA_OCR_PAGE_TIMEOUT` | `900` | Seconds per page (local Ollama — CPU can be slow) |
| `DEEPCATALOG_OCR_MAX_IMAGE_PX` | `1536` | Max width/height before vision OCR (cloud) |
| `DEEPCATALOG_OLLAMA_OCR_MAX_IMAGE_PX` | `1024` | Max width/height for local Ollama OCR |
| `DEEPCATALOG_OLLAMA_OCR_NUM_CTX` | `4096` | Ollama context for OCR calls |
| `DEEPCATALOG_OLLAMA_OCR_NUM_PREDICT` | `4096` | Max tokens per OCR page (Ollama) |
| `DEEPCATALOG_OLLAMA_VISION_NUM_CTX` | `8192` | Ollama context for other vision calls |
| `DEEPCATALOG_OLLAMA_VISION_NUM_PREDICT` | `8192` | Max tokens for other vision calls |
| `DEEPCATALOG_EXTRACT_MAX_CHARS` | `48000` | OCR text sampled for metadata extraction (head + tail when longer) |

LLM timeouts (chat, vision, Ollama) and cancel join behavior:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DEEPCATALOG_LLM_TIMEOUT` | `120` | Text completion timeout (seconds) |
| `DEEPCATALOG_LLM_VISION_TIMEOUT` | `300` | Cloud vision timeout |
| `DEEPCATALOG_OLLAMA_TIMEOUT` | `300` | Ollama chat/vision timeout |
| `DEEPCATALOG_CANCEL_JOIN_TIMEOUT` | `3` | Max wait when joining a cancelled async LLM call |

If you switch embedding providers or models (Gemini / OpenAI / Ollama / local ONNX), the app **detects the mismatch automatically** (stored `embedding_provider`, `model`, `dimension`, and index schema version in `data/chroma/index_meta.json`) and rebuilds the vector index on the next index or Ask request. You do not need to remember to re-index manually.

**Ask grounding:** answers use only confident semantic hits (cosine distance ≤ `DEEPCATALOG_ASK_MAX_CHUNK_DISTANCE`, default `0.55`) plus SQLite FTS5 / metadata matches. If nothing relevant is retrieved, Ask says there isn’t enough evidence — it does **not** fall back to recent unrelated documents.

## Updates

**Settings → Software update** checks GitHub Releases for `dpastoetter/DeepCatalog` and can download and install the latest **signed** release in place. Your documents, database, settings, and credentials (`data/`, `.env`, or `~/.local/share/deepcatalog` for AppImage) are never touched by an update.

| Install type | What “Download & install” does |
| --- | --- |
| Source / venv | Downloads the signed `.tar.gz`, verifies Ed25519 manifest + SHA-256 + `.release-commit`, extracts into the install directory (preserves `data/`, `.env`, `.venv`) |
| AppImage | Downloads the signed `.AppImage` listed in the manifest, verifies SHA-256, atomically replaces the outer `$APPIMAGE` file, then relaunches that path |

Keep the AppImage somewhere writable. If `$APPIMAGE` is missing or not writable, Settings reports the newer version and links the GitHub asset instead of offering install.

Installs are **fail-closed on provenance**: checksum files on the same GitHub Release are not authentic (whoever can upload `evil.tar.gz` can upload a matching `SHA256SUMS`). The updater requires an **Ed25519-signed** `release-manifest.json` whose public key is embedded in the app (`deepcatalog/release_trust.py`). The manifest binds the GitHub tag, the 40-character commit SHA, and each artifact’s SHA-256. Tag-only or checksum-only releases are shown but refused at install time. `DEEPCATALOG_UPDATE_REPO` is ignored by the in-app updater so an environment change cannot redirect the trust root; forks must change `DEFAULT_UPDATE_REPO` and the verify key in source. Update checks retry on transient network failures.

CI also publishes **SLSA / Sigstore** artifact attestations for every official file (tarball, AppImage, installers, signed manifest, checksums). Independently:

```bash
gh attestation verify deepcatalog-0.7.1.tar.gz \
  --repo dpastoetter/DeepCatalog \
  --cert-identity https://github.com/dpastoetter/DeepCatalog/.github/workflows/release.yml \
  --cert-oidc-issuer https://token.actions.githubusercontent.com
gh attestation verify install.sh \
  --repo dpastoetter/DeepCatalog \
  --cert-identity https://github.com/dpastoetter/DeepCatalog/.github/workflows/release.yml \
  --cert-oidc-issuer https://token.actions.githubusercontent.com
```

Official GitHub Release assets are published **only** by `.github/workflows/release.yml` after quality + security + AppImage. A pre-populated release (for example an AppImage uploaded in the UI) does **not** skip those gates; the workflow rebuilds from the tag and deletes every existing asset before uploading its own. `curl … | bash` / `irm … | iex` installers are those CI assets.

That pipeline runs when you:

- push a version tag (`git tag v0.7.1 && git push origin v0.7.1`) — intended path
- publish a GitHub Release in the UI (or `gh release create`) for a `v*` tag — treated as a rebuild that replaces UI files
- run **Actions → Release → Run workflow** *from that tag ref* (not a branch)

Do not attach files in the GitHub Release UI. The standalone **AppImage** workflow uploads an Actions artifact only; it never publishes a Release.

**Repo protection (configure once in GitHub):**

1. Environment **`official-release`** (the publish job waits here): required reviewers; limit deployments to tags matching `v[0-9]*`; store `DEEPCATALOG_RELEASE_SIGNING_KEY` as an environment secret rather than a repo secret.
2. Tag ruleset on `refs/tags/v*`: block updates and deletions (no force-push / retarget); restrict who can create version tags.

The packager archives the **exact tagged commit** (not a dirty working tree), verifies the file list against `git ls-tree`, and embeds `.release-commit` plus `.release-files` so installs/updates can confirm the SHA and prune stale paths.

**Release checklist:** land every change on `main` first, bump `version` in `pyproject.toml` (the single source for package metadata, OpenAPI/`FastAPI.version`, and the in-app updater), regenerate pins if dependencies changed (`./scripts/lock-deps.sh`), commit, then create the matching tag on that commit (`git tag v0.6.5 && git push origin v0.6.5`). Tagging an older commit is how earlier releases missed later work. Publishing requires the Actions secret `DEEPCATALOG_RELEASE_SIGNING_KEY` (64-char hex Ed25519 seed, stored separately from `GITHUB_TOKEN`, preferably on the `official-release` environment) and an approval of that environment. Do not draft a GitHub Release with uploaded binaries.

Local dry-run:

```bash
git checkout v0.6.5
./scripts/make-release-assets.sh v0.6.5
# or before the tag exists:
./scripts/make-release-assets.sh v0.6.5 HEAD
# dist/ contains deepcatalog-0.6.5.tar.gz, install.sh, install.ps1, SHA256SUMS
# CI then signs SHA256SUMS → release-manifest.json + .sig (needs DEEPCATALOG_RELEASE_SIGNING_KEY)

# Linux x86_64 AppImage (needs poppler-utils + patchelf):
./scripts/build-appimage.sh v0.7.1
# dist/DeepCatalog-0.7.1-x86_64.AppImage
```

## Mockup mode

**Settings → Look & feel → Mockup mode** fills every view with demo data — useful for screenshots and demos without exposing personal documents. It is client-side only: while enabled, no real data is read or written, and all mutating actions are blocked. Also available ad hoc via URL parameters: `http://localhost:8080/?mock=1&theme=slate#/archive`.

## Development

### Tests & CI quality gate

```bash
pip install -e ".[dev]" -c constraints.txt   # pytest, pytest-cov, ruff, mypy, pip-tools
./scripts/ci.sh                 # quality gate (format, lint, pip check, mypy, JS, Vitest, pytest+coverage)
./scripts/dependency-audit.sh   # pip-audit (OSV) + npm audit
python scripts/chroma-advisory-watch.py  # unsuppressed Chroma/OSV watch (scheduled in CI)
./scripts/secret-scan.sh        # gitleaks (pinned binary, checksum-verified)
./scripts/sast-scan.sh          # Semgrep ERROR-severity (Python + JavaScript)
./scripts/precommit.sh          # secret guard + CI gate (also usable as a git hook)
```

Coverage is measured with `pytest-cov` (`deepcatalog`, `app`, `query_agent`; branch coverage). CI fails if total coverage drops below the floor in `pyproject.toml` (`--cov-fail-under`). HTML report: `htmlcov/` (gitignored). Desktop GUI (`desktop.py`) and live network/Ollama/systemd are mocked or omitted — unit-test their helpers instead.

Pure frontend helpers (`app/static/api.js`, `router.js`, `state.js`) have a small Vitest harness (`npm test`). Install Node deps once with `npm install`; CI runs the same suite after the JS syntax check.

Install the pre-commit hook once per clone so the gate runs on every commit:

```bash
ln -sf ../../scripts/precommit.sh .git/hooks/pre-commit
```

The pre-commit gate refuses commits containing `.env` files, databases, `data/` content, or anything that looks like an API key.

### Dependencies

`pyproject.toml` is the source of truth for direct dependencies. Exact transitive versions are pinned in `constraints.txt` (regenerate with `./scripts/lock-deps.sh` after changing deps).

| Install | Command |
| --- | --- |
| Runtime | `pip install -e . -c constraints.txt` |
| Desktop shell | `pip install -e '.[desktop]' -c constraints.txt` |
| Dev tools | `pip install -e '.[dev]' -c constraints.txt` |

Installers still call `pip install -r requirements.txt` / `requirements-desktop.txt`, which are thin wrappers around those constrained editable installs (not a second dependency list).

Pull requests and pushes to `main` run [`.github/workflows/ci.yml`](.github/workflows/ci.yml) (`scripts/ci.sh` plus the reusable [security workflow](.github/workflows/security.yml): `pip-audit`/OSV + npm audit, gitleaks, Semgrep, and CodeQL). Version tags and published GitHub Releases run the same quality and security gates on the **exact tagged commit**, then pack the Linux AppImage and publish assets ([`.github/workflows/release.yml`](.github/workflows/release.yml)). Workflows pin Actions to full commit SHAs and grant `contents: write` only to the release publish job. Dependabot opens weekly PRs for GitHub Actions and pip, and monthly PRs for npm.

`constraints.txt` pins `chromadb==1.5.9`. CVE-2026-45829 (pre-auth code injection) and CVE-2026-45833 (authenticated injection) affect Chroma **1.0.0–1.5.9** with **no patched release** as of 10 September 2026. Both target Chroma’s HTTP/FastAPI server, not the embedded client. DeepCatalog only constructs `PersistentClient` through [`deepcatalog/chroma_local.py`](deepcatalog/chroma_local.py) (explicit settings so `CHROMA_SERVER_*` / `CHROMA_API_IMPL` cannot switch the process to HTTP) and never starts a Chroma listener. PR/release `pip-audit` therefore ignores `PYSEC-2026-311` / `GHSA-f4j7-r4q5-qw2c`. A **separate** weekly job ([`.github/workflows/advisory-watch.yml`](.github/workflows/advisory-watch.yml), `scripts/chroma-advisory-watch.py`) runs the same OSV scan **without** those ignores and fails when a newer chromadb lands on PyPI or a non-Chroma advisory appears. Upgrade, fork, or replace Chroma when a fix exists; then drop the CI ignores.

### ADK agents (debug, localhost only)

Production ingest (`deepcatalog.ingest`) and Ask (`deepcatalog.ask`) do **not** use
Google ADK tool agents. They call `complete_text` / `complete_with_images`. Ollama
always goes through `trusted_ollama_origin()` (including the ADK debug factory in
[`deepcatalog/adk_debug.py`](deepcatalog/adk_debug.py) — never a raw `OLLAMA_BASE_URL`).

The ADK agents exist for local `adk web` / `adk run` while developing. `adk web`
already defaults to `127.0.0.1`. Do **not** pass `--host 0.0.0.0`, open the port on
the LAN, or put it behind the DeepCatalog TLS proxy — the debug UI can invoke
inbox/file/RAG tools through the model.

```bash
adk web --host 127.0.0.1 --port 8000
# or
adk run deepcatalog
adk run query_agent
```

### Inbox watcher (CLI alternative to the built-in poller)

```bash
python scripts/watch_inbox.py --process-existing
```

## Project layout

```
deepcatalog/       # ingest pipeline, review queue, dedup, updater, auth/llm helpers
  ingest.py            #   OCR → extract → name → review gate → file + index
  ocr.py               #   adaptive text-layer / vision OCR, PDF render, image prep
  llm.py               #   production OpenAI / Codex / Gemini / Ollama completions + cancel
  adk_debug.py         #   local `adk web` / `adk run` only (not production ingest/Ask)
  prompt_safety.py     #   untrusted-content markers + output clamps
  providers/           #   LlmProvider interface (text, vision, embeddings, health, usage)
  progress.py          #   SSE progress + pipeline step labels/descriptions
  job_control.py       #   per-file cancel events for ingest
  review.py            #   human-in-the-loop queue (approve / reject)
  dedup.py             #   checksum + content-hash + similarity duplicate detection
  ollama_setup.py      #   Ollama probe, model pull, CPU/GPU summary, provider switch
  system_service.py    #   systemd user unit for boot autostart
  chroma_local.py      #   embedded PersistentClient only (no Chroma HTTP server)
  local_security.py    #   bind policy, token, Host allowlist (loopback is not auth)
  api_token.py         #   generate/persist DEEPCATALOG_API_TOKEN on first launch
  desktop_bootstrap.py #   one-time nonce for the native window session cookie
  serve.py             #   owned uvicorn entry (bind host + security policy stay aligned)
  access_log.py        #   strip query strings and bootstrap nonces from uvicorn logs
  auth_rate_limit.py   #   per-IP backoff for session exchange and failed token checks
  updater.py           #   self-update from GitHub releases
query_agent/           # RAG Q&A agent
app/                   # FastAPI app (main.py + routers/) and ES-module UI (static/)
  routers/             #   documents, reviews, settings, processing, auth, updates
  schemas.py           #   request/response models
  static/              #   api.js, inbox.js, review.js, settings.js, events.js, keyboard.js, pdf-preview.js, …
scripts/               # install.sh, install.ps1, ci.sh, dependency-audit.sh, chroma-advisory-watch.py, secret-scan.sh, sast-scan.sh, precommit.sh, watch_inbox.py, …
packaging/linux/       # AppImage AppRun, .desktop, icon
tests/                 # pytest (Python) + tests/frontend (Vitest)
docs/screenshots/      # README screenshots (generated with mockup mode)
docs/deck/             # product slide deck (open index.html in a browser)
package.json           # Vitest for pure ES-module UI helpers
data/                  # created at runtime (gitignored)
```

Regenerate README screenshots (server must be running on port 8080):

```bash
# python -m deepcatalog.serve --host 127.0.0.1 --port 8080
npm install --no-save playwright   # first time
npx playwright install chromium    # first time
node scripts/capture-screenshots.mjs
```

## Configuration reference

All optional variables are documented in `.env.example`. Common entries:

```bash
DATA_DIR=./data
DEEPCATALOG_HOST=127.0.0.1
DEEPCATALOG_PORT=8080
DEEPCATALOG_LLM_PROVIDER=ollama   # gemini | openai | ollama
DEEPCATALOG_MODEL=gemma3
DEEPCATALOG_EMBEDDING_MODEL=nomic-embed-text
OLLAMA_BASE_URL=http://localhost:11434
```

Runtime settings (inbox path, categories, poll interval, review requirement) are edited in the web UI and stored in `data/settings.json`, not in `.env`.

## License

[Apache License 2.0](LICENSE) — Copyright 2026 dpastoetter
