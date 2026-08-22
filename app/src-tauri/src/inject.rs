//! M2.2 — inserting the transcript at the active app's cursor (Wispr-like system-wide dictation).
//!
//! Method (after Wispr):
//!   1. When dictation STARTS, we save which app was active (bundle ID) — `lib.rs`.
//!   2. On insertion, the text is put on the clipboard.
//!   3. We RE-ACTIVATE the target app (`open -b <bundleID>`) → focus is guaranteed there.
//!   4. Synthetic Cmd+V (CGEvent) → the text lands at the cursor.
//!   5. The clipboard is NOT restored → the dictated text stays there, so if the
//!      auto-paste fails for any reason, the user can still paste manually (⌘V).
//!
//! ⚠️ Requires the macOS Accessibility permission to post CGEvents.

#[cfg(target_os = "macos")]
pub fn insert_text(text: &str, target_app: Option<&str>) -> Result<(), String> {
    use core_graphics::event::{CGEvent, CGEventFlags, CGEventTapLocation};
    use core_graphics::event_source::{CGEventSource, CGEventSourceStateID};

    if text.trim().is_empty() {
        return Ok(());
    }

    // 1) Text onto the clipboard (NOT restored → the manual ⌘V fallback always works).
    let mut clipboard = arboard::Clipboard::new().map_err(|e| e.to_string())?;
    clipboard
        .set_text(text.to_owned())
        .map_err(|e| e.to_string())?;

    // 2) Re-activate the target app (Wispr: "performing paste after app activation").
    //    This way Cmd+V goes to the right place even if focus drifted in the
    //    meantime (e.g. the pill expanded). `open -b` = activate by bundle ID.
    if let Some(app) = target_app {
        if !app.is_empty() {
            let _ = std::process::Command::new("open")
                .args(["-b", app])
                .output();
            // Give the target app time to come to the front + the clipboard to settle.
            std::thread::sleep(std::time::Duration::from_millis(120));
        }
    } else {
        std::thread::sleep(std::time::Duration::from_millis(90));
    }

    // 3) Synthetic Cmd+V.
    let source = CGEventSource::new(CGEventSourceStateID::CombinedSessionState)
        .map_err(|_| "failed to create CGEventSource (Accessibility permission?)".to_string())?;
    const V_KEYCODE: core_graphics::event::CGKeyCode = 9; // 'v'

    let down = CGEvent::new_keyboard_event(source.clone(), V_KEYCODE, true)
        .map_err(|_| "keydown event failed".to_string())?;
    down.set_flags(CGEventFlags::CGEventFlagCommand);
    down.post(CGEventTapLocation::HID);

    let up = CGEvent::new_keyboard_event(source, V_KEYCODE, false)
        .map_err(|_| "keyup event failed".to_string())?;
    up.set_flags(CGEventFlags::CGEventFlagCommand);
    up.post(CGEventTapLocation::HID);

    Ok(())
}

#[cfg(not(target_os = "macos"))]
pub fn insert_text(_text: &str, _target_app: Option<&str>) -> Result<(), String> {
    Err("Inserting at the cursor is currently only supported on macOS.".to_string())
}
