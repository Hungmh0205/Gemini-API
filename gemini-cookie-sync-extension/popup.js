const statusEl = document.getElementById("status");
const inspectBtn = document.getElementById("inspect");
const syncBtn = document.getElementById("sync");
const openBtn = document.getElementById("open");
const exportBtn = document.getElementById("export");
const baseUrlInput = document.getElementById("baseUrl");
const apiKeyInput = document.getElementById("apiKey");

function setStatus(text, isError = false) {
  statusEl.textContent = text;
  statusEl.className = isError ? "error" : "ok";
}

function showPayload(info) {
  if (!info || !info.validation || !info.pageMetadata) return;
  const keys = [...info.selected.keys()];
  const ready = info.validation.valid && Boolean(info.pageMetadata.xsrfToken);
  const lines = [
    `Google cookie(s): ${keys.length}`,
    `Session cookie: ${info.validation.sessionCookie || "missing"}`,
    `auth_user: ${info.authUser ?? "default account"}`,
    `XSRF / SNlM0e: ${info.pageMetadata.xsrfToken ? "present" : "missing"}`,
    `gemini_bl / cfb2h: ${info.pageMetadata.geminiBl ? "present" : "missing"}`,
    "",
    ready
      ? "Session and XSRF are ready to export / sync."
      : "Session is incomplete: SAPISID and one of __Secure-1PSID, __Secure-3PSID, or SID are required."
  ];
  setStatus(lines.join("\n"));
}

async function populateSettings() {
  // Server là nguồn của base_url + API key, nên mở popup là thấy key đúng,
  // không phải gõ tay. Nếu server chưa chạy thì giữ nguyên giá trị đang lưu.
  const settings = await ensureSettings();
  baseUrlInput.value = settings.syncBaseUrl || "";
  apiKeyInput.value = settings.syncApiKey || "";
  if (settings.lastAutoSyncSuccess) {
    const last = new Date(settings.lastAutoSyncSuccess).toLocaleTimeString();
    if (settings.lastAutoSyncError) {
      setStatus(
        `Last sync attempt failed (${settings.lastAutoSyncError}). Retrying automatically in background…`,
        true
      );
    } else {
      setStatus(`Server was synced at ${last}. No additional action needed right now.`);
    }
  } else if (!settings.syncApiKey) {
    setStatus("No API key yet — is the server running on the base URL above?", true);
  }
}

async function getStableInfo() {
  const captured = await captureStableInspection({ attempts: 3, settleMs: 3000 });
  if (!captured.stable || !captured.info) {
    throw new Error("Snapshot is unstable (Google may be mid-rotation). Retrying later in the background.");
  }
  return captured.info;
}

inspectBtn.addEventListener("click", async () => {
  inspectBtn.disabled = true;
  inspectBtn.textContent = "Scanning…";
  setStatus("");
  try {
    showPayload(await buildInspection());
  } catch (err) {
    setStatus(`Inspect failed: ${err.message || err}`, true);
  } finally {
    inspectBtn.disabled = false;
    inspectBtn.textContent = "Inspect session";
  }
});

openBtn.addEventListener("click", async () => {
  setStatus("Opening gemini.google.com…");
  await chrome.tabs.create({ url: "https://gemini.google.com/app" }).catch(() => {});
});

syncBtn.addEventListener("click", async () => {
  syncBtn.disabled = true;
  syncBtn.textContent = "Checking…";
  setStatus("Taking a stable snapshot…");

  try {
    await chrome.storage.local.set({
      syncBaseUrl: baseUrlInput.value || DEFAULT_BASE_URL,
      syncApiKey: apiKeyInput.value || ""
    });

    if (!apiKeyInput.value) {
      throw new Error("API key is empty. Enter the key from config.json.");
    }

    const info = await getStableInfo();
    const keys = [...info.selected.keys()];
    if (!info.validation.valid || !info.pageMetadata.xsrfToken) {
      throw new Error(
        "Missing required cookies. Open gemini.google.com, sign in, refresh the page, then try again."
      );
    }

    setStatus("Sending to server…");
    await pushToServer(info, {
      baseUrl: baseUrlInput.value,
      apiKey: apiKeyInput.value
    });
    chrome.runtime.sendMessage({ type: "reset-auto-sync" }).catch(() => {});
    setStatus(
      [
        `Exported ${keys.length} cookie(s) to server.`,
        `Session cookie: ${info.validation.sessionCookie || "missing"}`,
        `auth_user: ${info.authUser ?? "default account"}`,
        "Server wrote gemini-auth.json and reloaded itself."
      ].join("\n")
    );
  } catch (err) {
    setStatus(`Sync failed: ${err.message || err}`, true);
  } finally {
    syncBtn.disabled = false;
    syncBtn.textContent = "Sync to server";
  }
});

exportBtn.addEventListener("click", async () => {
  exportBtn.disabled = true;
  setStatus("Reading session…");
  try {
    const info = await getStableInfo();
    const payload = buildAuthPayload(info);
    const blob = new Blob([JSON.stringify(payload, null, 2)], {
      type: "application/json"
    });
    const url = URL.createObjectURL(blob);
    await chrome.downloads.download({
      url,
      filename: "gemini-auth.json"
    });
    setStatus(`Exported ${[...info.selected.keys()].length} cookie(s) to gemini-auth.json.`);
  } catch (err) {
    setStatus(`Export failed: ${err.message || err}`, true);
  } finally {
    exportBtn.disabled = false;
  }
});

populateSettings();