// Script nay CHI QUAN SAT, KHONG DUOC GOI SERVER.
//
// Trang Gemini co CSP `connect-src` khong chua `127.0.0.1`, nen script MAIN world
// bat buoc phai goi `fetch` toi server localhost se bi CSP chan. Chung realm voi
// trang thi khong co `chrome.*`; isolated world co `chrome.*` nhung `window.fetch`
// rieng. Vì vay chia hai viec:
//
//   MAIN world (file nay)  = mat: thay URL tai file.
//   isolated (`filesync.js`) = mieng: doc storage, goi server.
//
// Script nay chi gui CHUOI URL sang isolated world. Do la du lieu serializable
// nen `postMessage` qua duoc ranh gioi realm, khong can dung mang trong MAIN.
//
// Bytes file KHONG nam trong `batchexecute?rpcids=ESY5D` nhu tinh sai dau. Ban
// bam "Download" thi Chrome tao mot download den origin khac
// (`contribution.usercontent.google.com/download?c=...`), no di qua download
// manager chu khong phai XHR cua trang.

const FILE_HOST = "contribution.usercontent.google.com";
const PAGE_SOURCE = "gemini-file-sync-page";
const HOOK_FLAG = "__geminiFileSyncUrlHooked";

const seen = new Set();

function sendUrl(url, via) {
  if (typeof url !== "string" || !url.includes(FILE_HOST)) return;
  if (seen.has(url)) return;
  seen.add(url);
  window.postMessage({ source: PAGE_SOURCE, note: "url", url, via }, "*");
}

function install() {
  if (window[HOOK_FLAG]) return;
  window[HOOK_FLAG] = true;

  // fetch: preview duyet file bang fetch, day la duong chinh.
  const originalFetch = window.fetch.bind(window);
  window.fetch = async function (...args) {
    try {
      const input = args[0];
      const url = typeof input === "string" ? input : (input?.url || "");
      sendUrl(url, "fetch");
    } catch {
      // Hook khong bao gio duoc nem loi ra ngoai.
    }
    return originalFetch(...args);
  };

  // XMLHttpRequest: mot so luong di duong nay thay fetch.
  const originalOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    try {
      sendUrl(String(url), "xhr");
    } catch {
      // Xem fetch.
    }
    return originalOpen.call(this, method, url, ...rest);
  };

  // <a download> / <a href> den host nay: buoc tai file se tao mot download.
  document.addEventListener(
    "click",
    (e) => {
      try {
        const a = e.target?.closest?.("a[href]");
        if (a) sendUrl(a.getAttribute("href") || "", "anchor");
      } catch {
        // Khong tim thay phan tu la bo qua.
      }
    },
    true
  );
}

install();