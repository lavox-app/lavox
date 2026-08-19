fn main() {
    // A gauth.rs `option_env!`-fel FORDÍTÁSKOR égeti be a Google desktop-kliens
    // adatait. A cargo alapból nem tudja, hogy ezek az env-változók
    // befolyásolják a fordítást, ezért kulcs-cserénél állott cache-ből
    // dolgozna (és a régi/hiányzó kulcs maradna a bináriban). Ezek a
    // direktívák kényszerítik az újrafordítást, ha az érték változik.
    println!("cargo:rerun-if-env-changed=LAVOX_DESKTOP_CLIENT_ID");
    println!("cargo:rerun-if-env-changed=LAVOX_DESKTOP_CLIENT_SECRET");
    // helpers/syscap: Swift segéd a rendszerhang-felvételhez (ScreenCaptureKit).
    // A lefordított bináris nincs verziókövetésben, de a tauri.conf erőforrásként
    // várja — ha hiányzik vagy a forrás újabb nála, itt fordul le, hogy a puszta
    // `cargo build` / `cargo test` is működjön friss klónon. Temp-fájlba fordítunk
    // és atomikusan nevezünk át, hogy két párhuzamos build (pl. rust-analyzer
    // külön target-dirrel) ne írhasson tört Mach-O-t ugyanarra az útvonalra.
    // Nem-macOS targetnél kihagyjuk — ott a Tauri bundler ad értelmes hibát.
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
