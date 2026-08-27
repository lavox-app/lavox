// Lavox Meet Capture service worker.
// 1) The Hub (127.0.0.1:5192) is notified FROM HERE: a content-script fetch
//    would be blocked by CORS / Private Network Access, the service worker's
//    is allowed by host_permissions.
// 2) Badge: green dot = catching captions; orange CC = joined but no captions.
// 3) Cleanup: meeting events older than 48 hours are deleted.

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.type === "bar-notify") {
    fetch("http://127.0.0.1:5192/lavox/meeting", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(msg.payload),
    })
      .then((r) => sendResponse({ ok: r.ok, status: r.status }))
      .catch((e) => sendResponse({ ok: false, err: String(e) }));
    return true; // async response
  }

  // Live CC captions to the Hub, fire-and-forget (silent if the Hub is not running).
  if (msg?.type === "caption-batch") {
    fetch("http://127.0.0.1:5192/lavox/captions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(msg.payload),
    }).catch(() => { /* Hub not running: not an error */ });
  }

  if (msg?.type === "live-status") {
    const s = msg.status || {};
    const tabId = sender?.tab?.id;
    if (tabId != null) {
      const text = s.joined ? (s.captionCount > 0 ? "●" : "CC") : "";
      const color = s.captionCount > 0 ? "#3d7a5e" : "#c47f2a";
      try {
        chrome.action.setBadgeText({ tabId, text });
        chrome.action.setBadgeBackgroundColor({ tabId, color });
      } catch { /* tab closed */ }
    }
  }
});

chrome.runtime.onInstalled.addListener(cleanup);
chrome.runtime.onStartup.addListener(cleanup);

async function cleanup() {
  try {
    const all = await chrome.storage.local.get(null);
    const cutoff = Date.now() - 48 * 3600 * 1000;
    const stale = Object.entries(all)
      .filter(([k, v]) => k.startsWith("lavox-meet-") && (v?.startedAt || 0) < cutoff)
      .map(([k]) => k);
    if (stale.length) await chrome.storage.local.remove(stale);
  } catch {
    // ignore
  }
}
