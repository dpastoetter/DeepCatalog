# Security policy

## Supported versions

| Version | Security fixes |
| --- | --- |
| Latest released `0.7.x` (see [GitHub Releases](https://github.com/dpastoetter/DeepCatalog/releases)) | Supported |
| Older `0.7.x` patch releases | Best effort — upgrade to the latest release |
| `0.6.x` and earlier | Not supported |

Fixes ship in a new tagged release when practical. There is no long-term support branch.

## Reporting a vulnerability

**Do not open a public GitHub issue, discussion, or pull request for an unpatched vulnerability.** Public reports can put users at risk before a fix exists.

<!--
  MAINTAINER TODO: enable GitHub Private Vulnerability Reporting
  (repo Settings → Code security → Private vulnerability reporting),
  then replace the placeholder below with that URL as the primary channel.
-->

**Contact (placeholder — maintainer must configure):**  
`[SECURITY_CONTACT_PLACEHOLDER]` — replace this with a monitored private contact **or** enable [GitHub Private Vulnerability Reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) and point researchers to **Security → Advisories → Report a vulnerability** on this repository. No security email is published in this policy until that is done.

### What to include

A useful report usually contains:

- Affected version (git tag, AppImage filename, or `pyproject.toml` version) and install type (source / AppImage / systemd)
- Deployment mode (loopback local vs `DEEPCATALOG_ALLOW_REMOTE` / reverse proxy)
- Clear steps to reproduce, or a minimal proof of concept
- Impact (auth bypass, CSRF, RCE via document parse, path escape, updater integrity, etc.)
- Whether you tested against the latest release

### Sensitive proof-of-concept material

Treat weaponized PDFs, exploit scripts, credentials, and session tokens as sensitive:

- Prefer private advisory attachments or encrypted transfer once a contact channel exists
- Do not attach live secrets or production document corpora to public issues
- Redact tokens, cookies, and personal document content; describe structure instead when possible
- We will not ask you to test against third-party systems you do not own

We aim to acknowledge private reports and to coordinate disclosure after a fix or mitigating guidance is available. Timelines depend on severity and maintainer capacity.

## Scope (in)

Reports in these areas are welcome:

| Area | Examples |
| --- | --- |
| **Web / API auth** | Bearer token or session bypass, token leakage into JS/logs/URLs, Host / proxy spoofing, rate-limit bypass that enables brute force |
| **CSRF** | Cross-site state-changing requests that succeed without the required `X-Requested-With: DeepCatalog` header (or equivalent bypass) |
| **Document parsing** | Crashes or code execution via hostile PDF/image through `media_worker` / Poppler / Pillow / pypdf; sandbox escape from the media child |
| **Updater** | Supply-chain issues in signed release verification, HTTPS download redirect tricks, path traversal on install |
| **Integrations** | SSRF or credential abuse via Ollama / OpenAI / Gemini configuration; remote-Ollama URL validation gaps |
| **Local filesystem** | Inbox/archive path escape, symlink tricks past filing confinement, unintended reads/writes outside configured roots |

## Scope (out / known assumptions)

These are **architectural assumptions**, not bugs by themselves. Reports that only restate them without a concrete bypass are usually closed as informational:

- **Local-first:** default bind is loopback. **Loopback is not an authentication boundary** — any local account/process can reach the port; API auth is still required unless `DEEPCATALOG_SINGLE_USER=1` on a dedicated machine.
- **Remote binding requires TLS:** non-loopback bind needs `DEEPCATALOG_ALLOW_REMOTE=1`, an API token, and app-level TLS PEMs (or a TLS reverse proxy to loopback with trusted `X-Forwarded-*` hops only).
- **Embedded-only Chroma:** DeepCatalog uses `PersistentClient` / `RustBindingsAPI` via `deepcatalog/chroma_local.py` and must not start or connect to a Chroma HTTP server. Known Chroma *server* CVEs are tracked with documented compensating controls and expiry-gated audit suppressions — report a regression if HTTP/server mode becomes reachable from this app.
- **Hostile documents are untrusted input:** OCR/RAG text can steer models; prompt markers are **not** a security boundary. Filing still depends on allowlists, path confinement, media isolation, and (by default) human review. Model “jailbreaks” that only change suggested metadata without bypassing those controls are expected residual risk.

Operational misconfiguration (publishing without a token, trusting `0.0.0.0/0` proxies, disabling the media worker on a network bind) is out of scope unless the app fails to refuse an unsafe combination it claims to block.

## Prefer private disclosure

Please use the private channel above. Opening a public issue for an unpatched vulnerability may force an incomplete advisory and puts users who have not upgraded yet at risk.
