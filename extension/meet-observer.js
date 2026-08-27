// Lavox Meet Observer v2: captures captions, participants and the active speaker.
// v2 changes:
//  - Icon-junk-free name extraction: Material icon ligatures ("frame_person")
//    and tooltip texts can never enter the buffer as names
//  - Several caption strategies (jsname anchors + aria + heuristic); diag reports which one is live
//  - Live status for the popup + badge on the Chrome icon (you can see whether captions are caught)
//  - Body-level MutationObserver with throttling, so a region swap does not blind it

(() => {
  const meetCode = (location.pathname.match(/\/([a-z]{3,4}-[a-z]{4}-[a-z]{3,4})/) || [])[1] || location.pathname.slice(1) || "unknown";
  const startedAt = Date.now();

  /** @type {Array<{t:number,type:string,name?:string,text?:string,action?:string}>} */
  let buffer = [];
  let flushTimer = null;

  function emit(ev) {
    buffer.push({ t: Date.now(), ...ev });
    if (!flushTimer) flushTimer = setTimeout(flush, 1500);
  }

  async function flush() {
    flushTimer = null;
    if (buffer.length === 0) return;
    const batch = buffer;
    buffer = [];
    try {
      const key = `lavox-meet-${meetCode}`;
      const stored = await chrome.storage.local.get(key);
      const existing = stored[key] || { meetCode, startedAt, events: [] };
      existing.events.push(...batch);
      if (existing.events.length > 20000) existing.events = existing.events.slice(-20000);
      await chrome.storage.local.set({ [key]: existing });
    } catch { /* extension context invalidated */ }
    // Live captions also go to the Lavox Hub (fusion: CC names + whisper text).
    // The Hub (127.0.0.1:5192) buffers them; stopping the recording writes captions.json.
    const live = batch.filter((e) => e.type === "caption" || e.type === "active-speaker");
    if (live.length > 0) {
      try {
        chrome.runtime.sendMessage({ type: "caption-batch", payload: { meetCode, events: live } });
      } catch { /* extension context invalidated */ }
    }
  }

  // ── NAME HYGIENE ─────────────────────────────────────────────
  // Meet tiles contain icons (<i>frame_person</i>) and tooltips whose text is
  // NOT a name. Leaf-level extraction + strict validation.
  // NOTE: UI_WORDS mixes Hungarian and English Google Meet UI labels on purpose:
  // it must reject control labels in both locales. Functional data, keep as is.

  const JUNK_SELECTOR = 'i, svg, style, script, button, [role="button"], [class*="material-icons"], [class*="material-symbols"], [class*="google-material"], [aria-hidden="true"]';
  const UI_WORDS = /(közepére|kitűzés|kitűz|elnémít|némítás|feloldás|megosztás|kamera be|kamera ki|mikrofon|felirat be|turn on|turn off|mute|unmute|present now|more options|visual effects|vizuális effekt|frame_person|more_vert|devices|reakció|emoji)/i;

  function isValidName(n) {
    if (!n) return false;
    const s = n.trim();
    if (s.length < 2 || s.length > 60) return false;
    if (/[_@#{}<>[\]|/\\]/.test(s)) return false;
    if (/\d/.test(s)) return false;
    if (UI_WORDS.test(s)) return false;
    if (!/^\p{L}/u.test(s)) return false;
    if (s.split(/\s+/).length > 6) return false;
    return true;
  }

  /** Leaf element texts in DOM order, WITHOUT icon/button content. */
  function leafTexts(root, excludeEl) {
    const out = [];
    if (!root) return out;
    const all = root.querySelectorAll("*");
    for (const el of all) {
      if (el.childElementCount > 0) continue;
      if (excludeEl && (el === excludeEl || excludeEl.contains(el))) continue;
      let p = el;
      let junk = false;
      while (p && p !== root) {
        if (p.matches && p.matches(JUNK_SELECTOR)) { junk = true; break; }
        p = p.parentElement;
      }
      if (junk || (el.matches && el.matches(JUNK_SELECTOR))) continue;
      const t = (el.textContent || "").replace(/\s+/g, " ").trim();
      if (t) out.push(t);
    }
    if (out.length === 0) {
      const t = (root.textContent || "").replace(/\s+/g, " ").trim();
      if (t) out.push(t);
    }
    return out;
  }

  // ── 1. CAPTIONS: several strategies, diag reports which one is live ─────
  let captionStrategy = "-";

  function findCaptionBlocks() {
    // S1: jsname of the caption text element (stable anchor for years)
    const spans = document.querySelectorAll('[jsname="tgaKEf"]');
    if (spans.length > 0) {
      captionStrategy = "S1:tgaKEf";
      return [...spans].map((sp) => {
        let block = sp.parentElement;
        for (let i = 0; i < 5 && block && block.parentElement; i++) {
          if (block.querySelector("img")) break;
          block = block.parentElement;
        }
        return { block: block || sp.parentElement, textEl: sp };
      });
    }
    // S2: jsname of the caption window
    const win = document.querySelector('[jsname="dsyhDe"]');
    if (win && win.children.length > 0) {
      captionStrategy = "S2:dsyhDe";
      return [...win.children].map((block) => ({ block, textEl: null }));
    }
    // S3: region by aria-label (localized caption labels, incl. Hungarian "felirat")
    const labels = ["felirat", "caption", "untertitel", "sous-titres", "napisy"];
    for (const el of document.querySelectorAll('[role="region"][aria-label], div[aria-label]')) {
      const l = (el.getAttribute("aria-label") || "").toLowerCase();
      if (labels.some((x) => l.includes(x)) && el.querySelector("img") && (el.textContent || "").trim().length > 3) {
        captionStrategy = "S3:aria";
        const blocks = [...el.querySelectorAll(":scope > div")].filter((b) => b.querySelector("img"));
        return (blocks.length ? blocks : [el]).map((block) => ({ block, textEl: null }));
      }
    }
    // S4: heuristic: lower half of the screen, avatar + text
    for (const img of document.querySelectorAll('img[src*="googleusercontent"]')) {
      const box = img.closest("div[jscontroller]");
      if (!box) continue;
      const r = box.getBoundingClientRect();
      if (r.top > innerHeight * 0.45 && r.width > innerWidth * 0.25 && (box.textContent || "").trim().length > 3) {
        captionStrategy = "S4:heur";
        const region = box.parentElement;
        const blocks = [...region.children].filter((b) => b.querySelector && b.querySelector("img"));
        return (blocks.length ? blocks : [box]).map((block) => ({ block, textEl: null }));
      }
    }
    captionStrategy = "-";
    return [];
  }

  const lastCaption = new Map(); // name -> last emitted text
  let captionCount = 0;
  let lastCaptionLine = "";

  function scanCaptions() {
    const blocks = findCaptionBlocks();
    const seen = new Set();
    for (const { block, textEl } of blocks) {
      if (!block) continue;
      let name = "";
      let spoken = "";
      if (textEl) {
        spoken = (textEl.textContent || "").replace(/\s+/g, " ").trim();
        for (const t of leafTexts(block, textEl)) {
          if (isValidName(t)) { name = t; break; }
        }
      } else {
        const texts = leafTexts(block, null);
        const nameIdx = texts.findIndex((t) => isValidName(t));
        if (nameIdx === -1) continue;
        name = texts[nameIdx];
        spoken = texts.filter((_, i) => i !== nameIdx).join(" ").trim();
      }
      if (!name || !spoken || spoken.length < 2) continue;
      const key = name + "|" + spoken;
      if (seen.has(key)) continue;
      seen.add(key);
      const prev = lastCaption.get(name) || "";
      if (spoken === prev) continue;
      lastCaption.set(name, spoken);
      captionCount++;
      lastCaptionLine = `${name}: ${spoken}`;
      emit({ type: "caption", name, text: spoken });
    }
  }

  // ── 2. ACTIVE SPEAKER (aria regex matches English and Hungarian Meet) ────
  let lastActive = "";
  function scanActiveSpeaker() {
    for (const el of document.querySelectorAll("[aria-label]")) {
      const l = el.getAttribute("aria-label") || "";
      const m = l.match(/^(.+?)\s+(?:is speaking|beszél)/i);
      if (m) {
        const name = m[1].trim();
        if (isValidName(name) && name !== lastActive) {
          lastActive = name;
          emit({ type: "active-speaker", name });
        }
        return;
      }
    }
    const indicator = document.querySelector('div[class*="speaking"], [data-speaking="true"]');
    if (indicator) {
      const tile = indicator.closest("[data-participant-id], [data-requested-participant-id]") || indicator.closest("div[jscontroller]");
      if (!tile) return;
      const name = tileName(tile);
      if (name && name !== lastActive) {
        lastActive = name;
        emit({ type: "active-speaker", name });
      }
    }
  }

  /** Tile name, PRIMARILY from attribute values (not textContent!). */
  function tileName(tile) {
    const attrOwn = tile.getAttribute && (tile.getAttribute("data-self-name") || tile.getAttribute("data-participant-name"));
    if (attrOwn && isValidName(attrOwn)) return attrOwn.trim();
    const attrEl = tile.querySelector("[data-self-name], [data-participant-name]");
    if (attrEl) {
      const v = attrEl.getAttribute("data-self-name") || attrEl.getAttribute("data-participant-name");
      if (v && isValidName(v)) return v.trim();
    }
    for (const t of leafTexts(tile, null)) {
      if (isValidName(t)) return t;
    }
    return "";
  }

  // ── 3. PARTICIPANTS: attributes + panel, strict validation ──
  const knownParticipants = new Set();
  function scanParticipants() {
    const names = new Set();
    for (const el of document.querySelectorAll("[data-self-name], [data-participant-name]")) {
      const v = el.getAttribute("data-self-name") || el.getAttribute("data-participant-name");
      if (v && isValidName(v)) names.add(v.trim());
    }
    for (const el of document.querySelectorAll("[data-participant-id]")) {
      const n = tileName(el);
      if (n) names.add(n);
    }
    for (const el of document.querySelectorAll('[role="listitem"][aria-label]')) {
      const l = (el.getAttribute("aria-label") || "").split(",")[0].trim();
      if (isValidName(l)) names.add(l);
    }
    for (const name of names) {
      if (!knownParticipants.has(name)) {
        knownParticipants.add(name);
        emit({ type: "participant", name, action: "join" });
      }
    }
  }

  // ── Main loop: throttled body observer + backup interval ──
  let scanPending = false;
  function scheduleScan() {
    if (scanPending) return;
    scanPending = true;
    setTimeout(() => {
      scanPending = false;
      try { scanCaptions(); } catch { /* DOM racing */ }
      try { scanActiveSpeaker(); } catch { /* */ }
    }, 250);
  }

  const mo = new MutationObserver(scheduleScan);
  mo.observe(document.body, { childList: true, subtree: true, characterData: true });

  setInterval(scheduleScan, 900);
  setInterval(() => { try { scanParticipants(); } catch { /* */ } }, 5000);

  // ── 4. JOIN/LEAVE + LAVOX HUB (leave-call labels are localized on purpose) ──
  let inMeeting = false;

  function isJoined() {
    const labels = [
      "hívás elhagyása", "kilépés a hívásból", "kilépés",
      "leave call", "end call", "hang up",
      "anruf verlassen", "quitter l'appel",
    ];
    for (const el of document.querySelectorAll("button[aria-label], [role=button][aria-label]")) {
      const l = (el.getAttribute("aria-label") || "").toLowerCase();
      if (labels.some((x) => l.includes(x))) return true;
    }
    if (document.querySelector('[jsname="CQylAd"]')) return true;
    if (document.querySelectorAll("[data-participant-id]").length >= 2) return true;
    return false;
  }

  let lastBarResult = "(not attempted yet)";
  async function notifyBar(event) {
    try {
      const res = await chrome.runtime.sendMessage({
        type: "bar-notify",
        payload: {
          event,
          meetCode,
          title: document.title.replace(/^Meet [–-] /, ""),
          participants: [...knownParticipants],
          t: Date.now(),
        },
      });
      lastBarResult = res?.ok ? `ok(${res.status})` : `FAIL(${res?.status || res?.err || "?"})`;
    } catch (e) {
      lastBarResult = `SENDMSG_FAIL(${String(e).slice(0, 60)})`;
    }
  }

  let joinedAt = 0;
  setInterval(() => {
    try {
      const joined = isJoined();
      if (joined && !inMeeting) {
        inMeeting = true;
        joinedAt = Date.now();
        emit({ type: "session", action: "joined" });
        notifyBar("joined");
      } else if (!joined && inMeeting) {
        inMeeting = false;
        emit({ type: "session", action: "left" });
        notifyBar("left");
      }
    } catch { /* */ }
  }, 2000);

  // ── LIVE STATUS: popup + badge ──────────────────────────────
  function reportStatus() {
    const status = {
      meetCode,
      joined: inMeeting,
      captionStrategy,
      captionCount,
      lastCaption: lastCaptionLine.slice(0, 140),
      participants: [...knownParticipants].slice(0, 12),
      captionsOff: inMeeting && captionStrategy === "-" && Date.now() - joinedAt > 20000,
      bar: lastBarResult,
      t: Date.now(),
    };
    try { chrome.runtime.sendMessage({ type: "live-status", status }); } catch { /* */ }
    try { chrome.storage.local.set({ "lavox-live-status": status }); } catch { /* */ }
  }
  setInterval(() => { try { reportStatus(); } catch { /* */ } }, 2500);

  // ── SELF-DIAGNOSTICS: which strategy is live, what the DOM shows ─────
  function diagSnapshot() {
    let sample = "";
    try {
      const blocks = findCaptionBlocks();
      if (blocks.length > 0 && blocks[0].block) {
        sample = blocks[0].block.outerHTML.slice(0, 220);
      }
    } catch { /* */ }
    emit({
      type: "diag",
      text: JSON.stringify({
        joined: inMeeting,
        strategy: captionStrategy,
        captions: captionCount,
        tiles: document.querySelectorAll("[data-participant-id]").length,
        participants: [...knownParticipants],
        bar: lastBarResult,
        sample,
      }),
    });
  }
  setInterval(() => { try { diagSnapshot(); } catch { /* */ } }, 15000);
  setTimeout(() => { try { diagSnapshot(); } catch { /* */ } }, 4000);

  emit({ type: "session", action: "start" });
  window.addEventListener("beforeunload", () => {
    emit({ type: "session", action: "end" });
    if (inMeeting) notifyBar("left");
    flush();
  });

  console.log(`[Lavox] Meet observer v2 active: ${meetCode}`);
})();
