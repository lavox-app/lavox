// EN translations — key: the Hungarian source string as it appears in the code.
// Every new key lands here during the sweep (wrapping strings in t()).
// The Hungarian keys are functional dictionary data (gettext-style, see
// lib/i18n.ts) — do NOT translate them; only the English values are UI copy.
export const EN: Record<string, string> = {
  "Nyelv": "Language",
  "Link URL:": "Link URL:",

  // ── modes.tsx (labels + descriptions, shown to the user via t()) ──
  "Diktálás": "Dictation",
  "Videó": "Video",
  "AI jegyzet": "AI note",
  "Gyors hangfelvétel és azonnali átirat (whisper.cpp helyben).":
    "Quick voice recording and instant transcript (whisper.cpp, on-device).",
  "Képernyő- és kamerafelvétel rögzítése, majd átirat.":
    "Screen and camera recording, then a transcript.",
  "Online meeting rögzítése rendszerhanggal és beszélő-szétválasztással.":
    "Record online meetings with system audio and speaker separation.",
  "Az átiratból AI-összefoglaló, teendők és címkék generálása.":
    "Generate an AI summary, to-dos and tags from the transcript.",

  // ── Overlay.tsx ──
  "Felvétel indítás sikertelen: {msg}": "Failed to start recording: {msg}",
  "Videó indítás sikertelen: {msg}": "Failed to start video recording: {msg}",
  "LAVOX — meeting mentve (2 sáv)": "LAVOX — meeting saved (2 tracks)",
  "Mikrofon + rendszerhang:": "Microphone + system audio:",
  "LAVOX — meeting mentve (csak mikrofon!)": "LAVOX — meeting saved (microphone only!)",
  "A többiek hangjához engedélyezd: Rendszerbeállítások → Adatvédelem → Képernyő- és rendszerhang-felvétel → Lavox Hub":
    "To capture the others' audio, enable: System Settings → Privacy → Screen & System Audio Recording → Lavox Hub",
  "Mentés hiba: {msg}": "Save error: {msg}",
  "LAVOX — átirat kész": "LAVOX — transcript ready",
  "LAVOX — ÁTIRAT HIBA": "LAVOX — TRANSCRIPT ERROR",
  "Ellenőrizd a szerver-beállítást (Beállítások → Beszélők).":
    "Check the server settings (Settings → Speakers).",
  "Hallgatlak… engedd el a leállításhoz": "Listening… release to stop",
  "Felvétel… (⌃⇧Space = stop)": "Recording… (⌃⇧Space = stop)",
  "Hiba: {msg}": "Error: {msg}",
  "Átirat…": "Transcribing…",
  "{label} — hamarosan": "{label} — coming soon",
  "Kész": "Done",
  "Diktálás · ⌃⇧Space": "Dictation · ⌃⇧Space",
  "Leállítás": "Stop",
  "Felvétel": "Record",
  "Kihagy": "Skip",
  "Felvétel leállítása": "Stop recording",
  "Videó felvétel (képernyő + hang)": "Video recording (screen + audio)",
  "Videó felvétel": "Video recording",
  "Meeting felvétel": "Record meeting",
  "Videó felvétel indítása": "Start video recording",
  "Diktálás indítása": "Start dictation",
  "Lavox Notes — jegyzetfüzet": "Lavox Notes — notebook",
  "Egy nyelv → arra rögzít · több nyelv → auto-felismerés":
    "One language → locks to it · multiple languages → auto-detect",
  "hamarosan ✨": "coming soon ✨",
  "Meeting felvétel — kattints a leállításhoz": "Meeting recording — click to stop",
  "Meeting felvétel leállítása": "Stop meeting recording",
  "Hallgatlak…": "Listening…",
  "(üres)": "(empty)",
  "Újra": "Again",

  // ── Notebook.tsx ──
  "Névtelen": "Untitled",
  "most": "just now",
  "{n} perce": "{n} min ago",
  "{n} órája": "{n} h ago",
  "{n} napja": "{n} d ago",
  "Világos": "Light",
  "Sötét": "Dark",
  "Ultra-átlátszó": "Ultra-transparent",
  "„{name}\" túl nagy (max 5 MB).": "\"{name}\" is too large (max 5 MB).",
  "Normál szöveg": "Normal text",
  "Cím (H1)": "Title (H1)",
  "Fejléc (H2)": "Heading (H2)",
  "Alcím (H3)": "Subheading (H3)",
  "Felsorolás": "Bulleted list",
  "Számozott lista": "Numbered list",
  "Idézet": "Quote",
  "Kód": "Code",
  "Félkövér": "Bold",
  "Dőlt": "Italic",
  "Aláhúzott": "Underline",
  "Áthúzott": "Strikethrough",
  "Számozott": "Numbered",
  "Formázás törlése": "Clear formatting",
  "Jegyzetlista be/ki": "Toggle note list",
  "Jegyzetlista": "Note list",
  "Új jegyzet": "New note",
  "Keresés…": "Search…",
  "Nincs találat": "No results",
  "Diktálj a barból, vagy kezdj egy újat ✍️": "Dictate from the bar, or start a new one ✍️",
  "Kiemelés levétele": "Unpin",
  "Kiemelés": "Pin",
  "Jegyzet törlése": "Delete note",
  "Törlés": "Delete",
  "üres…": "empty…",
  "Húzd az átméretezéshez": "Drag to resize",
  "Nyiss meg egy jegyzetet a listából": "Open a note from the list",
  "Írj, „/” a parancsokhoz, vagy húzz ide / illessz be képet…":
    "Type, \"/\" for commands, or drop / paste an image here…",
  "Blokk beszúrása": "Insert block",
  "Szövegstílus (cím / fejléc / normál)": "Text style (title / heading / normal)",
  "Szövegstílus": "Text style",
  "Másolás": "Copy",
  "{n} szó": "{n} words",
  "Fájl csatolása": "Attach file",
  "Fájl": "File",
  "Kép beszúrása": "Insert image",
  "Kép": "Image",
  "Téma / beállítások": "Theme / settings",
  "Téma": "Theme",
  "Üveg-téma": "Glass theme",
  "Nyiss meg egy jegyzetet, diktálj a barból, vagy kezdj egy újat.":
    "Open a note, dictate from the bar, or start a new one.",

  // ── DictationView.tsx ──
  "Nincs modell — tedd a .bin fájlt a models/ mappába":
    "No model — put the .bin file in the models/ folder",
  "Felvétel {n} másodpercig...": "Recording for {n} seconds...",
  "Transzkripció folyamatban...": "Transcription in progress...",
  "Kész — {n} szegmens, nyelv: {lang}": "Done — {n} segments, language: {lang}",
  "Hossz:": "Length:",
  "5 mp": "5 s",
  "10 mp": "10 s",
  "30 mp": "30 s",
  "1 perc": "1 min",
  "2 perc": "2 min",
  "Felvétel...": "Recording...",
  "Transzkripció...": "Transcribing...",
  "Felvétel + Transzkripció": "Record + Transcribe",
  "Átirat": "Transcript",
  "Teljes szöveg": "Full text",
  "Mégse": "Cancel",
  "Mikrofonok ({n})": "Microphones ({n})",

  // ── SettingsView.tsx ──
  "Beállítások": "Settings",
  "Rendszer-állapot, naptár és felvételi beállítások.":
    "System status, calendar and recording settings.",
  "Felület nyelve": "Interface language",
  "A felület nyelve (újratöltéssel vált)": "Interface language (switches with a reload)",
  "Csatlakozva:": "Connected:",
  "Google fiók": "Google account",
  "Leválasztás": "Disconnect",
  "Nincs csatlakoztatva": "Not connected",
  "Google bejelentkezés": "Sign in with Google",
  "Ebben a buildben nincs beállítva a Google-kliens — a naptár-összekötés nem elérhető.":
    "This build has no Google client configured — calendar linking is unavailable.",
  "A böngészőben fejezd be a bejelentkezést, majd térj vissza ide.":
    "Finish signing in in your browser, then come back here.",
  "Automatikus rögzítés meeting-eknél": "Automatic recording for meetings",
  "Automatikus hangtanulás": "Automatic voice learning",
  "Beszélő neve:": "Speaker name:",
  "Kattints az átnevezéshez — a rendszer a hangot is megtanulja":
    "Click to rename — the system also learns the voice",
  "A megnevezett beszélők hangját a rendszer megjegyzi, és a következő felvételen felirat nélkül is felismeri. A tárolt profilok lent kezelhetők/törölhetők.":
    "The system remembers the voice of named speakers and recognizes them on future recordings without captions. Stored profiles can be managed/deleted below.",
  "A meeting indulásánál automatikusan elindul a mikrofon rögzítés.":
    "Microphone recording starts automatically when the meeting begins.",
  "Értesítés érkezik meeting előtt — manuálisan indíthatod a rögzítést.":
    "You get a notification before the meeting — you can start recording manually.",
  "{n} perces meeting": "{n}-minute meeting",
  "● Rögzítés...": "● Recording...",
  "OpenRouter API kulcs": "OpenRouter API key",
  "Mentve ✓": "Saved ✓",
  "Mentés": "Save",
  "Lokálisan tárolódik, nem kerül szerverre.": "Stored locally, never sent to a server.",
  "Hozzáadás": "Add",
  "Modell": "Model",
  "Nincs GGML modell a models/ mappában — tedd ide a .bin fájlt.":
    "No GGML model in the models/ folder — put the .bin file there.",
  "Nincs észlelt bemeneti eszköz.": "No input device detected.",
  "Gyorsbillentyűk": "Keyboard shortcuts",
  "⌃⇧Space — gyors diktálás-overlay előhívása":
    "⌃⇧Space — bring up the quick dictation overlay",

  // ── AI (meeting summary) ──
  "AI": "AI",
  "A meeting-összefoglalóhoz kell. Saját kulcs, lokálisan tárolva.":
    "Required for meeting summaries. Your own key, stored locally.",

  // ── Sidebar.tsx ──
  "Módok": "Modes",
  "Modell:": "Model:",
  "hamarosan": "coming soon",

  // ── Configurable dictation trigger (SettingsView + Overlay) ──
  "Felvétel… (engedd el a triggert a leállításhoz)":
    "Recording… (release the trigger to stop)",
  "Diktálás triggere (nyomva tartás)": "Dictation trigger (hold to talk)",
  "Nyomd le a kombót…": "Press the combo…",
  "Rögzítés": "Capture",
  "Alapértelmezett: Fn nyomva tartás. Tiszta Fn-diktáláshoz állítsd: Rendszerbeállítások → Billentyűzet → „🌐 gomb megnyomására: Semmi”.":
    "Default: hold Fn. For clean Fn dictation set: System Settings → Keyboard → “Press 🌐 key to: Do Nothing”.",

  // ── SpeakersPanel.tsx (diarization enrollment) ──
  "Beszélők (meeting-felismerés)": "Speakers (meeting recognition)",
  "A meeting-átiratban név szerint azonosítja, ki beszél. Rögzíts 20 mp hangmintát beszélőnként.":
    "Identifies who is speaking by name in meeting transcripts. Record a 20s voice sample per speaker.",
  "API kulcs (opcionális)": "API key (optional)",
  "Add meg a transzkripciós szerver címét a beszélő-profilokhoz.":
    "Set the transcription server URL to manage speaker profiles.",
  "Profilok ({n})": "Profiles ({n})",
  "Frissítés": "Refresh",
  "én": "me",
  "{n} minta": "{n} samples",
  "Név (pl. Dávid)": "Name (e.g. David)",
  "ez én vagyok": "this is me",
  "Felvétel… {n}s": "Recording… {n}s",
  "20 mp felvétel": "Record 20s",
  "Olvasd fel hangosan:": "Read this aloud:",
  "Feltöltés…": "Uploading…",
  "Mentve ✓ — új minta ugyanazzal a névvel = pontosítás":
    "Saved ✓ — a new sample with the same name refines the profile",

  // ── Video control menu in the bar (camera icon → menu) ──
  "Felvétel indítás": "Start recording",
  "Előlapi kamera ki/be": "Front camera on/off",
  "Mikrofon választás": "Choose microphone",
  "Képernyő választás": "Choose screen",
  "Vissza": "Back",
  "Mikrofon": "Microphone",
  "Képernyő": "Screen",
  "Alapértelmezett mikrofon": "Default microphone",

  // ── Obsidian-export (DictationView) ──
  "Export Obsidianba": "Export to Obsidian",
  "Exportálva ✓": "Exported ✓",

  // ── Model download (SettingsView, first-run) ──
  "Nincs beszédfelismerő modell — töltsd le egy kattintással.":
    "No speech-recognition model — download it with one click.",
  "Letöltés (1,6 GB)": "Download (1.6 GB)",

  // ── MeetingsView (call recording: diarized transcript → summary → export) ──
  "Rögzített hívások: ki mit mondott (diarizált átirat), AI-összefoglaló, Obsidian-export.":
    "Recorded calls: who said what (diarized transcript), AI summary, Obsidian export.",
  "Meetingek ({n})": "Meetings ({n})",
  "Még nincs meeting — lépj be egy Google Meetbe (a bővítmény felajánlja a rögzítést), vagy kapcsold be az auto-rögzítést a Beállításokban.":
    "No meetings yet — join a Google Meet (the extension offers to record) or enable auto-record in Settings.",
  "Még nincs átirat ehhez a felvételhez.": "No transcript for this recording yet.",
  "Átirat + beszélők": "Transcript + speakers",
  "Átirat hiba: {msg} — ellenőrizd a szerver-beállítást (Beállítások → Beszélők).":
    "Transcript error: {msg} — check the server settings (Settings → Speakers).",
  "Összefoglaló": "Summary",
  "Teendők": "Action items",
  "AI-összefoglaló": "AI summary",
  "Újra-átirat": "Re-transcribe",

  // ── VideoView (Loom-style personal videos: player + transcript) ──
  "Loom-stílusú felvételeid: képernyő + arc. Indítás a bár Videó-gombjával.":
    "Your Loom-style recordings: screen + face. Start with the bar's Video button.",
  "Videók ({n})": "Videos ({n})",
  "Még nincs videó — indíts egyet a bár Videó-gombjával (🎥). A felvételben ott lesz az arcod is.":
    "No videos yet — start one with the bar's Video button (🎥). Your face will be in the recording.",
  "Ehhez a felvételhez nincs videó-fájl (csak hang).":
    "This recording has no video file (audio only).",
  "Megnyitás": "Open",
  "Átirat készítése": "Create transcript",
  "Átirat hiba: {msg} — állítsd be a szervert (Beállítások → Beszélők).":
    "Transcript error: {msg} — configure the server (Settings → Speakers).",

  // ── Meet extension connection status (SettingsView) ──
  "Meet-bővítmény csatlakozva": "Meet extension connected",
  "épp egy meetingben": "in a meeting now",
  "utolsó jelzés {n} perce": "last signal {n} min ago",
  "A Meet-bővítmény még nem jelzett.": "The Meet extension hasn't signalled yet.",
  "Hogyan töltsem be?": "How to load it?",
  "Nyisd meg a Meet-böngésződ bővítmény-oldalát (chrome://extensions vagy arc://extensions).":
    "Open your Meet browser's extensions page (chrome://extensions or arc://extensions).",
  "Kapcsold BE a „Fejlesztői mód”-ot (jobb felül).": "Turn ON “Developer mode” (top right).",
  "„Kicsomagolt bővítmény betöltése” → válaszd a ~/lavox-extension mappát.":
    "“Load unpacked” → choose the ~/lavox-extension folder.",
  "Nyiss egy Google Meetet — a bővítmény ikonján ● badge jelenik meg, és ez az állapot zöldre vált.":
    "Open a Google Meet — a ● badge appears on the extension icon and this status turns green.",

  // ── NotesView (entry point of the working Lavox Notes in the main window) ──
  "Jegyzetek": "Notes",
  "Gyors jegyzetek — diktálj a barból, vagy írj a lebegő jegyzetfüzetben.":
    "Quick notes — dictate from the bar, or write in the floating notebook.",
  "Diktálj a barból a jegyzetfüzetbe, vagy írj kézzel — minden itt gyűlik.":
    "Dictate from the bar into the notebook, or write by hand — everything collects here.",
  "Jegyzetfüzet megnyitása": "Open notebook",
  "Még nincs jegyzet — nyisd meg a jegyzetfüzetet, vagy diktálj a barból (📝).":
    "No notes yet — open the notebook, or dictate from the bar (📝).",

  // ── modes.tsx (updated mode labels + descriptions) ──
  "Meeting": "Meeting",
  "Loom-stílusú saját felvétel: képernyő + arc-buborék + hang, megosztható videó.":
    "Loom-style personal recording: screen + face bubble + audio, a shareable video.",
  "Hívás rögzítése: rendszerhang + mikrofon → diarizált átirat (ki mit mondott) + összefoglaló.":
    "Record a call: system audio + microphone → diarized transcript (who said what) + summary.",

  // ── SpeakersPanel (Hub pairing) ──
  "Párosítva ✓ — a Hub mostantól a felhős fiókodhoz kapcsolódik":
    "Paired ✓ — the Hub is now connected to your cloud account",
  "Párosító kód a webappból (pl. E9MK-G3YD)": "Pairing code from the web app (e.g. E9MK-G3YD)",
  "Párosítás": "Pair",
  "Javítás": "Fix",
  "Szótár frissítve": "Dictionary updated",
};
