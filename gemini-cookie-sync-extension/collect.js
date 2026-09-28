const DEFAULT_BASE_URL = "http://127.0.0.1:8000";
const BOOTSTRAP_TIMEOUT_MS = 6000;

// ---------- Bootstrap: server là nguồn sự thật cho base_url + API key ----------
// Server bắt buộc phải tự tìm được API key, nhưng extension mới chính là thứ
// đẩy cookie lên. Để thoát vòng lặp đó, extension hỏi server trước:
//   GET <base_url>/v1/auth/bootstrap  ->  { base_url, api_key, auto_sync }
// Nhờ vậy bản clone profile mới không cần bấm popup "Sync" một lần nào cả.
async function fetchBootstrap(baseUrl) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), BOOTSTRAP_TIMEOUT_MS);
  try {
    const resp = await fetch(`${baseUrl}/v1/auth/bootstrap`, {
      signal: ctrl.signal,
      cache: "no-store"
    });
    if (!resp.ok) return null;
    return await resp.json();
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

async function readSettings() {
  return chrome.storage.local.get({
    syncBaseUrl: DEFAULT_BASE_URL,
    syncApiKey: "",
    autoSyncEnabled: true,
    lastAutoSyncSuccess: 0,
    lastAutoSyncError: "",
    lastAutoSyncErrorAt: 0,
    autoSyncErrorCount: 0,
    autoSyncPausedUntil: 0
  });
}

async function ensureSettings() {
  const settings = await readSettings();
  const baseUrl = (settings.syncBaseUrl || DEFAULT_BASE_URL).replace(/\/+$/, "");
  const cfg = await fetchBootstrap(baseUrl);
  if (!cfg) return settings;

  const patch = {};
  if (cfg.base_url) patch.syncBaseUrl = String(cfg.base_url).replace(/\/+$/, "");
  // Luôn nhận key từ server: profile clone mang theo key cũ, và server có thể
  // đã đổi key trong config.json. Nhờ vậy lỗi 401 tự biến mất thay vì phải xoá
  // storage bằng tay.
  if (cfg.api_key) patch.syncApiKey = String(cfg.api_key);
  if (Object.keys(patch).length === 0) return settings;
  await chrome.storage.local.set(patch);
  return { ...settings, ...patch };
}

// Shared cookie-collection library for the Gemini Cookie Sync extension.
// Loaded as a classic script by both popup.html and background.js (importScripts),
// so all declarations below become globals.

const CORE_REQUIRED = ["SAPISID"];
const SESSION_ALTERNATIVES = ["__Secure-1PSID", "__Secure-3PSID", "SID"];
const LEGACY_COOKIES = ["SID", "HSID", "SSID", "APISID", "SAPISID"];

const EXPORT_ORDER = [
  "SID",
  "HSID",
  "SSID",
  "APISID",
  "SAPISID",
  "LSID",
  "OSID",
  "SIDCC",
  "AEC",
  "NID",
  "COMPASS",
  "__Secure-1PAPISID",
  "__Secure-1PSID",
  "__Secure-1PSIDTS",
  "__Secure-1PSIDCC",
  "__Secure-1PSIDRTS",
  "__Secure-3PAPISID",
  "__Secure-3PSID",
  "__Secure-3PSIDTS",
  "__Secure-3PSIDCC",
  "__Secure-3PSIDRTS",
  "__Secure-OSID",
  "__Host-1PLSID",
  "__Host-3PLSID"
];

const LOOKUP_URLS = [
  "https://gemini.google.com/app",
  "https://accounts.google.com/",
  "https://www.google.com/",
  "https://google.com/"
];

function normalizeDomain(domain = "") {
  return domain.replace(/^\./, "").toLowerCase();
}

function isGoogleCookie(cookie) {
  const domain = normalizeDomain(cookie.domain);
  return domain === "google.com" || domain.endsWith(".google.com");
}

function cookieKey(cookie) {
  const partition = cookie.partitionKey ? JSON.stringify(cookie.partitionKey) : "";
  return [
    cookie.storeId || "",
    cookie.name,
    cookie.domain,
    cookie.path,
    partition
  ].join("|");
}

function scoreCookie(cookie) {
  const domain = (cookie.domain || "").toLowerCase();
  let score = 0;

  if (domain === ".google.com") score += 120;
  else if (domain === "google.com") score += 110;
  else if (domain === ".gemini.google.com") score += 100;
  else if (domain === "gemini.google.com") score += 95;
  else if (domain === ".accounts.google.com") score += 80;
  else if (domain === "accounts.google.com") score += 75;
  else if (domain.endsWith(".google.com")) score += 40;

  if (cookie.path === "/") score += 10;
  if (cookie.secure) score += 3;
  if (cookie.httpOnly) score += 2;
  if (!cookie.partitionKey) score += 2;
  if (!cookie.session) score += 1;

  return score;
}

async function readGoogleCookies() {
  const stores = await chrome.cookies.getAllCookieStores();
  const deduped = new Map();

  for (const store of stores) {
    const queries = [
      chrome.cookies.getAll({ storeId: store.id }),
      ...LOOKUP_URLS.map((url) => chrome.cookies.getAll({ storeId: store.id, url }))
    ];

    const results = await Promise.allSettled(queries);

    for (const result of results) {
      if (result.status !== "fulfilled") continue;
      for (const cookie of result.value) {
        if (!isGoogleCookie(cookie) || !cookie.value) continue;
        deduped.set(cookieKey(cookie), cookie);
      }
    }
  }

  return [...deduped.values()];
}

function selectBestCookies(cookies) {
  const selected = new Map();

  for (const name of EXPORT_ORDER) {
    const candidates = cookies
      .filter((cookie) => cookie.name === name && cookie.value)
      .sort((a, b) => scoreCookie(b) - scoreCookie(a));

    if (candidates.length > 0) selected.set(name, candidates[0]);
  }

  return selected;
}

function validateSelection(selected) {
  const missingCore = CORE_REQUIRED.filter((name) => !selected.has(name));
  const sessionCookie = SESSION_ALTERNATIVES.find((name) => selected.has(name));
  return {
    missingCore,
    sessionCookie,
    valid: missingCore.length === 0 && Boolean(sessionCookie)
  };
}

function listPresent(selected, names) {
  return names.filter((name) => selected.has(name));
}

function summarizeAvailable(cookies) {
  return [...new Set(cookies
    .map((cookie) => cookie.name)
    .filter((name) => /SID|APISID|LSID|OSID|COMPASS/.test(name)))]
    .sort();
}

function getActiveGeminiAccount(tabs) {
  for (const tab of tabs) {
    try {
      const url = new URL(tab.url || "");
      if (url.hostname !== "gemini.google.com") continue;
      const match = url.pathname.match(/^\/u\/(\d+)(?:\/|$)/);
      return match ? match[1] : null;
    } catch {
      // Ignore malformed or unavailable URLs.
    }
  }
  return null;
}

function chooseGeminiTab(tabs) {
  return tabs.find((tab) => tab.active) || tabs[0] || null;
}

async function readGeminiPageMetadata(tabs) {
  const tab = chooseGeminiTab(tabs);
  if (!tab?.id) {
    return {
      xsrfToken: null,
      geminiBl: null,
      source: null,
      error: "Open gemini.google.com in a tab, sign in, then refresh the page."
    };
  }

  try {
    const result = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: "MAIN",
      func: () => {
        const wiz = globalThis.WIZ_global_data || {};
        const html = document.documentElement?.innerHTML || "";

        const decode = (value) => {
          if (!value) return null;
          try {
            return JSON.parse(`"${value.replace(/"/g, '\\"')}"`);
          } catch {
            return value
              .replace(/\\u003d/gi, "=")
              .replace(/\\u0026/gi, "&")
              .replace(/\\u003c/gi, "<")
              .replace(/\\u003e/gi, ">");
          }
        };

        const regexValue = (name) => {
          const patterns = [
            new RegExp(`"${name}"\\s*:\\s*"([^"\\n]+)"`),
            new RegExp(`\\\\"${name}\\\\"\\s*:\\s*\\\\"([^"\\n]+)\\\\"`)
          ];
          for (const pattern of patterns) {
            const match = html.match(pattern);
            if (match?.[1]) return decode(match[1]);
          }
          return null;
        };

        let resourceBl = null;
        try {
          const resources = performance.getEntriesByType("resource");
          for (const entry of resources) {
            if (!entry?.name?.includes("gemini.google.com")) continue;
            const url = new URL(entry.name);
            const bl = url.searchParams.get("bl");
            if (bl) {
              resourceBl = bl;
              break;
            }
          }
        } catch {
          // Ignore performance API failures.
        }

        const xsrfToken = wiz.SNlM0e || regexValue("SNlM0e");
        const geminiBl = wiz.cfb2h || resourceBl || regexValue("cfb2h");

        let source = null;
        if (wiz.SNlM0e || wiz.cfb2h) source = "WIZ_global_data";
        else if (resourceBl) source = "performance";
        else if (xsrfToken || geminiBl) source = "page-html";

        return {
          xsrfToken: xsrfToken || null,
          geminiBl: geminiBl || null,
          source,
          url: location.href
        };
      }
    });

    return result?.[0]?.result || {
      xsrfToken: null,
      geminiBl: null,
      source: null,
      error: "Failed to read Gemini page data."
    };
  } catch (error) {
    return {
      xsrfToken: null,
      geminiBl: null,
      source: null,
      error: error?.message || String(error)
    };
  }
}

async function buildInspection() {
  const [cookies, tabs] = await Promise.all([
    readGoogleCookies(),
    chrome.tabs.query({ url: "https://gemini.google.com/*" })
  ]);

  const selected = selectBestCookies(cookies);
  const validation = validateSelection(selected);
  const legacyPresent = listPresent(selected, LEGACY_COOKIES);
  const legacyMissing = LEGACY_COOKIES.filter((name) => !selected.has(name));
  const available = summarizeAvailable(cookies);
  const authUser = getActiveGeminiAccount(tabs);
  const pageMetadata = await readGeminiPageMetadata(tabs);

  return {
    cookies,
    selected,
    validation,
    legacyPresent,
    legacyMissing,
    available,
    authUser,
    pageMetadata
  };
}

function inspectionMessage(info) {
  const {
    cookies,
    selected,
    validation,
    legacyPresent,
    legacyMissing,
    available,
    authUser,
    pageMetadata
  } = info;

  const lines = [
    `Found ${cookies.length} cookie(s) across Google domains.`,
    "",
    `SAPISID: ${selected.has("SAPISID") ? "present" : "missing"}`,
    `Session cookie: ${validation.sessionCookie || "missing"}`,
    `XSRF / SNlM0e: ${pageMetadata.xsrfToken ? "present" : "missing"}`,
    `gemini_bl / cfb2h: ${pageMetadata.geminiBl ? "present" : "missing"}`,
    `auth_user: ${authUser ?? "default account"}`,
    `Page data source: ${pageMetadata.source || "unavailable"}`,
    "",
    `Legacy present: ${legacyPresent.join(", ") || "none"}`,
    `Legacy not visible to the extension: ${legacyMissing.join(", ") || "none"}`,
    "",
    `Available for export: ${available.join(", ") || "none"}`
  ];

  if (pageMetadata.error) {
    lines.push("", `Page note: ${pageMetadata.error}`);
  }

  if (validation.valid && pageMetadata.xsrfToken) {
    lines.push("", "Session and XSRF are ready to export / sync.");
  } else if (validation.valid) {
    lines.push("", "Cookies are ready, but XSRF is missing. Open Gemini, refresh the page, then inspect again.");
  } else {
    lines.push("", "Session is incomplete: SAPISID and one of __Secure-1PSID, __Secure-3PSID, or SID are required.");
  }

  return lines.join("\n");
}

function buildCookieString(info) {
  return EXPORT_ORDER
    .filter((name) => info.selected.has(name))
    .map((name) => `${name}=${info.selected.get(name).value}`)
    .join("; ");
}

function buildAuthPayload(info) {
  return {
    cookie: buildCookieString(info),
    sapisid: info.selected.get("SAPISID").value,
    auth_user: info.authUser,
    xsrf_token: info.pageMetadata.xsrfToken,
    gemini_bl: info.pageMetadata.geminiBl
  };
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function snapshotKeyOf(info) {
  return [buildCookieString(info), info.pageMetadata?.xsrfToken || ""].join("|");
}

function snapshotStable(a, b) {
  return Boolean(
    a &&
      b &&
      a.validation?.valid &&
      b.validation?.valid &&
      a.pageMetadata?.xsrfToken &&
      b.pageMetadata?.xsrfToken &&
      snapshotKeyOf(a) === snapshotKeyOf(b)
  );
}

// Captures the session twice and only returns a consistent snapshot. Chrome
// keeps rotating __Secure-1PSIDTS, so a single read can catch a half-finished
// token swap that Google would reject.
async function captureStableInspection({ attempts = 3, settleMs = 3000 } = {}) {
  let last = null;
  for (let i = 0; i < attempts; i++) {
    const current = await buildInspection();
    if (!current.validation?.valid || !current.pageMetadata?.xsrfToken) {
      await delay(settleMs);
      continue;
    }
    if (last && snapshotStable(last, current)) {
      return {
        info: current,
        attempts: i + 1,
        stable: true
      };
    }
    last = current;
    await delay(settleMs);
  }
  return { info: last, attempts, stable: false };
}

async function pushToServer(info, { baseUrl, apiKey } = {}) {
  const base = (baseUrl || DEFAULT_BASE_URL).replace(/\/+$/, "");
  const resp = await fetch(`${base}/v1/auth/sync`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${apiKey}`
    },
    body: JSON.stringify(buildAuthPayload(info))
  });
  if (!resp.ok) {
    let detail = "";
    try {
      detail = (await resp.json())?.error?.message || "";
    } catch {
      // Ignore non-JSON error bodies.
    }
    throw new Error(`Server returned HTTP ${resp.status}${detail ? `: ${detail}` : ""}`);
  }
  return resp.json();
}