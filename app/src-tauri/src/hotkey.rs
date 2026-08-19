//! Megbízható push-to-talk billentyű-figyelő — a Discord / Wispr módszer.
//!
//! A „Global Shortcut" plugin egyszeri gyorsbillentyűkre való, és a nyomva-tartós
//! push-to-talk ELENGEDÉSÉT chordnál megbízhatatlanul jelzi (ezért „nem áll le" a
//! felvétel). Ehelyett egy **CGEventTap**-pel közvetlenül látjuk MINDEN billentyű
//! le/felmenetelét, és magunk követjük a ⌘⇧Space állapotát → megbízható start+stop.
//!
//! ⚠️ macOS **Input Monitoring** (Bemeneti figyelés) engedély kell hozzá.

#[cfg(target_os = "macos")]
mod imp {
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{Mutex, OnceLock};
    use crate::hotkey::{combo_active, HotkeyCombo, KeyState};

    static APP: OnceLock<tauri::AppHandle> = OnceLock::new();
    static CTRL: AtomicBool = AtomicBool::new(false);
    static SHIFT: AtomicBool = AtomicBool::new(false);
    static ALT: AtomicBool = AtomicBool::new(false);
    static CMD: AtomicBool = AtomicBool::new(false);
    static FNKEY: AtomicBool = AtomicBool::new(false);
    static SPACE: AtomicBool = AtomicBool::new(false);
    static DICTATING: AtomicBool = AtomicBool::new(false);
    // A CGEventTap által figyelt aktív trigger-kombó (a lib.rs set_hotkey/get_hotkey
    // állítja; induláskor a persistált érték töltődik be — Fn default).
    pub(crate) static ACTIVE_COMBO: Mutex<Option<HotkeyCombo>> = Mutex::new(None);

    const SPACE_KEY: i64 = 49;

    fn emit(event: &str) {
        use tauri::{Emitter, Manager};
        if let Some(app) = APP.get() {
            if let Some(overlay) = app.get_webview_window("overlay") {
                let res = overlay.emit(event, ());
                crate::dbg(&format!("RUST_EMIT {event} ok={}", res.is_ok()));
            } else {
                crate::dbg(&format!("RUST_EMIT {event} NO_OVERLAY_WINDOW"));
            }
        } else {
            crate::dbg(&format!("RUST_EMIT {event} NO_APP"));
        }
    }

    /// A pillanatnyi billentyű-állapotot a KONFIGURÁLT kombóhoz méri (combo_active).
    /// Amíg a teljes kombó le van nyomva → diktálás; amint bármelyik eleme felmegy → stop.
    /// A figyelt kombó az ACTIVE_COMBO (Fn default); élőben cserélhető set_active_combo-val.
    fn update() {
        let state = KeyState {
            fn_: FNKEY.load(Ordering::Relaxed),
            ctrl: CTRL.load(Ordering::Relaxed),
            shift: SHIFT.load(Ordering::Relaxed),
            alt: ALT.load(Ordering::Relaxed),
            cmd: CMD.load(Ordering::Relaxed),
            space: SPACE.load(Ordering::Relaxed),
        };
        let combo = ACTIVE_COMBO
            .lock()
            .ok()
            .and_then(|g| g.clone())
            .unwrap_or_default();
        let all = combo_active(&combo, &state);
        if let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open("/tmp/lavox-ptt.log")
        {
            use std::io::Write;
            let _ = writeln!(
                f,
                "PTT_STATE fn={} ctrl={} shift={} alt={} cmd={} space={} all={all}",
                state.fn_, state.ctrl, state.shift, state.alt, state.cmd, state.space
            );
        }
        let was = DICTATING.load(Ordering::Relaxed);
        if all && !was {
            DICTATING.store(true, Ordering::Relaxed);
            emit("dictation-start");
        } else if !all && was {
            DICTATING.store(false, Ordering::Relaxed);
            emit("dictation-stop");
        }
    }

    pub fn start(app: tauri::AppHandle) {
        let _ = APP.set(app);
        // A persistált trigger-kombó betöltése (Fn default), ha még nincs beállítva.
        if let Ok(mut g) = ACTIVE_COMBO.lock() {
            if g.is_none() {
                *g = Some(crate::hotkey_load_for_tap());
            }
        }
        std::thread::spawn(|| {
            use core_foundation::runloop::CFRunLoop;
            use core_graphics::event::{
                CGEventFlags, CGEventTap, CGEventTapLocation, CGEventTapOptions,
                CGEventTapPlacement, CGEventType, CallbackResult, EventField,
            };

            // with_enabled: létrehozza a tapet, runloop-forrást ad, engedélyezi, majd
            // futtatja a megadott függvényt (a run loop blokkol, így a tap életben marad).
            let result = CGEventTap::with_enabled(
                CGEventTapLocation::HID,
                CGEventTapPlacement::HeadInsertEventTap,
                CGEventTapOptions::ListenOnly,
                vec![
                    CGEventType::KeyDown,
                    CGEventType::KeyUp,
                    CGEventType::FlagsChanged,
                ],
                |_proxy, etype, event| {
                    match etype {
                        CGEventType::KeyDown => {
                            let kc =
                                event.get_integer_value_field(EventField::KEYBOARD_EVENT_KEYCODE);
                            if kc == SPACE_KEY {
                                SPACE.store(true, Ordering::Relaxed);
                                update();
                            }
                        }
                        CGEventType::KeyUp => {
                            let kc =
                                event.get_integer_value_field(EventField::KEYBOARD_EVENT_KEYCODE);
                            if kc == SPACE_KEY {
                                SPACE.store(false, Ordering::Relaxed);
                                update();
                            }
                        }
                        CGEventType::FlagsChanged => {
                            let f = event.get_flags();
                            crate::dbg(&format!("RAW_FLAGS bits=0x{:x}", f.bits()));
                            CTRL.store(
                                f.contains(CGEventFlags::CGEventFlagControl),
                                Ordering::Relaxed,
                            );
                            SHIFT.store(
                                f.contains(CGEventFlags::CGEventFlagShift),
                                Ordering::Relaxed,
                            );
                            ALT.store(
                                f.contains(CGEventFlags::CGEventFlagAlternate),
                                Ordering::Relaxed,
                            );
                            CMD.store(
                                f.contains(CGEventFlags::CGEventFlagCommand),
                                Ordering::Relaxed,
                            );
                            // A Fn (🌐) a SecondaryFn flag (bit 0x800000).
                            FNKEY.store(
                                f.contains(CGEventFlags::CGEventFlagSecondaryFn),
                                Ordering::Relaxed,
                            );
                            update();
                        }
                        _ => {}
                    }
                    CallbackResult::Keep
                },
                || CFRunLoop::run_current(),
            );

            if result.is_err() {
                eprintln!(
                    "Lavox Hub: a billentyű-figyelő (CGEventTap) nem indult — engedélyezd az Input Monitoringot."
                );
            }
        });
    }
}

#[cfg(target_os = "macos")]
pub use imp::start;

#[cfg(not(target_os = "macos"))]
pub fn start(_app: tauri::AppHandle) {}

/// Egy diktálás-trigger kombó: modifierek + opcionális fő billentyű (keycode).
/// Fn-only trigger esetén `key == None`.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct HotkeyCombo {
    /// A megkövetelt modifierek, kisbetűs kanonikus névvel: "fn","ctrl","shift","alt","cmd".
    pub mods: Vec<String>,
    /// Opcionális fő billentyű neve (jelenleg csak "Space" támogatott), vagy None.
    pub key: Option<String>,
}

impl Default for HotkeyCombo {
    fn default() -> Self {
        // Alapértelmezés: Fn nyomva tartás (Wispr-stílus).
        HotkeyCombo { mods: vec!["fn".to_string()], key: None }
    }
}

/// A CGEventTap által figyelt aktív kombó beállítása (élő újrakonfigurálás — a
/// set_hotkey parancs hívja, nem kell app-újraindítás).
pub fn set_active_combo(combo: HotkeyCombo) {
    #[cfg(target_os = "macos")]
    {
        if let Ok(mut g) = imp::ACTIVE_COMBO.lock() {
            *g = Some(combo);
        }
    }
    #[cfg(not(target_os = "macos"))]
    let _ = combo;
}

/// A billentyűzet pillanatnyi állapota, amit a matcher kiértékel.
#[derive(Debug, Clone, Copy, Default)]
pub struct KeyState {
    pub fn_: bool,
    pub ctrl: bool,
    pub shift: bool,
    pub alt: bool,
    pub cmd: bool,
    pub space: bool,
}

/// Igaz, ha a `state` KIELÉGÍTI a `combo`-t: minden megkövetelt modifier le van nyomva,
/// az összes NEM megkövetelt modifier fel van engedve (nincs túllövés — a Ctrl+Shift
/// ne aktiválódjon a Ctrl+Shift+Alt-tól eltérő kombónál), és a fő billentyű (ha van) le van nyomva.
pub fn combo_active(combo: &HotkeyCombo, state: &KeyState) -> bool {
    let want = |name: &str| combo.mods.iter().any(|m| m == name);
    let (wf, wc, ws, wa, wm) = (want("fn"), want("ctrl"), want("shift"), want("alt"), want("cmd"));
    if state.fn_ != wf || state.ctrl != wc || state.shift != ws || state.alt != wa || state.cmd != wm {
        return false;
    }
    match combo.key.as_deref() {
        Some("Space") => state.space,
        Some(_) => false, // ismeretlen fő billentyű → sosem aktív (védelem)
        None => true,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn st(fn_: bool, ctrl: bool, shift: bool, alt: bool, cmd: bool, space: bool) -> KeyState {
        KeyState { fn_, ctrl, shift, alt, cmd, space }
    }

    #[test]
    fn fn_only_active_when_fn_down_and_nothing_else() {
        let c = HotkeyCombo::default(); // fn-only
        assert!(combo_active(&c, &st(true, false, false, false, false, false)));
    }

    #[test]
    fn fn_only_inactive_when_fn_up() {
        let c = HotkeyCombo::default();
        assert!(!combo_active(&c, &st(false, false, false, false, false, false)));
    }

    #[test]
    fn fn_only_inactive_when_extra_modifier_held() {
        // Túllövés-védelem: Fn+Ctrl ne aktiválja a tiszta Fn kombót.
        let c = HotkeyCombo::default();
        assert!(!combo_active(&c, &st(true, true, false, false, false, false)));
    }

    #[test]
    fn ctrl_shift_space_active_only_with_all_three() {
        let c = HotkeyCombo { mods: vec!["ctrl".into(), "shift".into()], key: Some("Space".into()) };
        assert!(combo_active(&c, &st(false, true, true, false, false, true)));
        assert!(!combo_active(&c, &st(false, true, true, false, false, false))); // space nélkül
        assert!(!combo_active(&c, &st(false, true, false, false, false, true)));  // shift nélkül
    }

    #[test]
    fn ctrl_shift_space_inactive_with_extra_cmd() {
        let c = HotkeyCombo { mods: vec!["ctrl".into(), "shift".into()], key: Some("Space".into()) };
        assert!(!combo_active(&c, &st(false, true, true, false, true, true)));
    }

    #[test]
    fn alt_cmd_required_and_active_only_when_both_down() {
        // wa/wm (alt/cmd) eddig csak "extra"-ként szerepeltek a tesztekben — itt
        // MEGKÖVETELT modifierként is lefedjük, hogy egy esetleges wa/wm-felcserélés
        // (transzkripciós hiba) buktasson.
        let c = HotkeyCombo { mods: vec!["alt".into(), "cmd".into()], key: None };
        assert!(combo_active(&c, &st(false, false, false, true, true, false)));
        assert!(!combo_active(&c, &st(false, false, false, true, false, false))); // cmd nélkül
        assert!(!combo_active(&c, &st(false, false, false, false, true, false))); // alt nélkül
    }

    #[test]
    fn unknown_key_never_active() {
        // A `Some(_) => false` fallback-ág — ismeretlen fő billentyű sosem aktív,
        // még akkor sem, ha minden modifier stimmel.
        let c = HotkeyCombo { mods: vec!["ctrl".into()], key: Some("Tab".into()) };
        assert!(!combo_active(&c, &st(false, true, false, false, false, false)));
    }
}
