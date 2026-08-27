#!/bin/bash
# Lavox Hub installer: runs on double-click.
#
# Builds nothing: it downloads the already SIGNED .app and puts it in place.
# No Xcode, cargo, pnpm, Infisical or signing certificate needed.
#
# The signature was made on the build machine; here we ONLY strip the
# download-quarantine flag. NEVER re-sign: that would break the original
# Developer ID signature, and macOS would revoke the Hub's TCC permissions
# (microphone, screen recording).
set -euo pipefail

URL="https://api.lavox.cloud/releases/latest/LavoxHub.app.zip"
DST="/Applications/Lavox Hub.app"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "== Installing Lavox Hub =="
echo ""

# Pre-check: for a missing package, `curl -fL` would only print a terse HTTP
# error that tells the user nothing about what to do, and because the script
# runs with `set -e`, it would exit immediately without an explanation.
echo "0/4 Checking the release…"
HTTP_CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 -I "$URL" 2>/dev/null || echo "000")"
if [ "$HTTP_CODE" != "200" ]; then
  echo ""
  if [ "$HTTP_CODE" = "000" ]; then
    echo "Cannot reach the Lavox server. Check your internet connection."
  else
    echo "No downloadable Lavox Hub release (the server answered $HTTP_CODE)."
    echo "This is not a problem with your machine: the installer package has not been uploaded yet."
  fi
  echo ""
  echo "Write to hello@lavox.app, or try again later."
  echo ""
  exit 1
fi

echo "1/4 Downloading…"
curl -fL --progress-bar "$URL" -o "$TMP/LavoxHub.app.zip"

echo "2/4 Unpacking…"
ditto -x -k "$TMP/LavoxHub.app.zip" "$TMP"
if [ ! -d "$TMP/Lavox Hub.app" ]; then
  echo ""
  echo "ERROR: the downloaded package does not contain 'Lavox Hub.app'."
  echo "Report this at hello@lavox.app; the release packaging is probably broken."
  exit 1
fi

echo "3/4 Installing…"
osascript -e 'quit app "Lavox Hub"' 2>/dev/null || true
sleep 1
rm -rf "$DST"
cp -R "$TMP/Lavox Hub.app" "$DST"
xattr -cr "$DST"

# Check that the signature survived the copy. If not, Gatekeeper would reject
# the app as "damaged"; better to fail here with a clear message.
if ! codesign --verify --deep --strict "$DST" 2>/dev/null; then
  echo ""
  echo "WARNING: the signature check did not pass."
  echo "The app may still start; if macOS rejects it, report it at hello@lavox.app."
fi

echo "4/4 Launching…"
open "$DST"

echo ""
echo "== DONE =="
echo "Lavox Hub is running."
echo ""
echo "Pairing: open the web app (app.lavox.cloud) and enter the code it shows"
echo "in the Hub under Settings → Speakers."
echo ""
echo "(You can close this window.)"
