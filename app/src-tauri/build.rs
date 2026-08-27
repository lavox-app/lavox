fn main() {
    // gauth.rs bakes the Google desktop-client credentials in AT COMPILE TIME
    // via `option_env!`. Cargo does not know by default that these env vars
    // affect compilation, so after a key rotation it would build from a stale
    // cache (and the old/missing key would stay in the binary). These
    // directives force a rebuild whenever the value changes.
    println!("cargo:rerun-if-env-changed=LAVOX_DESKTOP_CLIENT_ID");
    println!("cargo:rerun-if-env-changed=LAVOX_DESKTOP_CLIENT_SECRET");
    // helpers/syscap: Swift helper for system-audio capture (ScreenCaptureKit).
    // The compiled binary is not under version control, but tauri.conf expects
    // it as a resource, if it is missing or the source is newer, it is built
    // here so a plain `cargo build` / `cargo test` works on a fresh clone. We
    // compile to a temp file and rename atomically, so two parallel builds
    // (e.g. rust-analyzer with its own target dir) cannot write a broken
    // Mach-O to the same path. Skipped on non-macOS targets, there the Tauri
    // bundler gives a meaningful error.
    println!("cargo:rerun-if-changed=helpers/syscap.swift");
    println!("cargo:rerun-if-changed=helpers/syscap");
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("macos") {
        let src = std::path::Path::new("helpers/syscap.swift");
        let bin = std::path::Path::new("helpers/syscap");
        let needs_build = match (bin.metadata(), src.metadata()) {
            (Err(_), _) => true,
            (Ok(b), Ok(s)) => matches!(
                (s.modified(), b.modified()),
                (Ok(src_m), Ok(bin_m)) if src_m > bin_m
            ),
            _ => false,
        };
        if needs_build {
            let tmp = format!("helpers/.syscap.{}.tmp", std::process::id());
            let status = std::process::Command::new("swiftc")
                .args([
                    "-O", "-target", "arm64-apple-macos13.0", "-parse-as-library",
                    "helpers/syscap.swift", "-o", &tmp,
                ])
                .status();
            match status {
                Err(err) => panic!(
                    "swiftc not found ({err}). Install the Xcode Command Line Tools \
                     (xcode-select --install) and retry."
                ),
                Ok(s) if !s.success() => {
                    let _ = std::fs::remove_file(&tmp);
                    panic!("compiling helpers/syscap.swift failed ({s})");
                }
                Ok(_) => std::fs::rename(&tmp, bin)
                    .expect("failed to move compiled syscap into place"),
            }
        }
    }

    tauri_build::build()
}
