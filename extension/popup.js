// Lavox Meet Capture popup: live status, so you can see whether the observer is catching captions.

async function render() {
  const el = document.getElementById("s");
  try {
    const all = await chrome.storage.local.get(null);
    const live = all["lavox-live-status"];
    const sessions = Object.entries(all).filter(([k]) => k.startsWith("lavox-meet-"));
    const totalEvents = sessions.reduce((s, [, v]) => s + (v?.events?.length || 0), 0);

    if (!live || Date.now() - (live.t || 0) > 15000) {
      el.innerHTML = `
        <div class="row"><span class="k">Observer</span><span class="v bad">not running / no Meet tab</span></div>
        <div class="row"><span class="k">Stored sessions</span><span class="v">${sessions.length} (${totalEvents} events)</span></div>
        <div class="foot">Open a meet.google.com tab and join the call.</div>`;
      return;
    }

    const strat = live.captionStrategy || "-";
    const stratHtml = strat === "-"
      ? '<span class="v bad">not found</span>'
      : `<span class="v ok">${strat}</span>`;

    let html = `
      <div class="row"><span class="k">Meeting</span><span class="v">${live.meetCode || "?"}</span></div>
      <div class="row"><span class="k">In the call</span><span class="v ${live.joined ? "ok" : "warn"}">${live.joined ? "yes" : "no"}</span></div>
      <div class="row"><span class="k">Caption source</span>${stratHtml}</div>
      <div class="row"><span class="k">Captions captured</span><span class="v ${live.captionCount > 0 ? "ok" : "warn"}">${live.captionCount || 0}</span></div>
      <div class="row"><span class="k">Hub connection</span><span class="v">${live.bar || "?"}</span></div>`;

    if (live.captionsOff) {
      html += `<div class="warnbox">Turn on captions in Meet (CC button). Without them there are no names or text.</div>`;
    }
    if (live.lastCaption) {
      html += `<div class="cap">„${live.lastCaption}"</div>`;
    }
    if (live.participants && live.participants.length > 0) {
      html += `<div class="parts"><b>Participants (${live.participants.length}):</b> ${live.participants.join(", ")}</div>`;
    }
    html += `<div class="foot">Stored sessions: ${sessions.length} · ${totalEvents} events · v2</div>`;
    el.innerHTML = html;
  } catch (e) {
    el.textContent = `Error: ${e}`;
  }
}

render();
setInterval(render, 1000);
