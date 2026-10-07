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

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message && message.type === "gemini-job-done") {
    void (async () => {
      const jobId = message.jobId || activeJobId;
      await logFilesync("job-done", `${jobId} files=${(message.files || []).length}`);
      await postJobResult(jobId, {
        text: message.text || "",
        files: message.files?.length ? message.files : activeJobFiles,
        cid: message.cid || activeJobCid || ""
      });
      activeJobId = "";
      activeJobFiles = [];
      sendResponse({ ok: true });
    })();
    return true; // giữ kênh mở để sendResponse chạy được bất đồng bộ
  }

  if (message && message.type === "gemini-job-failed") {
    void (async () => {
      await logFilesync("job-failed", `${message.jobId || activeJobId}: ${message.error}`);
      await postJobResult(message.jobId || activeJobId, {
        error: message.error || "loi khong ro"
      });
      activeJobId = "";
      activeJobFiles = [];
      sendResponse({ ok: true });
    })();
    return true;
  }

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
  if (message && message.type === "filesync:context") {
    lastFileContext = { name: message.name || "", cid: message.cid || "", mime: message.mime || "" };
    // Vừa phát hiện chip -> thử bấm nút "Tải xuống" trong preview bằng cách tiêm
    // vào MỌI frame (Drive viewer là iframe khác origin). tryClickDownload tự thử
    // lại nhiều nhịp nên không cần chờ preview mở xong.
    if (sender?.tab?.id != null) {
      setTimeout(() => void tryClickDownload(sender.tab.id, 0), 1200);
    }
  }
  if (message && message.type === "filesync:url") {
    const url = message.url || "";
    if (url.includes(FILE_DOWNLOAD_MATCH) || /googleusercontent\.com\/gg-dl\//i.test(url)) {
      if (!pushedUrls.has(url)) {
        pushedUrls.add(url);
        void fetchAndPushUrl(url);
      }
    }
  }
  if (message && message.type === "filesync:clickat") {
    // Click tổng hợp (isTrusted=false) bị Drive viewer bỏ qua; dùng CDP
    // Input.dispatchMouseEvent để tạo click trusted, đủ điều kiện cho download.
    const tabId = sender?.tab?.id;
    void logFilesync("clickat-recv", `tab=${tabId} x=${message.x} y=${message.y}`);
    if (typeof tabId === "number") {
      void trustedClick(tabId, message.x, message.y);
    }
  }
  });

// ---------- Bắt file Gemini tải về ----------
// Bytes KHÔNG đến từ `batchexecute?rpcids=ESY5D` như tưởng. Bấm "Download" khiến
// Chrome tạo một download tới `contribution.usercontent.google.com/download` —
// origin khác, đi qua download manager chứ không phải XHR, nên hook `fetch` trong
// trang không thấy gì. Service worker là nơi duy nhất lấy được bytes: `fetch` của
// nó mang cookie phiên thật (thử bằng urllib + cookie thủ công thì 403).
const FILE_DOWNLOAD_MATCH = "contribution.usercontent.google.com/download";

const pendingFiles = new Map();

// Một URL chỉ đẩy một lần; trang hay render lại preview và báo lại cùng URL.
const pushedUrls = new Set();

// Chip vừa xuất hiện; gán cho download kế tiếp để server biết file thuộc
// conversation nào. Không có nó thì server không nối được file vào response.
let lastFileContext = null;

async function logFilesync(stage, detail) {
  try {
    const s = await ensureSettings();
    const base = (s.syncBaseUrl || DEFAULT_BASE_URL).replace(/\/+$/, "");
    await fetch(`${base}/v1/files/debug`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(s.syncApiKey ? { Authorization: `Bearer ${s.syncApiKey}` } : {})
      },
      body: JSON.stringify({ stage, detail })
    });
  } catch {
    // Báo lỗi thất bại không được làm hỏng luồng sync.
  }
}

async function pushDownloadedFile(downloadId) {
  const info = pendingFiles.get(downloadId);
  if (!info) return;
  pendingFiles.delete(downloadId);
  try {
    const s = await ensureSettings();
    const base = (s.syncBaseUrl || DEFAULT_BASE_URL).replace(/\/+$/, "");
    const resp = await fetch(info.url, { credentials: "include" });
    if (!resp.ok) {
      await logFilesync("dl-fetch-fail", `${resp.status} ${info.name}`);
      return;
    }
    const buf = await resp.arrayBuffer();
    if (!buf.byteLength) {
      await logFilesync("dl-empty", info.name);
      return;
    }
    const ctx = lastFileContext || {};
    const qs = new URLSearchParams({
      key: `${ctx.cid || "gemini"}|${info.name || "file"}|0`,
      name: info.name || "",
      mime: info.mime || "",
      cid: ctx.cid || ""
    });
    const up = await fetch(`${base}/v1/files/upload?${qs}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        ...(s.syncApiKey ? { Authorization: `Bearer ${s.syncApiKey}` } : {})
      },
      body: buf
    });
    await logFilesync("dl-upload", `${up.status} ${info.name} ${buf.byteLength}B key=${qs.get("key")}`);
  } catch (e) {
    await logFilesync("dl-error", `${info.name}: ${e}`);
  }
}

async function fetchAndPushUrl(url) {
  const name = (() => {
    try {
      return new URL(url).searchParams.get("filename") || "";
    } catch {
      return "";
    }
  })();
  // URL download thuộc origin Drive nên không tự mang cid. Dùng cid của job mà
  // service worker vừa tự mở tab — đó mới là hội thoại cần file.
  const cid = activeJobCid || lastFileContext?.cid || "";
  try {
    const s = await ensureSettings();
    const base = (s.syncBaseUrl || DEFAULT_BASE_URL).replace(/\/+$/, "");
    const resp = await fetch(url, { credentials: "include" });
    if (!resp.ok) {
      await logFilesync("url-fetch-fail", `${resp.status} ${name}`);
      return;
    }
    const buf = await resp.arrayBuffer();
    if (!buf.byteLength) {
      await logFilesync("url-empty", name);
      return;
    }
    const ctx = lastFileContext || {};
    const qs = new URLSearchParams({
      key: `${cid || ctx.cid || "gemini"}|${name || "file"}|0`,
      name,
      mime: ctx.mime || "",
      cid
    });
    const up = await fetch(`${base}/v1/files/upload?${qs}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        ...(s.syncApiKey ? { Authorization: `Bearer ${s.syncApiKey}` } : {})
      },
      body: buf
    });
    await logFilesync("url-upload", `${up.status} ${name} ${buf.byteLength}B key=${qs.get("key")}`);
    // Gắn vào job đang chạy để server đưa file vào response của câu hỏi đó.
    if (up.ok && activeJobId) {
      try {
        const meta = await up.json();
        activeJobFiles.push({
          id: meta.id,
          url: meta.url,
          filename: name || meta.filename,
          mime_type: meta.mime_type,
          size: meta.size
        });
      } catch {
        /* meta không đọc được thì bỏ, response vẫn trả được text */
      }
    }
  } catch (e) {
    await logFilesync("url-error", `${name}: ${e}`);
  }
}

// Bấm nút bằng CDP để tạo sự kiện trusted (dot.click() tổng hợp bị Drive bỏ qua).
async function trustedClick(tabId, x, y) {
  const target = { tabId };
  try {
    await chrome.debugger.attach(target, "1.3");
  } catch (e) {
    await logFilesync("dbg-attach-fail", String(e && e.message ? e.message : e));
    return;
  }
  const px = Math.max(1, Math.round(x));
  const py = Math.max(1, Math.round(y));
  const base = { x: px, y: py, button: "left", clickCount: 1 };
  try {
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", { type: "mouseMoved", x: px, y: py });
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", { type: "mousePressed", ...base });
    await new Promise((r) => setTimeout(r, 60));
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", { type: "mouseReleased", ...base });
    await logFilesync("dbg-click", `(${px},${py})`);
  } catch (e) {
    await logFilesync("dbg-click-err", String(e && e.message ? e.message : e));
  } finally {
    setTimeout(() => { chrome.debugger.detach(target).catch(() => {}); }, 4000);
  }
}

// ---------- Mở hội thoại để lấy file ----------
// Server tự sinh câu trả lời qua API nên không có tab nào mở hội thoại đó, mà file
// chỉ tải được từ web UI. Server đăng ký cid ở `/v1/files/jobs`; extension thấy
// thì mở `gemini.google.com/app/<cid>`, rồi tự bấm nút tải trong Drive viewer.
const CHAT_JOB_ALARM = "gemini-chat-jobs";
const claimedJobs = new Set();   // job_id da giao cho content script
const doneJobs = new Set();      // job_id da bao ket qua len server

// Job dang chay: moi download phat sinh trong luc nay se gan vao job nay.
let activeJobId = "";
let activeJobCid = "";
let activeJobFiles = [];

async function settingsOrNull() {
  try {
    return await ensureSettings();
  } catch {
    return null;
  }
}

async function postJobResult(jobId, payload) {
  const s = await settingsOrNull();
  if (!s) return;
  const base = (s.syncBaseUrl || DEFAULT_BASE_URL).replace(/\/+$/, "");
  try {
    await fetch(`${base}/v1/chat/jobs/${jobId}/result`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(s.syncApiKey ? { Authorization: `Bearer ${s.syncApiKey}` } : {})
      },
      body: JSON.stringify(payload)
    });
    doneJobs.add(jobId);
  } catch (e) {
    await logFilesync("job-post-fail", `${jobId}: ${e}`);
  }
}

// Dua tab ve man hinh chat moi (/app) truoc moi job de cat ngu canh hoi thoai cu.
async function openFreshChat(tabId) {
  let path = "";
  try {
    const t = await chrome.tabs.get(tabId);
    try {
      path = new URL(t.url || "").pathname;
    } catch {
      path = "";
    }
  } catch {
    path = "";
  }
  // Dang o dung man hinh chat moi (/app, chua co id hoi thoai) thi khoi reload.
  if (/^\/app\/?$/.test(path)) return;
  try {
    await chrome.tabs.update(tabId, { url: GEMINI_APP_URL, active: true });
  } catch {
    return;
  }
  // Nghi 800ms de khong doc nham status "complete" cua trang cu truoc khi chuyen trang.
  await new Promise((r) => setTimeout(r, 800));
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    try {
      const t = await chrome.tabs.get(tabId);
      if (t.status === "complete") break;
    } catch {
      // tab dang chuyen trang
    }
    await new Promise((r) => setTimeout(r, 400));
  }
  // Cho content script tiem lai sau khi trang render.
  await new Promise((r) => setTimeout(r, 500));
}

// Gui job va thu lai khi content script chua san sang. Chi thu lai khi sendMessage
// nem loi (job CHUA duoc nhan) nen khong chay trung.
async function sendJobWithRetry(tabId, job, attempt = 0) {
  try {
    await chrome.tabs.sendMessage(tabId, {
      type: "gemini-run",
      jobId: job.job_id,
      prompt: job.prompt,
      model: job.model
    });
  } catch (e) {
    if (attempt >= 15) {
      await postJobResult(job.job_id, { error: `Khong gui duoc prompt: ${e}` });
      return;
    }
    await new Promise((r) => setTimeout(r, 1000));
    await sendJobWithRetry(tabId, job, attempt + 1);
  }
}

async function ensureGeminiTab() {
  const existing = await chrome.tabs.query({ url: "https://gemini.google.com/app*" });
  if (existing.length) {
    await chrome.tabs.update(existing[0].id, { active: true });
    return existing[0].id;
  }
  const tab = await chrome.tabs.create({ url: "https://gemini.google.com/app", active: true });
  return tab.id;
}

async function checkChatJobs() {
  const s = await settingsOrNull();
  if (!s) return;
  const base = (s.syncBaseUrl || DEFAULT_BASE_URL).replace(/\/+$/, "");
  let jobs = [];
  try {
    const r = await fetch(`${base}/v1/chat/jobs`, {
      headers: s.syncApiKey ? { Authorization: `Bearer ${s.syncApiKey}` } : {}
    });
    if (!r.ok) return;
    ({ jobs = [] } = await r.json());
  } catch {
    return; // Server chua san sang thi thu o nhip alarm sau.
  }
  if (!jobs.length) return;

  // Chi mot job mot luc: Gemini web UI la mot cua so chat, hai job chay song
  // song se vao cung mot hoi thoai va lan nhau doc ket qua.
  for (const job of jobs) {
    if (!job?.job_id || claimedJobs.has(job.job_id) || doneJobs.has(job.job_id)) continue;

    claimedJobs.add(job.job_id);
    activeJobId = job.job_id;
    activeJobCid = job.cid || "";
    activeJobFiles = [];

    let tabId;
    try {
      tabId = await ensureGeminiTab();
    } catch (e) {
      await postJobResult(job.job_id, { error: `Khong mo duoc tab Gemini: ${e}` });
      continue;
    }
    // Moi job chay trong hoi thoai moi de khong ke thua "ngu canh" tu choi cu.
    await openFreshChat(tabId);
    await logFilesync("job-dispatch", `${job.job_id} tab=${tabId}`);
    await sendJobWithRetry(tabId, job);
    break; // het job sau, xu ly lap lai o nhip alarm toi
  }
}

// Nut tai nam trong iframe Drive viewer, khong thay duoc bang `document` cua trang
// Gemini. `executeScript` voi `allFrames` la cach chac chan tiem vao dung frame.
function clickDownloadInFrame() {
  const sel = [
    '[aria-label="Tải xuống"]',
    '[aria-label="Tải về"]',
    '[aria-label="Tai xuong"]',
    '[aria-label*="Tải xuống"]',
    '[aria-label*="Tải về"]',
    '[aria-label*="Tai xuong"]',
    '[aria-label*="Download"]',
    'button[aria-label*="Download"]',
    '[data-tooltip*="Tải xuống"]',
    '[data-tooltip*="Tai xuong"]',
    '[data-tooltip*="Download"]'
  ].join(", ");
  const el = document.querySelector(sel);
  if (!el) return null;
  el.click();
  return el.getAttribute("aria-label") || el.getAttribute("data-tooltip") || "?";
}

async function tryClickDownload(tabId, attempt) {
  if (attempt > 20 || !activeJobId) return;
  try {
    const res = await chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      func: clickDownloadInFrame
    });
    for (const r of res) {
      if (r.result) {
        await logFilesync("job-download-click", `nut="${r.result}"`);
        return;
      }
    }
  } catch {
    // Tab dang chuyen trang thi thu lai o nhip sau.
  }
  setTimeout(() => void tryClickDownload(tabId, attempt + 1), 1500);
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === CHAT_JOB_ALARM) void checkChatJobs();
});

void chrome.alarms.create(CHAT_JOB_ALARM, { periodInMinutes: 0.5 });
void checkChatJobs();

chrome.downloads.onCreated.addListener((item) => {
  if (!item?.url?.includes(FILE_DOWNLOAD_MATCH)) return;
  let name = "";
  let mime = "";
  try {
    const u = new URL(item.url);
    name = u.searchParams.get("filename") || "";
  } catch {
    // URL giả thì bỏ qua, không chặn download của người dùng.
  }
  pendingFiles.set(item.id, { url: item.url, name, mime });
  void logFilesync("dl-created", `${name} #${item.id}`);
});

chrome.downloads.onChanged.addListener((delta) => {
  if (delta?.state?.current === "complete" && pendingFiles.has(delta.id)) {
    void pushDownloadedFile(delta.id);
  }
});

// ---------- Bắt URL file qua webRequest (bất kể frame/iframe) ----------
// Preview Drive nạp file bên trong iframe khác origin nên hook `fetch` của trang
// Gemini (filesync-page.js) không thấy; mà `chrome.downloads` cũng không phát
// sinh khi nút "Tải xuống" bị Drive bỏ qua (click tổng hợp không phải user
// gesture). webRequest thấy MỌI request, kể cả trong iframe, nên đây là chỗ chắc
// chắn nhất để lấy URL, rồi service worker tự tải bytes bằng cookie phiên thật.
const FILE_URL_FILTER = [
  "*://contribution.usercontent.google.com/download*",
  "*://*.googleusercontent.com/gg-dl/*"
];

function isDeliverableFileUrl(url) {
  if (typeof url !== "string") return false;
  if (url.includes(FILE_DOWNLOAD_MATCH)) return true;
  return /googleusercontent\.com\/gg-dl\//i.test(url);
}

chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    const url = details.url || "";
    if (!isDeliverableFileUrl(url)) return;
    if (pushedUrls.has(url)) return;
    pushedUrls.add(url);
    void logFilesync("wr-url", `via=${details.type} ${url.slice(0, 180)}`);
    void fetchAndPushUrl(url);
  },
  { urls: FILE_URL_FILTER }
);

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
