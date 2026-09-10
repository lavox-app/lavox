//! Passive correction learning. After a dictation is inserted, this module
//! grabs the focused text field (Accessibility API, same permission the
//! synthetic Cmd+V already needs) and watches it for up to 90 seconds. If the
//! user edits the inserted sentence in place (a prompt box, an email draft)
//! and the text settles, the before/after pair is sent to the personal
//! dictionary. No extra click; the Edit button on the pill stays as a fallback.
//!
//! Privacy: only the region where the dictation landed is compared, anchored
//! by short context strings around it. Nothing is logged, and only the
//! (raw, corrected) sentence pair leaves this module, toward the local server.
//! Secure fields never expose AXValue, so they abort on the first read.

#![cfg(target_os = "macos")]

use std::ffi::c_void;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use core_foundation::base::{CFGetTypeID, CFRelease, CFTypeRef, TCFType};
use core_foundation::string::{CFString, CFStringGetTypeID, CFStringRef};

type AXUIElementRef = *const c_void;
type AXError = i32;
const AX_SUCCESS: AXError = 0;

#[link(name = "ApplicationServices", kind = "framework")]
extern "C" {
    fn AXUIElementCreateSystemWide() -> AXUIElementRef;
    fn AXUIElementCopyAttributeValue(
        element: AXUIElementRef,
        attribute: CFStringRef,
        value: *mut CFTypeRef,
    ) -> AXError;
}

/// AXUIElementRef holder that can cross into the watcher thread. All AX calls
/// happen on that one thread; the ref is released on drop.
struct AxElem(AXUIElementRef);
unsafe impl Send for AxElem {}
impl Drop for AxElem {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe { CFRelease(self.0 as CFTypeRef) };
        }
    }
}

fn copy_attr(element: AXUIElementRef, name: &str) -> Option<CFTypeRef> {
    let attr = CFString::new(name);
    let mut out: CFTypeRef = std::ptr::null();
    let err = unsafe {
        AXUIElementCopyAttributeValue(element, attr.as_concrete_TypeRef(), &mut out)
    };
    if err == AX_SUCCESS && !out.is_null() {
        Some(out)
    } else {
        None
    }
}

fn focused_element() -> Option<AxElem> {
    let system = unsafe { AXUIElementCreateSystemWide() };
    if system.is_null() {
        return None;
    }
    let focused = copy_attr(system, "AXFocusedUIElement");
    unsafe { CFRelease(system as CFTypeRef) };
    focused.map(|f| AxElem(f as AXUIElementRef))
}

/// The field's text, if the element exposes a string AXValue.
fn element_text(el: &AxElem) -> Option<String> {
    let value = copy_attr(el.0, "AXValue")?;
    let text = unsafe {
        if CFGetTypeID(value) == CFStringGetTypeID() {
            let s = CFString::wrap_under_create_rule(value as CFStringRef);
            Some(s.to_string())
        } else {
            CFRelease(value);
            None
        }
    };
    text
}

/// Locate `needle` in `hay`, tolerating the paste having normalized newlines.
fn find_inserted(hay: &str, needle: &str) -> Option<(usize, usize)> {
    if let Some(i) = hay.find(needle) {
        return Some((i, i + needle.len()));
    }
    let flat = needle.replace('\n', " ");
    if flat != needle {
        if let Some(i) = hay.find(&flat) {
            return Some((i, i + flat.len()));
        }
    }
    None
}

fn anchors(hay: &str, start: usize, end: usize, len: usize) -> (String, String) {
    let pre_from = hay[..start]
        .char_indices()
        .rev()
        .nth(len.saturating_sub(1))
        .map(|(i, _)| i)
        .unwrap_or(0);
    let post_to = hay[end..]
        .char_indices()
        .nth(len)
        .map(|(i, _)| end + i)
        .unwrap_or(hay.len());
    (hay[pre_from..start].to_string(), hay[end..post_to].to_string())
}

/// The corrected region between the two anchors, or None if either is gone.
fn region_between<'a>(hay: &'a str, pre: &str, post: &str) -> Option<&'a str> {
    let start = if pre.is_empty() { 0 } else { hay.rfind(pre)? + pre.len() };
    let rest = &hay[start..];
    let end = if post.is_empty() {
        rest.len()
    } else {
        rest.find(post)?
    };
    Some(&rest[..end])
}

static GENERATION: AtomicU64 = AtomicU64::new(0);

/// Start watching the field the dictation was just pasted into. A newer
/// dictation replaces any watcher that is still running.
pub fn watch_after_insert(inserted: String, learn: impl Fn(String, String) + Send + 'static) {
    if inserted.trim().chars().count() < 12 {
        return; // too short to learn from reliably
    }
    let generation = GENERATION.fetch_add(1, Ordering::SeqCst) + 1;

    std::thread::spawn(move || {
        // Let the paste land and focus settle. Right after the synthetic
        // Cmd+V the system may briefly report no focused element (or focus
        // is still on our pill), so ask patiently instead of giving up on
        // the first empty read.
        std::thread::sleep(Duration::from_millis(600));
        let mut field = None;
        for _ in 0..5 {
            if GENERATION.load(Ordering::SeqCst) != generation {
                return; // a newer dictation took over while waiting
            }
            if let Some(f) = focused_element() {
                field = Some(f);
                break;
            }
            std::thread::sleep(Duration::from_millis(700));
        }
        let Some(field) = field else { return };

        // The inserted text must be visible in the field, otherwise this field
        // is not readable for us (canvas editors, secure fields) and we stop.
        let mut located = None;
        for _ in 0..2 {
            if let Some(text) = element_text(&field) {
                if let Some((s, e)) = find_inserted(&text, &inserted) {
                    located = Some((text, s, e));
                    break;
                }
            }
            std::thread::sleep(Duration::from_millis(1200));
        }
        let Some((text, start, end)) = located else { return };
        let (mut pre, mut post) = anchors(&text, start, end, 24);
        let baseline = text[start..end].to_string();

        let mut last_seen: Option<String> = None;
        let deadline = std::time::Instant::now() + Duration::from_secs(90);
        while std::time::Instant::now() < deadline {
            std::thread::sleep(Duration::from_secs(3));
            if GENERATION.load(Ordering::SeqCst) != generation {
                return; // a newer dictation took over
            }
            let Some(now) = element_text(&field) else { return };

            // Untouched so far: refresh the anchors (surrounding text may grow).
            if let Some((s, e)) = find_inserted(&now, &inserted) {
                let (p, q) = anchors(&now, s, e, 24);
                pre = p;
                post = q;
                last_seen = None;
                continue;
            }

            let Some(current) = region_between(&now, &pre, &post) else { return };
            let current = current.to_string();
            let ratio = current.chars().count() as f32 / baseline.chars().count() as f32;
            if current.trim().is_empty() || !(0.25..=4.0).contains(&ratio) {
                return; // deleted wholesale or the anchors slid — nothing to learn
            }
            // Learn once the edit has been stable for two polls.
            if last_seen.as_deref() == Some(current.as_str()) {
                if current != baseline {
                    learn(baseline, current);
                }
                return;
            }
            last_seen = Some(current);
        }
    });
}
