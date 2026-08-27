// Lavox Dashboard Bridge: hands the Meet events collected in chrome.storage
// to the Lavox web app via window.postMessage.
// The web app receives them with the lib/meet-events.ts listener.

(() => {
  async function pushAll() {
    try {
      const all = await chrome.storage.local.get(null);
      const sessions = Object.entries(all)
        .filter(([k]) => k.startsWith("lavox-meet-"))
        .map(([, v]) => v);
      if (sessions.length === 0) return;
      window.postMessage({ source: "lavox-meet-capture", type: "meet-sessions", sessions }, window.location.origin);
    } catch {
      // extension context invalidated
    }
  }

  // The web app may request deletion once it has stored them
  window.addEventListener("message", async (e) => {
    if (e.source !== window || e.data?.source !== "lavox-dashboard") return;
    if (e.data.type === "meet-sessions-ack" && Array.isArray(e.data.meetCodes)) {
      try {
        const keys = e.data.meetCodes.map((c) => `lavox-meet-${c}`);
        await chrome.storage.local.remove(keys);
      } catch { /* */ }
    }
    if (e.data.type === "meet-sessions-request") pushAll();
  });

  // Forward freshly arriving events automatically
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    if (Object.keys(changes).some((k) => k.startsWith("lavox-meet-"))) pushAll();
  });

  // Once on startup
  pushAll();
  console.log("[Lavox] Dashboard bridge active");
})();
