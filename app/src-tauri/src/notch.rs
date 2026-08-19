//! Notch-detektálás (MacBook kijelző-bevágás) — a Dynamic Island-stílusú
//! „notch két oldalán" (compact) layouthoz. Ha nincs notch → fallback (lebegő pill).
//!
//! Forrás: `NSScreen.safeAreaInsets.top` (notch magasság) +
//! `auxiliaryTopLeftArea` / `auxiliaryTopRightArea` (a menüsor-sávok a notch két
//! oldalán) → a köztük lévő rés a notch vízszintes tartománya.

use serde::Serialize;

#[derive(Serialize, Clone, Default, Debug)]
pub struct NotchInfo {
    pub has_notch: bool,
    /// Notch magasság (logikai pont).
    pub notch_height: f64,
    /// A notch bal éle a képernyőn (logikai pont, a primary képernyő origójához).
    pub notch_left: f64,
    /// A notch jobb éle.
    pub notch_right: f64,
    /// A primary képernyő szélessége (logikai pont).
    pub screen_width: f64,
    pub scale: f64,
}

#[cfg(target_os = "macos")]
pub fn detect() -> NotchInfo {
    use objc2::MainThreadMarker;
    use objc2_app_kit::NSScreen;

    let Some(mtm) = MainThreadMarker::new() else {
        return NotchInfo::default();
    };
    let screens = NSScreen::screens(mtm);
    // A primary (menüsoros) képernyő = screens[0]. A pill is ide kerül.
    let Some(screen) = screens.firstObject() else {
        return NotchInfo::default();
    };
    let insets = screen.safeAreaInsets();
    if insets.top <= 0.0 {
        return NotchInfo::default(); // nincs notch → fallback
    }
    let frame = screen.frame();
    let left = screen.auxiliaryTopLeftArea(); // menüsor a notch BAL oldalán
    let right = screen.auxiliaryTopRightArea(); // menüsor a notch JOBB oldalán
    // A notch x-tartománya: a bal sáv jobb éle .. a jobb sáv bal éle.
    let notch_left = left.origin.x + left.size.width;
    let notch_right = right.origin.x;
    NotchInfo {
        has_notch: true,
        notch_height: insets.top,
        notch_left,
        notch_right,
        screen_width: frame.size.width,
        scale: screen.backingScaleFactor(),
    }
}

#[cfg(not(target_os = "macos"))]
pub fn detect() -> NotchInfo {
    NotchInfo::default()
}
