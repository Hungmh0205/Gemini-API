importScripts("collect.js"); // Shared collection helpers: DEFAULT_BASE_URL, captureStableInspection, buildAuthPayload, isGoogleCookie, ...

const AUTO_SYNC_ALARM = "gemini-auto-sync";
const GEMINI_APP_URL = "https://gemini.google.com/app";
const SYNC_MAX_AGE_MS = 3 * 60 * 1000; // Push at least this often to keep a fresh token.
const BACKOFF_MS = 15 * 60 * 1000;
const ERROR_PAUSE_THRESHOLD = 5;
const TAB_SETTLE_MS = 8000;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// readSettings / ensureSettings / fetchBootstrap nằm trong collect.js
// (dùng chung với popup).

async function waitTabLoaded(tabId, tries = 45) {
  for (let i = 0; i < tries; i++) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) return false;
    if (tab.status === "complete") return true;
    await sleep(1000);
  }
  return false;
}

// Hidden tab lifecycle: open an inactive Gemini tab (no window focus), let the
// page settle, collect, push, then close it again.
async function withGeminiTab(run) {
  const existing = await chrome.tabs.query({ url: "https://gemini.google.com/*" });
  const reuse = existing.find((tab) => tab.id !== undefined) || null;

  if (reuse) {
    // Tab do launcher vừa mở có thể còn đang tải; chờ nạp xong rồi mới đọc
    // WIZ_global_data, nếu không sẽ bắt được snapshot rỗng.
    await waitTabLoaded(reuse.id, 30);
    return run(reuse, false);
  }

  const created = await chrome.tabs.create({ url: GEMINI_APP_URL, active: false });
  if (!(await waitTabLoaded(created.id))) {
    await chrome.tabs.remove(created.id).catch(() => {});
    return null;
  }
  // Let WIZ_global_data / cfb2h populate and the initial token rotate settle.
  await sleep(TAB_SETTLE_MS);
  try {
    return await run(created, true);
  } finally {
    await chrome.tabs.remove(created.id).catch(() => {});
  }
}

async function checkServerHealth(baseUrl) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 8000);
  try {
    const resp = await fetch(`${baseUrl}/health`, { signal: ctrl.signal });
    if (!resp.ok) return { reachable: true, healthy: false };
    const data = await resp.json().catch(() => ({}));
    return { reachable: true, healthy: data.status === "ok" };
  } catch {
    return { reachable: false, healthy: false };
  } finally {
    clearTimeout(timer);
  }
}

async function pushOnce(baseUrl, apiKey) {
  const captured = await captureStableInspection({ attempts: 3, settleMs: 3000 });
  if (!captured.stable || !captured.info) {
    return { ok: false, reason: "unstable-snapshot", retry: false };
  }
  let resp;
  try {
    resp = await fetch(`${baseUrl}/v1/auth/sync`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${apiKey}`
      },
      body: JSON.stringify(buildAuthPayload(captured.info))
    });
  } catch {
    return { ok: false, reason: "network-error", retry: false };
  }
  if (resp.status === 401) {
    // Key trong storage lệch với server -> lấy key mới rồi thử lại 1 lần.
    const fresh = await ensureSettings();
    if (fresh.syncApiKey && fresh.syncApiKey !== apiKey) {
      return pushOnce(baseUrl, fresh.syncApiKey);
    }
    return { ok: false, reason: "HTTP 401 (api key mismatch)", retry: false };
  }
  if (!resp.ok) {
    const retry = resp.status >= 500;
    return { ok: false, reason: `HTTP ${resp.status}`, retry };
  }
  return { ok: true, retry: false };
}

async function pushCookies(baseUrl, apiKey, _tab) {
  return pushOnce(baseUrl, apiKey);
}

async function recordResult(ok, reason) {
  const settings = await readSettings();
  if (ok) {
    await chrome.storage.local.set({
      lastAutoSyncSuccess: Date.now(),
      autoSyncErrorCount: 0,
      lastAutoSyncError: "",
      autoSyncPausedUntil: 0
    });
    return;
  }
  const count = (settings.autoSyncErrorCount || 0) + 1;
  const patch = {
    lastAutoSyncError: reason,
    lastAutoSyncErrorAt: Date.now(),
    autoSyncErrorCount: count
  };
  if (count >= ERROR_PAUSE_THRESHOLD) {
    patch.autoSyncPausedUntil = Date.now() + BACKOFF_MS;
  }
  await chrome.storage.local.set(patch);
}

async function tryAutoSync() {
  if (!chrome.tabs || !chrome.cookies || !chrome.scripting) return;

  const initial = await readSettings();
  if (!initial.autoSyncEnabled) return; // Người dùng tắt trong popup thì không tự bật lại.
  if ((initial.autoSyncPausedUntil || 0) > Date.now()) return;

  // Luôn hỏi server trước: vừa để lấy key, vừa để biết server còn sống không.
  const settings = await ensureSettings();
  if (!settings.syncApiKey) return; // Server không cấp key (bootstrap tắt) -> cần bấm popup.

  const baseUrl = (settings.syncBaseUrl || DEFAULT_BASE_URL).replace(/\/+$/, "");

  const health = await checkServerHealth(baseUrl);
  const lastSuccess = settings.lastAutoSyncSuccess || 0;
  const cookieAge = Date.now() - lastSuccess;
  const needSync =
    !health.reachable || !health.healthy || cookieAge > SYNC_MAX_AGE_MS;
  if (!needSync) return;

  const result = await withGeminiTab((tab) => pushCookies(baseUrl, settings.syncApiKey, tab));

  if (result === null) {
    // Could not open/load a Gemini tab; retry on the next alarm.
    return;
  }
  if (result.retry) {
    // Server đang verify cookie vừa nhận (Google xoay token thoáng qua) —
    // không tính lỗi vào ngưỡng pause; đẩy lại sau 20 giây.
    scheduleEarlySync(20000);
    return;
  }
  await recordResult(result.ok, result.reason);
}

async function ensureAutoSyncAlarm() {
  const alarm = await chrome.alarms.get(AUTO_SYNC_ALARM).catch(() => null);
  if (!alarm) {
    await chrome.alarms.create(AUTO_SYNC_ALARM, { periodInMinutes: 1 }).catch(() => {});
  }
  if (!chrome.runtime.onStartup.hasListeners()) {
    chrome.runtime.onStartup.addListener(() => {
      // Lấy key ngay khi Chrome vừa mở, rồi sync sau 15s cho Gemini kịp tải.
      void ensureSettings().then(() => setTimeout(() => void tryAutoSync(), 15000));
    });
  }
}

chrome.runtime.onInstalled.addListener((details) => {
  void ensureAutoSyncAlarm();
  // Profile clone mới luôn phát sinh "install": đây là đường tự động chính.
  if (details.reason === "install" || details.reason === "chrome_update") {
    void ensureSettings().then(() => setTimeout(() => void tryAutoSync(), 15000));
  }
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === AUTO_SYNC_ALARM) {
    void tryAutoSync();
  }
});

chrome.runtime.onMessage.addListener((message) => {
  if (message && message.type === "reset-auto-sync") {
    void chrome.storage.local.set({
      autoSyncErrorCount: 0,
      autoSyncPausedUntil: 0
    });
  }
  if (message && message.type === "refresh-settings") {
    void ensureSettings().then((s) => {
      chrome.runtime.sendMessage({ type: "settings-refreshed", settings: s }).catch(() => {});
    });
  }
});

// ---------- Đẩy nhanh khi sự kiện xảy ra (debounced) ----------
let earlySyncTimer = null;

function scheduleEarlySync(delayMs) {
  if (earlySyncTimer !== null) return;
  earlySyncTimer = setTimeout(() => {
    earlySyncTimer = null;
    void tryAutoSync();
  }, delayMs);
}

// Google xoay token (__Secure-1PSIDTS...) ngay khi ta đang dùng Gemini —
// đẩy sớm để server khỏi bao giờ giữ cookie cũ.
chrome.cookies.onChanged.addListener((changeInfo) => {
  const cookie = changeInfo && changeInfo.cookie;
  if (!cookie || !isGoogleCookie(cookie)) return;
  scheduleEarlySync(20000);
});

// Sau khi tab Gemini tải xong cũng là lúc WIZ_global_data / cfb2h đã sẵn sàng.
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status !== "complete") return;
  if (!tab || !tab.url || !tab.url.includes("https://gemini.google.com")) return;
  scheduleEarlySync(15000);
});

void ensureSettings();
void ensureAutoSyncAlarm();
