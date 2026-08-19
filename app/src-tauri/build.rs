fn main() {
    // A gauth.rs `option_env!`-fel FORDÍTÁSKOR égeti be a Google desktop-kliens
    // adatait. A cargo alapból nem tudja, hogy ezek az env-változók
    // befolyásolják a fordítást, ezért kulcs-cserénél állott cache-ből
    // dolgozna (és a régi/hiányzó kulcs maradna a bináriban). Ezek a
    // direktívák kényszerítik az újrafordítást, ha az érték változik.
    println!("cargo:rerun-if-env-changed=LAVOX_DESKTOP_CLIENT_ID");
    println!("cargo:rerun-if-env-changed=LAVOX_DESKTOP_CLIENT_SECRET");
    tauri_build::build()
}
