// Cau noi giua trang Gemini (MAIN world) va server.
//
// Trang ban chay o isolated world nen khong cham duoc `window.fetch` cua web UI -
// viec bat response do `filesync-page.js` (world MAIN) dam nhan. Script nay lam
// phan con lai: doc storage, biet ten/MIME tu chip file, va day bytes len server.

const DEFAULT_BASE_URL = "http://127.0.0.1:8000";
const PAGE_SOURCE = "gemini-file-sync-page";

const pushed = new Set();
let lastChip = null;

// `f.sid` chi xuat hien tren URL cua cac request batchexecute trang vua gui, nen doc
// tu Performance Resource Timing la cach re nhat de lay dung session hien tai.
function currentFsid() {
  try {
    const entries = performance.getEntriesByType("resource");
    for (let i = entries.length - 1; i >= 0; i--) {
      const name = entries[i].name || "";
      if (!name.includes("batchexecute")) continue;
      const m = name.match(/[?&]f\.sid=(-?\d+)/);
      if (m) return m[1];
    }
  } catch {
    // Khong co Performance API thi download se fail nhung khong ga trang.
  }
  return "";
}

function conversationId() {
  const m = location.pathname.match(/\/app\/([A-Za-z0-9_-]+)/);
  return m ? m[1] : "";
}

function responseId() {
  const m = location.pathname.match(/[?&]rcid=([A-Za-z0-9_-]+)/);
  return m ? m[1] : "";
}

// Chip file giu metadata o `jslog` (base64). Giai ma de lay r_/c_ thi cham hon
// la doc DOM, va `jslog` la noi duy nhat server chup duoc de doi chieu.
function idsFromChip(chip) {
  const raw = chip?.getAttribute?.("jslog");
  if (!raw) return {};
  try {
    const json = atob(raw);
    const r = json.match(/\br_[A-Za-z0-9_-]+/);
    const c = json.match(/\bc_[A-Za-z0-9_-]+/);
    return { rcid: r ? r[0] : "", cid: c ? c[0] : "" };
  } catch {
    return {};
  }
}

// Icon cua chip file la https://drive-thirdparty.googleusercontent.com/32/type/<mime>
function mimeFromIconSrc(src) {
  const m = (src || "").match(/\/type\/([\w.+-]+\/[\w.+-]+)/);
  return m ? m[1] : "";
}

function getSetting(key, fallback) {
  try {
    return chrome.storage.local.get(key).then((v) => v[key] ?? fallback);
  } catch {
    return Promise.resolve(fallback);
  }
}

function readChip(chip) {
  const title = chip?.querySelector(".file-name-lr[title]")?.getAttribute("title");
  if (!title) return null;
  return {
    name: title,
    mime: mimeFromIconSrc(chip.querySelector("img.file-icon-lr")?.src || ""),
    ...idsFromChip(chip)
  };
}

// MAIN world khong doc duoc `chrome.storage`, nen ghi base URL xuong DOM de
// `filesync-page.js` gui bao loi len server duoc.
async function exposeBaseUrl() {
  const base = (await getSetting("syncBaseUrl", DEFAULT_BASE_URL)).replace(/\/+$/, "");
  try {
    document.documentElement.dataset.filesyncBase = base;
  } catch {
    // Khong gan duoc DOM thi chi mat phan bao loi, khong anh huong upload.
  }
  return base;
}

async function diag(stage, detail) {
  try {
    const base = await exposeBaseUrl();
    const apiKey = await getSetting("syncApiKey", "");
    await fetch(`${base}/v1/files/debug`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {})
      },
      body: JSON.stringify({ stage, detail: String(detail).slice(0, 500) })
    });
  } catch {
    // Bao loi that bai khong duoc lam chap trang.
  }
}

// Payload RPC chi co chi so file, khong mang ten - ma server can duoi file de client
// tai ve mo duoc. Web UI goi ESY5D ngay sau khi bam Open/Download tren chip, nen
// ghi lai chip vua bam de gan ten khi bytes den.
function noteChipClick(event) {
  const chip = event.target?.closest?.(".chip-lr");
  const info = readChip(chip);
  if (info) lastChip = info;
}

async function upload(index, bytes, chip) {
  const base = (await getSetting("syncBaseUrl", DEFAULT_BASE_URL)).replace(/\/+$/, "");
  const apiKey = await getSetting("syncApiKey", "");
  if (!bytes || !bytes.length) return false;

  const cid = chip?.cid || conversationId();
  const rcid = chip?.rcid || responseId();
  const key = `${cid}|${rcid}|${index}`;
  if (pushed.has(key)) return false;

  const query = new URLSearchParams({ key, index });
  if (chip?.name) query.set("name", chip.name);
  if (chip?.mime) query.set("mime", chip.mime);

  try {
    const resp = await fetch(`${base}/v1/files/upload?${query}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {})
      },
      body: bytes
    });
    if (!resp.ok) {
      console.debug("[filesync] upload that bai", resp.status, index);
      return false;
    }
    const saved = await resp.json().catch(() => null);
    pushed.add(key);
    console.debug("[filesync] da day file", index, saved?.filename || "");
    return true;
  } catch (e) {
    console.debug("[filesync] upload loi", e);
    return false;
  }
}

window.addEventListener("message", (event) => {
  if (event.source !== window) return;
  const data = event.data;
  if (!data || data.source !== PAGE_SOURCE) return;
  if (data.note === "url") {
    // MAIN world chi gui chuoi URL (du lieu serializable, qua duoc ranh gioi realm).
    // Chuyen sang service worker de no tai bytes bang cookie phien that roi day len
    // server — server tu tai URL nay se bi 403.
    void diag("dl-url", `${data.via || "?"} ${String(data.url).slice(0, 200)}`);
    try {
      chrome.runtime.sendMessage({ type: "filesync:url", url: data.url });
    } catch {
      // Khong gui duoc thi vong quet DOM sau se thu lai.
    }
    return;
  }
  if (data.note === "ack") {
    // ESY5D tra ve JSON thay vi bytes: can ghi lai de biet thuc te no tra gi.
    console.debug("[filesync] ESY5D chi tra ack", data.index, data.preview);
    return;
  }
  const bytes = data.bytes instanceof ArrayBuffer ? new Uint8Array(data.bytes) : null;
  if (!bytes) return;
  void upload(data.index, bytes, lastChip);
});

// Chip moi xuat hien la du lieu ESY5D chua ton tai, nen phai bam Open/Download
// giup thay vi doi nguoi dung thao tac. Chi bam chip chua tung thu.
function scanChips(force) {
  const all = document.querySelectorAll(".chip-lr");
  const withName = document.querySelectorAll(".chip-lr:has(.file-name-lr[title])");
  let clicked = 0;
  for (const chip of withName) {
    if (!force && chip.dataset.filesyncTried === "1") continue;
    const info = readChip(chip);
    if (!info) continue;
    chip.dataset.filesyncTried = "1";
    lastChip = info;
    const labels = [...chip.querySelectorAll("button")]
      .map((b) => b.getAttribute("aria-label") || b.textContent?.trim().slice(0, 20) || "?")
      .join("|");
    // Phai truy van tung nut rieng. `querySelector("a, b")` tra ve phan tu dau
    // tien theo thu tu DOM, nen nut "Open preview" (nam truoc trong DOM) thang
    // nut "Download" du selector cu viet Download truoc — chi bam Preview thi
    // khong bao gio co bytes.
    const btn =
      chip.querySelector("button[aria-label*='ownload']") ||
      chip.querySelector("[data-tooltip*='ownload']") ||
      chip.querySelector("button[aria-label*='Open']") ||
      chip.querySelector("button");
    if (btn) {
      // Service worker tự bắt bytes qua `chrome.downloads`; nó chỉ cần biết chip
      // này thuộc conversation nào để gắn file vào response đúng.
      try {
        chrome.runtime.sendMessage({ type: "filesync:context", name: info.name, cid: info.cid, mime: info.mime });
      } catch {
        // Không gửi được context thì file vẫn tải lên, chỉ mất liên kết cid.
      }
      const jslogRaw = chip.getAttribute("jslog") || "";
      let jslogPreview = "(none)";
      if (jslogRaw) {
        try {
          jslogPreview = atob(jslogRaw).slice(0, 200);
        } catch (e) {
          jslogPreview = `RAW(len=${jslogRaw.length}) undecodable`;
        }
      }
      void diag("chip", `name=${info.name} mime=${info.mime} cid=${info.cid} jslog=${jslogPreview} buttons=[${labels}]`);
      void diag("chip-html", chip.outerHTML.slice(0, 560));
      // "Mo" chi mo preview (4 nut: Tai xuong / In / menu / X). Hay bam nut
      // "Tai xuong" trong do; khong bam "Mo" mot lan nua.
      btn.click();
      clicked++;
      void clickDownloadInPreview();
    }
  }
  return { all: all.length, withName: withName.length, clicked };
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "gemini-run") {
    void runJob(msg);
    sendResponse({ ok: true });
    return true;
  }
  if (msg?.type === "filesync:scan") {
    scanChips(Boolean(msg.force));
    sendResponse({ ok: true, cid: conversationId(), chips: document.querySelectorAll(".chip-lr").length });
    return true;
  }
  if (msg?.type === "filesync:ping") {
    sendResponse({ ok: true, cid: conversationId(), fsid: currentFsid() });
    return true;
  }
  return false;
});

// URL tải file KHÔNG nằm trong batchexecute: nó là
// `contribution.usercontent.google.com/download?c=...`, xuất hiện trong DOM của
// hộp thoại xem trước (hoặc trong resource timing sau khi preview nạp file).
// Dò cả hai nguồn: không cần bấm Download thật, nên không tạo file rác trong
// thư mục Downloads của người dùng.
function probeDownloadUrls() {
  const found = new Set();
  for (const el of document.querySelectorAll("a[href], [data-href], [href]")) {
    const href = el.getAttribute("href") || el.getAttribute("data-href") || "";
    if (href.includes("usercontent.google.com")) found.add(href);
  }
  try {
    for (const e of performance.getEntriesByType("resource")) {
      if (e.name && e.name.includes("usercontent.google.com")) found.add(e.name);
    }
  } catch {
    // Resource timing không khả dụng thì chỉ còn DOM là đủ.
  }
  return [...found].filter((u) => u.includes("/download")).slice(0, 4);
}

// Preview có 4 nút: Tải xuống, In, ⋮, ✕. Bấm "Tải xuống" sẽ mở một tab
// `about:blank`, tải xong thì tab tự đóng — nên download đi qua download manager,
// không phải fetch của trang, và hook URL ở MAIN world không thấy được.
// `chrome.downloads.onCreated` trong service worker mới là chỗ bắt được bytes.
const DOWNLOAD_LABEL_RE = /(tải xuống|tai xuong|download)/i;

async function clickDownloadInPreview() {
  for (let i = 0; i < 12; i++) {
    let hit = null;
    for (const b of document.querySelectorAll("button, [role='button']")) {
      const label = (b.getAttribute("aria-label") || b.textContent || "").trim();
      if (!label || !DOWNLOAD_LABEL_RE.test(label)) continue;
      if (/^mở|^mo\b/i.test(label)) continue;
      hit = b;
      break;
    }
    if (hit) {
      void diag("dl-click", `bam "${(hit.getAttribute("aria-label") || hit.textContent || "").trim().slice(0, 40)}"`);
      hit.click();
      return true;
    }
    await new Promise((r) => setTimeout(r, 700));
  }
  void diag("dl-miss", "khong tim thay nut tai file trong preview");
  return false;
}

async function pushProbeUrls() {
  for (const url of probeDownloadUrls()) {
    void diag("dl-url", url.slice(0, 300));
    try {
      // Service worker tải bytes bằng cookie phiên thật rồi đẩy lên server.
      chrome.runtime.sendMessage({ type: "filesync:url", url });
    } catch {
      // Không gửi được thì bỏ qua, vòng quét sau sẽ thử lại.
    }
  }
}

function start() {
  exposeBaseUrl();
  document.addEventListener("click", noteChipClick, true);

  let lastReport = 0;
  const mo = new MutationObserver(() => {
    const r = scanChips(false);
    // MutationObserver bat lien tuc; chi bao cao khi co gi doi thuc su.
    const stamp = Date.now();
    if ((r.all || r.clicked) && stamp - lastReport > 4000) {
      lastReport = stamp;
      void diag("scan", `chips=${r.all} withName=${r.withName} clicked=${r.clicked}`);
      void pushProbeUrls();
    }
  });
  mo.observe(document.documentElement, { childList: true, subtree: true });

  void diag("boot", `url=${location.pathname}`);

  exposeBaseUrl()
    .then((base) => fetch(`${base}/v1/files/ping`).catch(() => {}))
    .catch(() => {});
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", start, { once: true });
} else {
  start();
}
// ---------------------------------------------------------------------------
// Chay mot job tu server: go prompt vao Gemini, doi cau tra loi, tai file.
// Server khong goi duoc `generate_content` de tao file (do la API, khong co
// sandbox), nen phan sinh cau tra loi phai chay o day.
// ---------------------------------------------------------------------------

function findComposer() {
  return (
    document.querySelector("rich-textarea .ql-editor") ||
    document.querySelector("rich-textarea textarea") ||
    document.querySelector('div[contenteditable="true"][role="textbox"]') ||
    document.querySelector('[contenteditable="true"]')
  );
}

function findSendButton() {
  return (
    document.querySelector("button.send-button") ||
    document.querySelector('button[aria-label*="Send"]') ||
    document.querySelector('button[aria-label*="Gửi"]')
  );
}

// Gán `innerText` KHÔNG kích hoạt framework của Gemini: editor là Quill, nó nghe
// sự kiện `input` nên phải đi qua `execCommand` để có đúng thao tác nhập thật.
async function typePrompt(text) {
  const box = findComposer();
  if (!box) throw new Error("Khong tim thay o nhap cua Gemini");

  box.focus();
  await new Promise((r) => setTimeout(r, 120));
  document.execCommand("selectAll", false, null);
  document.execCommand("delete", false, null);
  if (!document.execCommand("insertText", false, text)) {
    // Fallback: set text roi ban input de tranh framework bo qua.
    box.textContent = text;
    box.dispatchEvent(new InputEvent("input", { bubbles: true, data: text }));
  }
  await diag("prompt-typed", `len=${text.length} execOK`);
  await new Promise((r) => setTimeout(r, 250));
  return box;
}

async function sendPrompt() {
  const btn = findSendButton();
  if (btn && !btn.disabled) {
    btn.click();
    await diag("prompt-sent", "bang nut gui");
    return true;
  }
  // Khong nut gui (van xam): Enter la cach duy nhat.
  const box = findComposer();
  box.focus();
  box.dispatchEvent(
    new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true })
  );
  await diag("prompt-sent", "bang Enter");
  return true;
}

// Cau tra loi cua Gemini la cac khoi `model-response`. Cho bang chay het (khong con
// nut "stop") truoc khi doc, neu doc som se lay ban nhap.
function lastResponseText() {
  const blocks = [...document.querySelectorAll("model-response")];
  if (!blocks.length) return "";
  const last = blocks[blocks.length - 1];
  return (last.innerText || "").trim();
}

function isGenerating() {
  return Boolean(
    document.querySelector('button[aria-label*="Stop"], button.stop-button, .stop-generating')
  );
}

async function waitForReply(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let stableSince = 0;
  let lastLen = -1;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 1000));
    const txt = lastResponseText();
    const gen = isGenerating();
    if (!gen && txt && txt.length === lastLen) {
      if (!stableSince) stableSince = Date.now();
      // Can 2 lan lien tiep giong nhau de dam bao da render xong.
      if (Date.now() - stableSince > 2000) return txt;
    } else {
      stableSince = 0;
    }
    lastLen = txt.length;
  }
  const tail = lastResponseText();
  if (tail) return tail;
  throw new Error("Gemini khong tra loi trong thoi gian cho");
}

async function runJob({ jobId, prompt }) {
  try {
    await diag("job-run", `job=${jobId} prompt="${String(prompt).slice(0, 60)}"`);
    await waitForIdleComposer();
    await typePrompt(prompt);
    await sendPrompt();
    const text = await waitForReply(240000);
    await diag("job-reply", `job=${jobId} len=${text.length}`);

    // Mo chip file de background service worker bat download trong iframe Drive.
    const chips = [...document.querySelectorAll(".chip-lr")];
    await diag("job-chips", `job=${jobId} chips=${chips.length}`);
    for (const chip of chips) {
      const btn = chip.querySelector("button") || chip;
      btn.click();
      await new Promise((r) => setTimeout(r, 900));
      chrome.runtime.sendMessage({ type: "gemini-tab-ready" }).catch(() => {});
      await new Promise((r) => setTimeout(r, 2500));
    }
    // Cho download + upload kip chay truoc khi bao ket qua.
    await new Promise((r) => setTimeout(r, 4000));

    chrome.runtime.sendMessage({
      type: "gemini-job-done",
      jobId,
      text,
      cid: conversationId(),
      files: []
    }).catch(() => {});
  } catch (e) {
    await diag("job-error", `job=${jobId}: ${e}`);
    chrome.runtime
      .sendMessage({ type: "gemini-job-failed", jobId, error: String(e && e.message ? e.message : e) })
      .catch(() => {});
  }
}

// Cho den khi o nhap san sang (tab moi vua mo, UI chua render xong).
async function waitForIdleComposer(timeoutMs) {
  const deadline = Date.now() + (timeoutMs || 20000);
  while (Date.now() < deadline) {
    if (findComposer()) return true;
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error("Khong tim thay o nhap cua Gemini");
}
