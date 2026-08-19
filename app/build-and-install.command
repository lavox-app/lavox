#!/bin/bash
# Lavox Hub — build + telepítés (TELJES BUNDLE flow).
#
# ⚠️ KRITIKUS SZABÁLYOK (2026-07-03 + 2026-07-14 tanulságok):
#   1. SOHA ne cseréld csak a binárist egy meglévő .app-ban — a bundle
#      inkonzisztenssé válik és a WebView semmit nem renderel többé.
#      MINDIG: teljes `tauri build --bundles app` + TELJES .app másolás (cp -R)
#      + xattr -cr + codesign a STABIL certtel (így a TCC-engedélyek —
#      Kamera / Képernyőfelvétel / Bemeneti figyelés — megmaradnak).
#      Ad-hoc aláírt teszt-build ezek NÉLKÜL fut → a bar hover/gombok sem mennek!
#   2. A cargo target az iCloud-on KÍVÜL (/tmp) — az iCloud-szinkron beragasztja
#      a fájlműveleteket, és a lemezt sem tölti tartósan (reboot után ürül).
#   3. A models/*.bin iCloud-evicted (dataless) lehet → a bundle `os error 89`-cel
#      hal meg. A build ELŐTT materializálni kell (brctl download).
set -e
cd "$(dirname "$0")" || exit 1

# Alapértelmezett (self-signed "Hangar Dev") cert — a TCC-engedélyek ehhez kötődnek.
# Signing identity for the installed app. TCC permissions (mic, screen) stick
# to the signature — use a stable self-signed cert if you rebuild often
# (Keychain Access → Certificate Assistant → Code Signing). Falls back to
# ad-hoc signing, which works but resets permissions on each reinstall.
CERT="${LAVOX_CERT:-B32D83540C9FDEAD097FD55E589F9454677A8C1E}"
if ! security find-identity -p codesigning 2>/dev/null | grep -q "$CERT"; then
  echo "   (no signing cert found — using ad-hoc signature)"
  CERT="-"
fi
# ── Opcionális Developer ID út (Apple Developer Program) ──────────────────────
# Ha a LAVOX_SIGN_IDENTITY be van állítva (a Developer ID Application cert neve
# vagy SHA-1 hash-e), az aláírás hardened runtime + entitlements módban történik.
# Ha emellett a LAVOX_NOTARY_KEY_ID + LAVOX_NOTARY_KEY_ISSUER +
# LAVOX_NOTARY_KEY_PATH (App Store Connect API .p8 kulcs) is megvan,
# notarizáció + staple is fut. Ha a LAVOX_SIGN_IDENTITY NINCS beállítva,
# minden PONTOSAN a régi self-signed módon megy (TCC-engedélyek megmaradnak).
TARGET_DIR="/tmp/hangar-cargo-target"
APP_SRC="$TARGET_DIR/release/bundle/macos/Lavox Hub.app"
APP_DST="/Applications/Lavox Hub.app"

echo "== 0/6 Előfeltételek =="
# Lemez-ellenőrzés: a release build + bundle ~6 GB-ot igényel (modell NÉLKÜL —
# a modell az app-supportban él, NEM a bundle-ben; ld. lentebb).
FREE_GB=$(df -g / | awk 'NR==2 {print $4}')
if [ "$FREE_GB" -lt 6 ]; then
  echo "❌ Kevés a szabad hely: ${FREE_GB} GB (legalább 6 GB kell). Szabadíts fel helyet."
  exit 1
fi
# A whisper-modell NEM kerül a bundle-be (az iCloud folyton kilakoltatta a
# repo models/ példányát → a bundling örökké újratöltötte). A modell tartós,
# iCloud-MENTES helye: ~/Library/Application Support/live.plansmart.hangar/models
# — a find_model ezt keresi először, és a Settings letöltő-gombja is ide tölt.
MODEL_DIR="$HOME/Library/Application Support/live.plansmart.hangar/models"
if ! ls "$MODEL_DIR"/*.bin >/dev/null 2>&1; then
  echo "⚠️  Nincs modell a(z) $MODEL_DIR mappában — az app első indításkor a"
  echo "    Beállítások → Modell → Letöltés gombbal tölti le (1,6 GB)."
fi
echo "   ✓ lemez rendben"

# Google naptár-integráció: a desktop OAuth kliens FORDÍTÁSKOR ég be
# (gauth.rs option_env!). Ha nincs meg, a build lefut, csak a Beállítások azt
# írja majd, hogy a naptár nem elérhető — ezért itt hangosan jelezzük.
#
# Ajánlott indítás (a titok nem kerül a shell-előzménybe). FIGYELEM a két
# paraméterre: a Lavox-titkok a `/lavox` PATH-on élnek (nem a projekt
# gyökerében), és `dev` környezetben (az Infisical UI-ban "Development"):
#     infisical run --env=dev --path=/lavox -- ./build-and-install.command
# vagy kézzel: export LAVOX_DESKTOP_CLIENT_ID=... LAVOX_DESKTOP_CLIENT_SECRET=...
INFISICAL_HINT="infisical run --env=dev --path=/lavox -- ./build-and-install.command"
if [ -n "$LAVOX_DESKTOP_CLIENT_ID" ] && [ -n "$LAVOX_DESKTOP_CLIENT_SECRET" ]; then
  echo "   ✓ Google desktop-kliens megvan (id vége: ...${LAVOX_DESKTOP_CLIENT_ID: -6}) — a naptár be lesz kötve"
else
  echo "   ⚠️  Nincs LAVOX_DESKTOP_CLIENT_ID/SECRET a környezetben."
  echo "      A build folytatódik, de a NAPTÁR-INTEGRÁCIÓ KIMARAD ebből a buildből."
  echo "      Naptárral:  $INFISICAL_HINT"
fi

echo "== 1/5 Frontend build (vite) =="
node ./node_modules/vite/bin/vite.js build

echo "== 2/5 Tauri bundle build (lokális target: $TARGET_DIR) =="
# beforeBuildCommand üres a tauri.conf-ban — a vite build fent már lefutott
# (a beforeBuildCommand pipe-olt shellben hamis exit-hibát dob — tauri-cli quirk).
CARGO_TARGET_DIR="$TARGET_DIR" MACOSX_DEPLOYMENT_TARGET=13.0 \
  pnpm tauri build --bundles app

echo "== 3/5 Telepítés (TELJES bundle csere) =="
osascript -e 'quit app "Lavox Hub"' 2>/dev/null || true
osascript -e 'quit app "hangar"' 2>/dev/null || true
# A régi nevű app eltakarítása (az átnevezés után ne legyen két példány).
rm -rf "/Applications/hangar.app"
sleep 1
rm -rf "$APP_DST"
cp -R "$APP_SRC" "$APP_DST"

if [ -n "${LAVOX_SIGN_IDENTITY:-}" ]; then
  echo "== 4/5 Aláírás Developer ID-vel (hardened runtime + entitlements) =="
  xattr -cr "$APP_DST"
  # Belülről kifelé írunk alá: ELŐBB a beágyazott Mach-O helper (syscap),
  # UTÁNA a teljes .app. --deep SZÁNDÉKOSAN NINCS: az Apple deprecálta, és
  # felülírná a helper külön aláírását — belülről kifelé haladva felesleges.
  # (Ha a notarizáció valaha aláíratlan beágyazott dylib-re panaszkodna,
  # azt is itt, az .app aláírása ELŐTT kell külön aláírni.)
  codesign --force --options runtime --timestamp \
    --sign "$LAVOX_SIGN_IDENTITY" \
    "$APP_DST/Contents/Resources/helpers/syscap"
  codesign --force --options runtime --timestamp \
    --entitlements src-tauri/Entitlements.plist \
    --sign "$LAVOX_SIGN_IDENTITY" \
    "$APP_DST"

  if [ -n "${LAVOX_NOTARY_KEY_ID:-}" ] && [ -n "${LAVOX_NOTARY_KEY_ISSUER:-}" ] && [ -n "${LAVOX_NOTARY_KEY_PATH:-}" ]; then
    echo "== 5b/6 Notarizáció (notarytool + stapler) =="
    NOTARY_STAGE=$(mktemp -d)
    NOTARY_ZIP="$NOTARY_STAGE/LavoxHub-notary.zip"
    ditto -c -k --keepParent "$APP_DST" "$NOTARY_ZIP"
    xcrun notarytool submit "$NOTARY_ZIP" \
      --key "$LAVOX_NOTARY_KEY_PATH" \
      --key-id "$LAVOX_NOTARY_KEY_ID" \
      --issuer "$LAVOX_NOTARY_KEY_ISSUER" \
      --wait
    # A staple az /Applications-beli appra kerül — így a --release zip
    # (lentebb) már a VÉGSŐ, staplelt appból készül.
    xcrun stapler staple "$APP_DST"
    rm -rf "$NOTARY_STAGE"
    echo "   ✓ notarizálva + staplelve"
  else
    echo "   ⚠️  Nincs LAVOX_NOTARY_KEY_ID/ISSUER/PATH a környezetben —"
    echo "      a csomag ALÁÍRVA, de NOTARIZÁLATLANUL készül."
    echo "      (Más gépen a Gatekeeper első indításkor kifogásolhatja.)"
  fi
else
  echo "== 4/5 Aláírás a stabil certtel (TCC engedélyek megmaradnak) =="
  xattr -cr "$APP_DST"
  codesign --force --deep --sign "$CERT" --identifier live.plansmart.hangar "$APP_DST"
fi

echo "== 5/5 Indítás =="
open "$APP_DST"
echo ""
echo "== ✅ KÉSZ — teljes bundle telepítve és újraindítva =="
echo "(bezárhatod ezt az ablakot)"

# ── Opcionális release: a kész, ALÁÍRT .app felmásolása a VPS-re ──────────────
# Csak `--release` kapcsolóval fut:
#     infisical run --env=dev --path=/lavox -- ./build-and-install.command --release
#
# A `ditto` azért kell sima `zip` helyett, mert megőrzi a kódaláírást és az
# extended attribútumokat. Sima `zip`-pel a másik gépen a Gatekeeper elutasítja
# az appot. Lásd: docs/superpowers/specs/2026-08-12-hub-installer-design.md
if [ "${1:-}" = "--release" ]; then
  echo ""
  echo "== 7/7 Release feltöltés a VPS-re =="
  VERSION=$(date -u +%Y%m%d-%H%M)
  STAGE=$(mktemp -d)
  ZIP="$STAGE/LavoxHub.app.zip"

  ditto -c -k --keepParent "$APP_DST" "$ZIP"
  SHA=$(shasum -a 256 "$ZIP" | awk '{print $1}')
  SIZE=$(stat -f%z "$ZIP")
  printf '{"version":"%s","sha256":"%s","size":%s,"built_at":"%s"}\n' \
    "$VERSION" "$SHA" "$SIZE" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$STAGE/latest.json"

  ssh netcup "mkdir -p /root/lavox-releases/latest /root/lavox-releases/$VERSION"
  scp -q "$ZIP" "$STAGE/latest.json" netcup:/root/lavox-releases/latest/
  scp -q "$ZIP" "netcup:/root/lavox-releases/$VERSION/"
  scp -q installer/install-lavox-hub.command netcup:/root/lavox-releases/latest/

  rm -rf "$STAGE"
  echo "   ✓ verzió: $VERSION  ($(echo "scale=1; $SIZE/1048576" | bc) MB)"
  echo "   ✓ letöltés: https://api.lavox.cloud/releases/latest/install-lavox-hub.command"
fi
