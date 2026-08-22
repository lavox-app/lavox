#!/bin/bash
# Lavox Hub — build + install (FULL BUNDLE flow).
#
# ⚠️ CRITICAL RULES (lessons from 2026-07-03 + 2026-07-14):
#   1. NEVER swap just the binary inside an existing .app — the bundle
#      becomes inconsistent and the WebView stops rendering anything.
#      ALWAYS: full `tauri build --bundles app` + FULL .app copy (cp -R)
#      + xattr -cr + codesign with the STABLE cert (so the TCC permissions —
#      Camera / Screen Recording / Input Monitoring — are preserved).
#      An ad-hoc-signed test build runs WITHOUT them → even the bar
#      hover/buttons stop working!
#   2. Keep the cargo target OUTSIDE iCloud (/tmp) — iCloud sync stalls
#      file operations and doesn't persist the disk space either (it is
#      cleared after reboot).
#   3. models/*.bin may be iCloud-evicted (dataless) → the bundle dies with
#      `os error 89`. Materialize them BEFORE the build (brctl download).
set -e
cd "$(dirname "$0")" || exit 1

# Default (self-signed "Hangar Dev") cert — the TCC permissions are bound to it.
# Signing identity for the installed app. TCC permissions (mic, screen) stick
# to the signature — use a stable self-signed cert if you rebuild often
# (Keychain Access → Certificate Assistant → Code Signing). Falls back to
# ad-hoc signing, which works but resets permissions on each reinstall.
CERT="${LAVOX_CERT:-B32D83540C9FDEAD097FD55E589F9454677A8C1E}"
if ! security find-identity -p codesigning 2>/dev/null | grep -q "$CERT"; then
  echo "   (no signing cert found — using ad-hoc signature)"
  CERT="-"
fi
# ── Optional Developer ID path (Apple Developer Program) ──────────────────────
# If LAVOX_SIGN_IDENTITY is set (the name or SHA-1 hash of the Developer ID
# Application cert), signing happens in hardened runtime + entitlements mode.
# If, in addition, LAVOX_NOTARY_KEY_ID + LAVOX_NOTARY_KEY_ISSUER +
# LAVOX_NOTARY_KEY_PATH (App Store Connect API .p8 key) are present,
# notarization + stapling also runs. If LAVOX_SIGN_IDENTITY is NOT set,
# everything works EXACTLY as in the old self-signed mode (TCC permissions
# are preserved).
TARGET_DIR="/tmp/hangar-cargo-target"
APP_SRC="$TARGET_DIR/release/bundle/macos/Lavox Hub.app"
APP_DST="/Applications/Lavox Hub.app"

echo "== 0/6 Prerequisites =="
# Disk check: the release build + bundle needs ~6 GB (WITHOUT the model —
# the model lives in app support, NOT in the bundle; see below).
FREE_GB=$(df -g / | awk 'NR==2 {print $4}')
if [ "$FREE_GB" -lt 6 ]; then
  echo "❌ Not enough free space: ${FREE_GB} GB (at least 6 GB required). Free up some space."
  exit 1
fi
# The whisper model is NOT bundled (iCloud kept evicting the repo's models/
# copy → bundling kept re-downloading it forever). The model's permanent,
# iCloud-FREE location: ~/Library/Application Support/live.plansmart.hangar/models
# — find_model looks there first, and the Settings download button also
# downloads to that location.
MODEL_DIR="$HOME/Library/Application Support/live.plansmart.hangar/models"
if ! ls "$MODEL_DIR"/*.bin >/dev/null 2>&1; then
  echo "⚠️  No model in $MODEL_DIR — on first launch the app downloads it"
  echo "    via Settings → Model → Download (1.6 GB)."
fi
echo "   ✓ disk ok"

# Google Calendar integration: the desktop OAuth client is baked in AT COMPILE
# TIME (gauth.rs option_env!). Without it the build succeeds, but Settings
# will report the calendar as unavailable — hence the loud warning here.
#
# Recommended launch (keeps the secret out of shell history). MIND the two
# parameters: the Lavox secrets live under the `/lavox` PATH (not the project
# root), in the `dev` environment ("Development" in the Infisical UI):
#     infisical run --env=dev --path=/lavox -- ./build-and-install.command
# or manually: export LAVOX_DESKTOP_CLIENT_ID=... LAVOX_DESKTOP_CLIENT_SECRET=...
INFISICAL_HINT="infisical run --env=dev --path=/lavox -- ./build-and-install.command"
if [ -n "$LAVOX_DESKTOP_CLIENT_ID" ] && [ -n "$LAVOX_DESKTOP_CLIENT_SECRET" ]; then
  echo "   ✓ Google desktop client present (id ends in: ...${LAVOX_DESKTOP_CLIENT_ID: -6}) — calendar will be wired in"
else
  echo "   ⚠️  LAVOX_DESKTOP_CLIENT_ID/SECRET not in the environment."
  echo "      The build continues, but CALENDAR INTEGRATION IS LEFT OUT of this build."
  echo "      With calendar:  $INFISICAL_HINT"
fi

echo "== 1/5 Frontend build (vite) =="
node ./node_modules/vite/bin/vite.js build

echo "== 2/5 Tauri bundle build (local target: $TARGET_DIR) =="
# beforeBuildCommand is empty in tauri.conf — the vite build already ran above
# (beforeBuildCommand throws a spurious exit error in a piped shell — tauri-cli quirk).
CARGO_TARGET_DIR="$TARGET_DIR" MACOSX_DEPLOYMENT_TARGET=13.0 \
  pnpm tauri build --bundles app

echo "== 3/5 Install (FULL bundle replacement) =="
osascript -e 'quit app "Lavox Hub"' 2>/dev/null || true
osascript -e 'quit app "hangar"' 2>/dev/null || true
# Clean up the app under its old name (no duplicate copies after the rename).
rm -rf "/Applications/hangar.app"
sleep 1
rm -rf "$APP_DST"
cp -R "$APP_SRC" "$APP_DST"

if [ -n "${LAVOX_SIGN_IDENTITY:-}" ]; then
  echo "== 4/5 Signing with Developer ID (hardened runtime + entitlements) =="
  xattr -cr "$APP_DST"
  # Sign inside-out: FIRST the embedded Mach-O helper (syscap),
  # THEN the full .app. --deep is INTENTIONALLY absent: Apple deprecated it,
  # and it would overwrite the helper's separate signature — signing
  # inside-out makes it unnecessary anyway.
  # (If notarization ever complains about an unsigned embedded dylib,
  # sign it separately here too, BEFORE signing the .app.)
  codesign --force --options runtime --timestamp \
    --sign "$LAVOX_SIGN_IDENTITY" \
    "$APP_DST/Contents/Resources/helpers/syscap"
  codesign --force --options runtime --timestamp \
    --entitlements src-tauri/Entitlements.plist \
    --sign "$LAVOX_SIGN_IDENTITY" \
    "$APP_DST"

  if [ -n "${LAVOX_NOTARY_KEY_ID:-}" ] && [ -n "${LAVOX_NOTARY_KEY_ISSUER:-}" ] && [ -n "${LAVOX_NOTARY_KEY_PATH:-}" ]; then
    echo "== 5b/6 Notarization (notarytool + stapler) =="
    NOTARY_STAGE=$(mktemp -d)
    NOTARY_ZIP="$NOTARY_STAGE/LavoxHub-notary.zip"
    ditto -c -k --keepParent "$APP_DST" "$NOTARY_ZIP"
    xcrun notarytool submit "$NOTARY_ZIP" \
      --key "$LAVOX_NOTARY_KEY_PATH" \
      --key-id "$LAVOX_NOTARY_KEY_ID" \
      --issuer "$LAVOX_NOTARY_KEY_ISSUER" \
      --wait
    # The staple lands on the app in /Applications — so the --release zip
    # (below) is built from the FINAL, stapled app.
    xcrun stapler staple "$APP_DST"
    rm -rf "$NOTARY_STAGE"
    echo "   ✓ notarized + stapled"
  else
    echo "   ⚠️  LAVOX_NOTARY_KEY_ID/ISSUER/PATH not in the environment —"
    echo "      the package is built SIGNED but NOT NOTARIZED."
    echo "      (On another machine Gatekeeper may object on first launch.)"
  fi
else
  echo "== 4/5 Signing with the stable cert (TCC permissions preserved) =="
  xattr -cr "$APP_DST"
  codesign --force --deep --sign "$CERT" --identifier live.plansmart.hangar "$APP_DST"
fi

echo "== 5/5 Launch =="
open "$APP_DST"
echo ""
echo "== ✅ DONE — full bundle installed and relaunched =="
echo "(you can close this window)"

# ── Optional release: copy the finished, SIGNED .app up to the VPS ────────────
# Runs only with the `--release` flag:
#     infisical run --env=dev --path=/lavox -- ./build-and-install.command --release
#
# `ditto` is used instead of plain `zip` because it preserves the code
# signature and the extended attributes. With plain `zip`, Gatekeeper rejects
# the app on the other machine. See: docs/superpowers/specs/2026-08-12-hub-installer-design.md
if [ "${1:-}" = "--release" ]; then
  echo ""
  echo "== 7/7 Release upload to the VPS =="
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
  echo "   ✓ version: $VERSION  ($(echo "scale=1; $SIZE/1048576" | bc) MB)"
  echo "   ✓ download: https://api.lavox.cloud/releases/latest/install-lavox-hub.command"
fi
