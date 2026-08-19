//! M2.2 — átirat beillesztése az aktív app kurzorához (Wispr-szerű rendszerszintű diktálás).
//!
//! Módszer (Wispr-mintára):
//!   1. A diktálás INDULÁSAKOR elmentjük, melyik app volt aktív (bundle ID) — `lib.rs`.
//!   2. Beillesztéskor a szöveget a vágólapra tesszük.
//!   3. VISSZAAKTIVÁLJUK a cél-appot (`open -b <bundleID>`) → a fókusz biztosan ott van.
//!   4. Szintetikus Cmd+V (CGEvent) → a szöveg a kurzorhoz kerül.
//!   5. A vágólapot NEM állítjuk vissza → a diktált szöveg ott marad, így ha az
//!      auto-paste valamiért nem fog, a felhasználó kézzel is be tudja illeszteni (⌘V).
//!
//! ⚠️ macOS Accessibility engedély kell a CGEvent posztoláshoz.

#[cfg(target_os = "macos")]
pub fn insert_text(text: &str, target_app: Option<&str>) -> Result<(), String> {
    use core_graphics::event::{CGEvent, CGEventFlags, CGEventTapLocation};
    use core_graphics::event_source::{CGEventSource, CGEventSourceStateID};

    if text.trim().is_empty() {
        return Ok(());
    }

    // 1) A szöveg a vágólapra (NEM állítjuk vissza → kézi ⌘V fallback mindig működik).
    let mut clipboard = arboard::Clipboard::new().map_err(|e| e.to_string())?;
    clipboard
        .set_text(text.to_owned())
        .map_err(|e| e.to_string())?;

    // 2) A cél-app visszaaktiválása (Wispr: "performing paste after app activation").
    //    Így a Cmd+V akkor is a jó helyre megy, ha közben a fókusz elcsúszott
    //    (pl. a pill kinyílt). `open -b` = aktiválás bundle ID alapján.
    if let Some(app) = target_app {
        if !app.is_empty() {
            let _ = std::process::Command::new("open")
                .args(["-b", app])
                .output();
            // Adjunk időt a célapp előtérbe jövetelének + a vágólapnak.
            std::thread::sleep(std::time::Duration::from_millis(120));
        }
    } else {
        std::thread::sleep(std::time::Duration::from_millis(90));
    }

    // 3) Szintetikus Cmd+V.
    let source = CGEventSource::new(CGEventSourceStateID::CombinedSessionState)
        .map_err(|_| "CGEventSource létrehozása sikertelen (Accessibility engedély?)".to_string())?;
    const V_KEYCODE: core_graphics::event::CGKeyCode = 9; // 'v'

    let down = CGEvent::new_keyboard_event(source.clone(), V_KEYCODE, true)
        .map_err(|_| "keydown esemény sikertelen".to_string())?;
    down.set_flags(CGEventFlags::CGEventFlagCommand);
    down.post(CGEventTapLocation::HID);

    let up = CGEvent::new_keyboard_event(source, V_KEYCODE, false)
        .map_err(|_| "keyup esemény sikertelen".to_string())?;
    up.set_flags(CGEventFlags::CGEventFlagCommand);
    up.post(CGEventTapLocation::HID);

    Ok(())
}

#[cfg(not(target_os = "macos"))]
pub fn insert_text(_text: &str, _target_app: Option<&str>) -> Result<(), String> {
    Err("A kurzorhoz beillesztés jelenleg csak macOS-en támogatott.".to_string())
}
