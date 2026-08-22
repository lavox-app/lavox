//! Notch detection (MacBook display cutout) — for the Dynamic Island-style
//! "either side of the notch" (compact) layout. No notch → fallback (floating pill).
//!
//! Source: `NSScreen.safeAreaInsets.top` (notch height) +
//! `auxiliaryTopLeftArea` / `auxiliaryTopRightArea` (the menu-bar strips on
//! either side of the notch) → the gap between them is the notch's horizontal range.

use serde::Serialize;

#[derive(Serialize, Clone, Default, Debug)]
pub struct NotchInfo {
    pub has_notch: bool,
    /// Notch height (logical points).
    pub notch_height: f64,
    /// Left edge of the notch on screen (logical points, relative to the primary screen's origin).
    pub notch_left: f64,
    /// Right edge of the notch.
    pub notch_right: f64,
    /// Width of the primary screen (logical points).
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
    // The primary screen (with the menu bar) = screens[0]. The pill goes there too.
    let Some(screen) = screens.firstObject() else {
        return NotchInfo::default();
    };
    let insets = screen.safeAreaInsets();
    if insets.top <= 0.0 {
        return NotchInfo::default(); // no notch → fallback
    }
    let frame = screen.frame();
    let left = screen.auxiliaryTopLeftArea(); // menu bar LEFT of the notch
    let right = screen.auxiliaryTopRightArea(); // menu bar RIGHT of the notch
    // The notch's x range: right edge of the left strip .. left edge of the right strip.
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
