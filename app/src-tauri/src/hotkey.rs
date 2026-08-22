//! Reliable push-to-talk key listener — the Discord / Wispr approach.
//!
//! The "Global Shortcut" plugin is meant for one-shot hotkeys, and for a
//! hold-to-talk chord it reports the RELEASE unreliably (which is why the
//! recording "never stops"). Instead, a **CGEventTap** lets us see EVERY key's
//! down/up directly, and we track the ⌘⇧Space state ourselves → reliable start+stop.
//!
//! ⚠️ Requires the macOS **Input Monitoring** permission.

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
    // The active trigger combo watched by the CGEventTap (set via lib.rs
    // set_hotkey/get_hotkey; on startup the persisted value is loaded — Fn default).
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

    /// Measures the current key state against the CONFIGURED combo (combo_active).
    /// While the full combo is held → dictation; as soon as any part goes up → stop.
    /// The watched combo is ACTIVE_COMBO (Fn default); swappable live via set_active_combo.
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
        // Load the persisted trigger combo (Fn default) if not set yet.
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

            // with_enabled: creates the tap, adds a runloop source, enables it, then
            // runs the given function (the run loop blocks, keeping the tap alive).
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
                            // Fn (🌐) is the SecondaryFn flag (bit 0x800000).
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
                    "Lavox Hub: the key listener (CGEventTap) did not start — enable Input Monitoring."
                );
            }
        });
    }
}

#[cfg(target_os = "macos")]
pub use imp::start;

#[cfg(not(target_os = "macos"))]
pub fn start(_app: tauri::AppHandle) {}

/// A dictation-trigger combo: modifiers + optional main key (keycode).
/// For an Fn-only trigger, `key == None`.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct HotkeyCombo {
    /// The required modifiers, by lowercase canonical name: "fn","ctrl","shift","alt","cmd".
    pub mods: Vec<String>,
    /// Optional main key name (currently only "Space" is supported), or None.
    pub key: Option<String>,
}

impl Default for HotkeyCombo {
    fn default() -> Self {
        // Default: hold Fn (Wispr style).
        HotkeyCombo { mods: vec!["fn".to_string()], key: None }
    }
}

/// Sets the active combo watched by the CGEventTap (live reconfiguration —
/// called by the set_hotkey command, no app restart needed).
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

/// The keyboard's current state, evaluated by the matcher.
#[derive(Debug, Clone, Copy, Default)]
pub struct KeyState {
    pub fn_: bool,
    pub ctrl: bool,
    pub shift: bool,
    pub alt: bool,
    pub cmd: bool,
    pub space: bool,
}

/// True if `state` SATISFIES `combo`: every required modifier is down, every
/// NON-required modifier is up (no overshoot — Ctrl+Shift must not activate
/// on Ctrl+Shift+Alt), and the main key (if any) is down.
pub fn combo_active(combo: &HotkeyCombo, state: &KeyState) -> bool {
    let want = |name: &str| combo.mods.iter().any(|m| m == name);
    let (wf, wc, ws, wa, wm) = (want("fn"), want("ctrl"), want("shift"), want("alt"), want("cmd"));
    if state.fn_ != wf || state.ctrl != wc || state.shift != ws || state.alt != wa || state.cmd != wm {
        return false;
    }
    match combo.key.as_deref() {
        Some("Space") => state.space,
        Some(_) => false, // unknown main key → never active (safety)
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
        // Overshoot protection: Fn+Ctrl must not activate the plain Fn combo.
        let c = HotkeyCombo::default();
        assert!(!combo_active(&c, &st(true, true, false, false, false, false)));
    }

    #[test]
    fn ctrl_shift_space_active_only_with_all_three() {
        let c = HotkeyCombo { mods: vec!["ctrl".into(), "shift".into()], key: Some("Space".into()) };
        assert!(combo_active(&c, &st(false, true, true, false, false, true)));
        assert!(!combo_active(&c, &st(false, true, true, false, false, false))); // without space
        assert!(!combo_active(&c, &st(false, true, false, false, false, true)));  // without shift
    }

    #[test]
    fn ctrl_shift_space_inactive_with_extra_cmd() {
        let c = HotkeyCombo { mods: vec!["ctrl".into(), "shift".into()], key: Some("Space".into()) };
        assert!(!combo_active(&c, &st(false, true, true, false, true, true)));
    }

    #[test]
    fn alt_cmd_required_and_active_only_when_both_down() {
        // wa/wm (alt/cmd) only appeared as "extras" in the tests so far — here
        // we cover them as REQUIRED modifiers too, so an accidental wa/wm swap
        // (a transcription slip) would fail the test.
        let c = HotkeyCombo { mods: vec!["alt".into(), "cmd".into()], key: None };
        assert!(combo_active(&c, &st(false, false, false, true, true, false)));
        assert!(!combo_active(&c, &st(false, false, false, true, false, false))); // without cmd
        assert!(!combo_active(&c, &st(false, false, false, false, true, false))); // without alt
    }

    #[test]
    fn unknown_key_never_active() {
        // The `Some(_) => false` fallback arm — an unknown main key is never
        // active, even when all modifiers match.
        let c = HotkeyCombo { mods: vec!["ctrl".into()], key: Some("Tab".into()) };
        assert!(!combo_active(&c, &st(false, true, false, false, false, false)));
    }
}
