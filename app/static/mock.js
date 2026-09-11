/*
 * Mockup mode: fills every view with canned demo data for screenshots.
 *
 * Client-side only — while enabled, api() never talks to the backend and all
 * mutating actions are blocked, so real documents and settings are untouched.
 * Enable via the Settings toggle (persisted) or a ?mock=1 URL parameter.
 */
(function () {
  const param = new URLSearchParams(window.location.search).get("mock");
  let stored = null;
  try {
    stored = localStorage.getItem("dc-mock");
  } catch (_err) {
    // private mode
  }
  const enabled = param !== null ? param !== "0" : stored === "1";

  const DOCS = [
    {
      id: "demo-doc-1",
      filename: "2024-06-03_Invoice_AcmeCorp_EUR1240.pdf",
      original_name: "scan_20240603_1412.pdf",
      path: "/home/demo/DeepCatalog/archive/invoice/2024/2024-06-03_Invoice_AcmeCorp_EUR1240.pdf",
      doc_type: "invoice",
      doc_date: "2024-06-03",
      subject: "Consulting services May 2024",
      counterparties: "Acme Corp",
      amount: 1240.0,
      currency: "EUR",
      summary:
        "Invoice #2024-118 from Acme Corp for consulting services in May, total EUR 1,240.00 incl. VAT, due 2024-06-17.",
      created_at: "2024-06-03T14:20:00Z",
    },
    {
      id: "demo-doc-2",
      filename: "2024-05-28_Contract_Stadtwerke.pdf",
      original_name: "vertrag_stadtwerke.pdf",
      path: "/home/demo/DeepCatalog/archive/contract/2024/2024-05-28_Contract_Stadtwerke.pdf",
      doc_type: "contract",
      doc_date: "2024-05-28",
      subject: "Electricity supply agreement",
      counterparties: "Stadtwerke München",
      amount: null,
      currency: null,
      summary:
        "Electricity supply contract with Stadtwerke München, base price EUR 12.90/month, 24-month term starting July 2024.",
      created_at: "2024-05-28T10:00:00Z",
    },
    {
      id: "demo-doc-3",
      filename: "2024-05-12_Tax_Finanzamt_EUR842.pdf",
      original_name: "steuerbescheid_2023.pdf",
      path: "/home/demo/DeepCatalog/archive/tax/2024/2024-05-12_Tax_Finanzamt_EUR842.pdf",
      doc_type: "tax",
      doc_date: "2024-05-12",
      subject: "Income tax assessment 2023",
      counterparties: "Finanzamt München",
      amount: 842.0,
      currency: "EUR",
      summary:
        "Income tax assessment for 2023 with a refund of EUR 842.00, transferred to the account on file.",
      created_at: "2024-05-12T09:10:00Z",
    },
    {
      id: "demo-doc-4",
      filename: "2024-04-19_Medical_MRI-right-knee_Dr-Weber.pdf",
      original_name: "befund_scan.pdf",
      path: "/home/demo/DeepCatalog/archive/medical/2024/2024-04-19_Medical_MRI-right-knee_Dr-Weber.pdf",
      doc_type: "medical",
      doc_date: "2024-04-19",
      subject: "MRI right knee results",
      counterparties: "Dr. Weber",
      amount: null,
      currency: null,
      summary:
        "Radiology report from Dr. Weber: MRI of the right knee, no structural damage, physiotherapy recommended.",
      created_at: "2024-04-19T16:40:00Z",
    },
    {
      id: "demo-doc-5",
      filename: "2024-03-30_Receipt_Bauhaus_EUR67p80.jpg",
      original_name: "IMG_2041.jpg",
      path: "/home/demo/DeepCatalog/archive/receipt/2024/2024-03-30_Receipt_Bauhaus_EUR67p80.jpg",
      doc_type: "receipt",
      doc_date: "2024-03-30",
      subject: "Paint and brushes purchase",
      counterparties: "Bauhaus",
      amount: 67.8,
      currency: "EUR",
      summary: "Hardware store receipt from Bauhaus: paint, brushes, and masking tape, EUR 67.80 total.",
      created_at: "2024-03-30T18:05:00Z",
    },
    {
      id: "demo-doc-6",
      filename: "2024-06-10_Invoice_NordSoft_EUR490.pdf",
      original_name: "nordsoft_june.pdf",
      path: "/home/demo/DeepCatalog/archive/invoice/2024/2024-06-10_Invoice_NordSoft_EUR490.pdf",
      doc_type: "invoice",
      doc_date: "2024-06-10",
      subject: "SaaS subscription June 2024",
      counterparties: "NordSoft GmbH",
      amount: 490.0,
      currency: "EUR",
      summary: "Monthly SaaS invoice from NordSoft GmbH for team seats, EUR 490.00.",
      created_at: "2024-06-10T08:15:00Z",
    },
    {
      id: "demo-doc-7",
      filename: "2024-02-14_Letter_Landlord_rent-increase.pdf",
      original_name: "schreiben_vermieter.pdf",
      path: "/home/demo/DeepCatalog/archive/letter/2024/2024-02-14_Letter_Landlord_rent-increase.pdf",
      doc_type: "letter",
      doc_date: "2024-02-14",
      subject: "Notice of rent adjustment",
      counterparties: "Hausverwaltung Berger",
      amount: null,
      currency: null,
      summary: "Letter from Hausverwaltung Berger announcing a rent adjustment effective May 2024.",
      created_at: "2024-02-14T11:22:00Z",
    },
    {
      id: "demo-doc-8",
      filename: "2024-01-22_Receipt_DB_EUR48p60.pdf",
      original_name: "bahn_ticket.pdf",
      path: "/home/demo/DeepCatalog/archive/receipt/2024/2024-01-22_Receipt_DB_EUR48p60.pdf",
      doc_type: "receipt",
      doc_date: "2024-01-22",
      subject: "Train ticket München–Nürnberg",
      counterparties: "Deutsche Bahn",
      amount: 48.6,
      currency: "EUR",
      summary: "DB ticket München to Nürnberg, EUR 48.60, travel date 2024-01-22.",
      created_at: "2024-01-22T07:40:00Z",
    },
    {
      id: "demo-doc-9",
      filename: "2023-12-01_Contract_HealthInsurance.pdf",
      original_name: "kv_vertrag.pdf",
      path: "/home/demo/DeepCatalog/archive/contract/2023/2023-12-01_Contract_HealthInsurance.pdf",
      doc_type: "contract",
      doc_date: "2023-12-01",
      subject: "Private health insurance policy",
      counterparties: "Allianz Private Krankenversicherung",
      amount: null,
      currency: null,
      summary: "Private health insurance policy documents from Allianz, effective December 2023.",
      created_at: "2023-12-01T12:00:00Z",
    },
    {
      id: "demo-doc-10",
      filename: "2024-05-02_Invoice_AcmeCorp_EUR220.pdf",
      original_name: "acme_may_addon.pdf",
      path: "/home/demo/DeepCatalog/archive/invoice/2024/2024-05-02_Invoice_AcmeCorp_EUR220.pdf",
      doc_type: "invoice",
      doc_date: "2024-05-02",
      subject: "Emergency support callout",
      counterparties: "Acme Corp",
      amount: 220.0,
      currency: "EUR",
      summary: "Invoice #2024-099 from Acme Corp for an after-hours support callout, EUR 220.00.",
      created_at: "2024-05-02T19:30:00Z",
    },
    {
      id: "demo-doc-11",
      filename: "2024-03-08_Medical_Bloodwork_LabNord.pdf",
      original_name: "labor_befund.pdf",
      path: "/home/demo/DeepCatalog/archive/medical/2024/2024-03-08_Medical_Bloodwork_LabNord.pdf",
      doc_type: "medical",
      doc_date: "2024-03-08",
      subject: "Routine bloodwork results",
      counterparties: "LabNord",
      amount: null,
      currency: null,
      summary: "Laboratory results from LabNord: routine blood panel within normal ranges.",
      created_at: "2024-03-08T14:55:00Z",
    },
    {
      id: "demo-doc-12",
      filename: "2024-04-01_Tax_VAT-Q1_EUR310.pdf",
      original_name: "ust_q1.pdf",
      path: "/home/demo/DeepCatalog/archive/tax/2024/2024-04-01_Tax_VAT-Q1_EUR310.pdf",
      doc_type: "tax",
      doc_date: "2024-04-01",
      subject: "VAT return Q1 2024",
      counterparties: "Finanzamt München",
      amount: 310.0,
      currency: "EUR",
      summary: "VAT filing for Q1 2024 with a payable amount of EUR 310.00.",
      created_at: "2024-04-01T09:05:00Z",
    },
    {
      id: "demo-doc-13",
      filename: "2023-11-18_ID_Passport-scan.pdf",
      original_name: "pass_scan.pdf",
      path: "/home/demo/DeepCatalog/archive/id/2023/2023-11-18_ID_Passport-scan.pdf",
      doc_type: "id",
      doc_date: "2023-11-18",
      subject: "Passport scan",
      counterparties: null,
      amount: null,
      currency: null,
      summary: "Color scan of passport identification page for personal records.",
      created_at: "2023-11-18T20:10:00Z",
    },
    {
      id: "demo-doc-14",
      filename: "2024-06-18_Receipt_Rewe_EUR34p12.jpg",
      original_name: "rewe_receipt.jpg",
      path: "/home/demo/DeepCatalog/archive/receipt/2024/2024-06-18_Receipt_Rewe_EUR34p12.jpg",
      doc_type: "receipt",
      doc_date: "2024-06-18",
      subject: "Groceries",
      counterparties: "REWE",
      amount: 34.12,
      currency: "EUR",
      summary: "Grocery receipt from REWE, EUR 34.12 total.",
      created_at: "2024-06-18T17:45:00Z",
    },
  ];

  function mockFilterDocuments(path) {
    const url = new URL(path, "http://mock.local");
    const q = (url.searchParams.get("q") || "").trim().toLowerCase();
    const docType = (url.searchParams.get("doc_type") || "").trim();
    const counterparty = (url.searchParams.get("counterparty") || "").trim().toLowerCase();
    const dateFrom = (url.searchParams.get("date_from") || "").trim();
    const dateTo = (url.searchParams.get("date_to") || "").trim();
    const limit = Math.max(1, Math.min(100, Number(url.searchParams.get("limit") || 40) || 40));
    const offset = Math.max(0, Number(url.searchParams.get("offset") || 0) || 0);

    let rows = DOCS.slice().sort((a, b) => String(b.doc_date || "").localeCompare(String(a.doc_date || "")));
    if (docType) rows = rows.filter((d) => d.doc_type === docType);
    if (counterparty) {
      rows = rows.filter((d) => {
        const hay = `${d.counterparties || ""} ${d.subject || ""}`.toLowerCase();
        return hay.includes(counterparty);
      });
    }
    if (dateFrom) rows = rows.filter((d) => (d.doc_date || "") >= dateFrom);
    if (dateTo) rows = rows.filter((d) => (d.doc_date || "") <= dateTo);
    if (q) {
      rows = rows.filter((d) => {
        const hay = [
          d.filename,
          d.original_name,
          d.subject,
          d.counterparties,
          d.summary,
          d.doc_type,
        ]
          .join(" ")
          .toLowerCase();
        return hay.includes(q);
      });
    }
    const slice = rows.slice(offset, offset + limit + 1);
    const hasMore = slice.length > limit;
    const page = slice.slice(0, limit);
    return {
      status: "success",
      count: page.length,
      documents: page,
      search: q ? "like" : "filter",
      has_more: hasMore,
      offset,
      limit,
    };
  }

  const REVIEWS = [
    {
      id: "demo-review-1",
      source_path: "/home/demo/DeepCatalog/inbox/scan_20240614_0932.pdf",
      original_name: "scan_20240614_0932.pdf",
      status: "pending",
      created_at: "2024-06-14T09:32:00Z",
      proposal: {
        filename: "2024-06-14_Invoice_AcmeCorp_EUR380.pdf",
        doc_type: "invoice",
        doc_date: "2024-06-14",
        subject: "On-site support June 12",
        counterparties: "Acme Corp",
        reference_ids: ["2024-131"],
        amount: 380.0,
        currency: "EUR",
        summary:
          "Invoice #2024-131 from Acme Corp for on-site support on June 12, total EUR 380.00 incl. VAT.",
      },
      duplicates: [
        {
          kind: "similar",
          document_id: "demo-doc-1",
          filename: "2024-06-03_Invoice_AcmeCorp_EUR1240.pdf",
          score: 0.86,
        },
      ],
    },
    {
      id: "demo-review-2",
      source_path: "/home/demo/DeepCatalog/inbox/chess_board.jpg",
      original_name: "chess_board.jpg",
      status: "pending",
      created_at: "2024-06-15T18:20:00Z",
      proposal: {
        filename: "2024-06-15_other_ChessBoardMidGame.jpg",
        doc_type: "other",
        doc_date: "2024-06-15",
        subject: "Chess board mid-game position",
        counterparties: null,
        reference_ids: [],
        amount: null,
        currency: null,
        summary:
          "Photo of a chess board showing a mid-game position with white to move.",
      },
      duplicates: [],
    },
  ];

  const SETTINGS = {
    source_dir: "/home/demo/DeepCatalog/inbox",
    categories: [
      "invoice",
      "receipt",
      "contract",
      "letter",
      "tax",
      "medical",
      "id",
      "other",
    ].map((name) => ({ name, folder: `/home/demo/DeepCatalog/archive/${name}` })),
    batch: { poll_interval_seconds: 30 },
    review: { require_approval: true },
    ocr: { mode: "balanced" },
  };

  const ASK = {
    status: "success",
    reply:
      "You received two invoices from Acme Corp this quarter. The larger one is invoice #2024-118 from June 3 over EUR 1,240.00 for consulting services in May (due June 17). A second invoice over EUR 380.00 for on-site support is currently waiting in your review queue and has not been filed yet.",
    sources: [
      {
        document_id: "demo-doc-1",
        filename: "2024-06-03_Invoice_AcmeCorp_EUR1240.pdf",
        doc_type: "invoice",
        doc_date: "2024-06-03",
        snippet:
          "Invoice #2024-118 from Acme Corp for consulting services in May, total EUR 1,240.00 incl. VAT.",
        open_url: "/api/documents/demo-doc-1/file",
        reveal_url: "/api/documents/demo-doc-1/reveal",
      },
      {
        document_id: "demo-doc-10",
        filename: "2024-05-02_Invoice_AcmeCorp_EUR220.pdf",
        doc_type: "invoice",
        doc_date: "2024-05-02",
        snippet: "Invoice #2024-099 from Acme Corp for an after-hours support callout, EUR 220.00.",
        open_url: "/api/documents/demo-doc-10/file",
        reveal_url: "/api/documents/demo-doc-10/reveal",
      },
    ],
    grounded: true,
    evidence: "strong",
    retrieval_count: 2,
    metadata_count: 2,
  };

  const AUTH = {
    status: "success",
    auth_mode: "chatgpt_oauth",
    openai_ready: true,
    chatgpt_email: "demo@deepcatalog.app",
    chatgpt_plan: "plus",
  };

  const OLLAMA = {
    active: false,
    ready: false,
    reachable: true,
    listening: true,
    can_start: false,
    binary: "/usr/local/bin/ollama",
    is_local: true,
    base_url: "http://localhost:11434",
    version: "0.6.0",
    installed_models: ["gemma3:latest", "nomic-embed-text:latest"],
    catalog: {
      chat: [
        { id: "gemma3", label: "Gemma 3 (default)" },
        { id: "llama3.2-vision", label: "Llama 3.2 Vision" },
        { id: "qwen2.5vl", label: "Qwen2.5-VL" },
        { id: "minicpm-v", label: "MiniCPM-V" },
        { id: "llava", label: "LLaVA" },
        { id: "moondream", label: "Moondream (small)" },
      ],
      embed: [
        { id: "nomic-embed-text", label: "nomic-embed-text (768-d, default)" },
        { id: "mxbai-embed-large", label: "mxbai-embed-large (1024-d)" },
        { id: "bge-m3", label: "bge-m3 (1024-d)" },
        { id: "all-minilm", label: "all-minilm (384-d)" },
      ],
    },
    chat_model: "gemma3",
    embedding_model: "nomic-embed-text",
    missing_models: [],
    pull_command: "",
    compute: "cpu",
    compute_label: "CPU",
    size_vram: 0,
    running_models: [],
    error: null,
    install_hint: null,
  };

    // Mutable inbox list so mock uploads can append for screenshot demos.
    let inboxFiles = [
      { name: "scan_20240614_0932.pdf", path: `${SETTINGS.source_dir}/scan_20240614_0932.pdf`, suffix: ".pdf", size_bytes: 482113 },
      { name: "receipt_okt.jpg", path: `${SETTINGS.source_dir}/receipt_okt.jpg`, suffix: ".jpg", size_bytes: 201422 },
    ];

    const ROUTES = [
    [/^\/api\/health/, () => ({
      status: "ok",
      version: "0.0.0-mock",
    })],
    [/^\/api\/diagnostics/, () => ({
      status: "ok",
      version: "0.0.0-mock",
      llm_provider: "openai",
      model: "gpt-5",
      auth: AUTH,
      cloud_disclaimer: { version: "1", accepted: true, accepted_at: "2026-01-01T00:00:00+00:00" },
      usage: {
        requests: 12,
        chat_requests: 10,
        embed_requests: 2,
        prompt_tokens: 8400,
        completion_tokens: 2100,
        total_tokens: 10500,
        last_provider: "openai",
        last_model: "gpt-5",
        last_kind: "chat",
        updated_at: "2026-01-01T00:00:00+00:00",
      },
    })],
    [/^\/api\/auth\/status/, () => ({
      ...AUTH,
      cloud_disclaimer: { version: "1", accepted: true, accepted_at: "2026-01-01T00:00:00+00:00" },
    })],
    [
      /^\/api\/privacy\/cloud-disclaimer/,
      () => ({
        status: "success",
        cloud_disclaimer: { version: "1", accepted: true, accepted_at: "2026-01-01T00:00:00+00:00" },
      }),
    ],
    [/^\/api\/ollama\/status/, () => ({ status: "success", ollama: OLLAMA })],
    [
      /^\/api\/ollama\/enable/,
      () => ({
        status: "success",
        applied: { provider: "ollama", model: "gemma3", embedding_model: "nomic-embed-text" },
        ollama: { ...OLLAMA, active: true, ready: true },
      }),
    ],
    [
      /^\/api\/ollama\/start/,
      () => ({
        status: "success",
        started: true,
        already_running: false,
        method: "ollama serve",
        ollama: { ...OLLAMA, active: true, ready: true, can_start: false },
      }),
    ],
    [
      /^\/api\/ollama\/pull/,
      () => ({ status: "success", model: "gemma3", ollama: { ...OLLAMA, active: true, ready: true } }),
    ],
    [
      /^\/api\/ollama\/ps/,
      () => ({
        status: "success",
        models: [{ name: "gemma3:4b", size: 2800000000, size_vram: 2800000000 }],
      }),
    ],
    [
      /^\/api\/ollama\/unload/,
      () => ({ status: "success", unloaded: ["gemma3:4b"], running_models: [] }),
    ],
    [
      /^\/api\/ollama\/restart/,
      () => ({
        status: "success",
        restarted: true,
        method: "systemctl --user restart ollama.service",
        ollama: { ...OLLAMA, active: true, ready: true },
      }),
    ],
    [
      /^\/api\/process\/cancel/,
      () => ({ status: "success", file_id: "demo-f2", message: "Cancellation requested" }),
    ],
    [
      /^\/api\/process\/retry/,
      () => ({ status: "success", cancelled: true, message: "Cancellation requested for the active file." }),
    ],
    [
      /^\/api\/llm\/provider/,
      () => ({
        status: "success",
        applied: { provider: "openai", model: "gpt-5.6-luna", embedding_model: "text-embedding-3-small" },
      }),
    ],
    [
      /^\/api\/upload$/,
      (_path, options = {}) => {
        const body = options.body;
        let filename = `scan_${Date.now()}.pdf`;
        let size = 120000;
        if (body && typeof FormData !== "undefined" && body instanceof FormData) {
          const file = body.get("file");
          if (file && typeof file === "object" && "name" in file) {
            filename = file.name || filename;
            size = Number(file.size) || size;
          }
        }
        const suffix = filename.includes(".") ? `.${filename.split(".").pop().toLowerCase()}` : ".pdf";
        const entry = {
          name: filename,
          path: `${SETTINGS.source_dir}/${filename}`,
          suffix,
          size_bytes: size,
        };
        inboxFiles = [...inboxFiles, entry];
        return {
          status: "success",
          path: entry.path,
          filename: entry.name,
          source_dir: SETTINGS.source_dir,
          bytes: size,
          media: { kind: suffix === ".pdf" ? "pdf" : "image" },
        };
      },
    ],
    [
      /^\/api\/inbox$/,
      (_path, options = {}) => {
        if (String(options.method || "GET").toUpperCase() === "DELETE") {
          const removed = inboxFiles.length;
          inboxFiles = [];
          return { status: "success", removed_count: removed };
        }
        return {
          status: "success",
          count: inboxFiles.length,
          source_dir: SETTINGS.source_dir,
          files: inboxFiles,
        };
      },
    ],
    [/^\/api\/documents\?|^\/api\/documents$/, (path) => mockFilterDocuments(path)],
    [
      /^\/api\/documents\/([^/?#]+)\/reveal$/,
      () => ({ status: "success", path: "/home/demo/DeepCatalog/archive", opened: "explorer" }),
    ],
    [
      /^\/api\/documents\/([^/?#]+)$/,
      (path) => {
        const id = decodeURIComponent(path.split("/")[3] || "");
        const document = DOCS.find((d) => d.id === id);
        if (!document) throw new Error("Document not found");
        return { status: "success", document };
      },
    ],
    [/^\/api\/reviews$/, () => ({ status: "success", count: REVIEWS.length, reviews: REVIEWS })],
    [/^\/api\/autostart\/status/, () => ({
      status: "success",
      autostart: {
        supported: true,
        enabled: false,
        active: false,
        url: "http://127.0.0.1:8080",
        unit_path: "/home/demo/.config/systemd/user/deepcatalog.service",
        linger: false,
        error: null,
      },
    })],
    [/^\/api\/autostart$/, () => ({
      status: "success",
      autostart: { supported: true, enabled: true, active: true, url: "http://127.0.0.1:8080" },
    })],
    [/^\/api\/settings$/, () => ({ status: "success", settings: SETTINGS })],
    // Intentionally do NOT mock /api/update/status — Software update must show
    // the real installed version vs GitHub, even in mockup mode.
    [/^\/api\/ask$/, () => ASK],
  ];

  async function respond(path, options = {}) {
    // Software update / version must always reflect the real install + GitHub.
    if (/^\/api\/update(\/|\?|$)/.test(path)) {
      const headers = new Headers(options.headers || {});
      headers.set("X-Requested-With", "DeepCatalog");
      const res = await fetch(path, { ...options, headers });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error((data && data.detail) || res.statusText || "Request failed");
      }
      return data;
    }
    for (const [pattern, handler] of ROUTES) {
      if (pattern.test(path)) {
        // Small delay so spinners/status lines render naturally.
        await new Promise((r) => setTimeout(r, 120));
        return handler(path, options);
      }
    }
    throw new Error("Mockup mode is on — this action is disabled. Turn it off in Settings.");
  }

  function setEnabled(value) {
    try {
      localStorage.setItem("dc-mock", value ? "1" : "0");
    } catch (_err) {
      // private mode — toggle just won't persist
    }
  }

  window.DC_MOCK = {
    enabled,
    respond,
    setEnabled,
    ask: ASK,
    // Frozen mid-run pipeline scene for the inbox workflow panel.
    workflow: {
      activeFilename: "scan_20240614_0932.pdf",
      activeFileId: "demo-f2",
      jobTotal: 3,
      stepStatus: {
        read: "done",
        ai_ocr: "done",
        extract: "running",
        name: "wait",
        review: "wait",
        file: "wait",
        index: "wait",
      },
      stepDetail: {
        read: "2 pages · 1840 chars in text layer",
        ai_ocr: "2 pages · 3120 chars",
        extract: "Extracting type, date, subject, parties — can take a while",
      },
      queue: [
        { file_id: "demo-f1", filename: "invoice_mai.pdf", status: "done", stepLabel: "Done", path: "/home/demo/DeepCatalog/inbox/invoice_mai.pdf" },
        { file_id: "demo-f2", filename: "scan_20240614_0932.pdf", status: "running", stepLabel: "Find details", path: "/home/demo/DeepCatalog/inbox/scan_20240614_0932.pdf" },
        { file_id: "demo-f3", filename: "receipt_okt.jpg", status: "queued", stepLabel: null, path: "/home/demo/DeepCatalog/inbox/receipt_okt.jpg" },
        { file_id: "demo-f4", filename: "contract_scan.pdf", status: "error", stepLabel: "Failed", path: "/home/demo/DeepCatalog/inbox/contract_scan.pdf" },
        { file_id: "demo-f5", filename: "letter_bank.pdf", status: "review", stepLabel: "Needs review", path: "/home/demo/DeepCatalog/inbox/letter_bank.pdf" },
      ],
    },
  };
})();
