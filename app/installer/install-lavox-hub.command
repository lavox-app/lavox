#!/bin/bash
# Lavox Hub telepítő — dupla-kattintásra fut.
#
# Nem fordít semmit: a már ALÁÍRT .app-ot tölti le és teszi a helyére.
# Nem kell hozzá Xcode, cargo, pnpm, Infisical vagy aláíró tanúsítvány.
#
# Az aláírás a build gépen történt; itt CSAK a letöltés-karantén jelzőt vesszük le.
# Újra-aláírni TILOS — az elrontaná az eredeti Developer ID aláírást, és a
# macOS visszavonná a Hub TCC-engedélyeit (mikrofon, képernyőfelvétel).
set -euo pipefail

URL="https://api.lavox.cloud/releases/latest/LavoxHub.app.zip"
DST="/Applications/Lavox Hub.app"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "== Lavox Hub telepítése =="
echo ""

# Előellenőrzés: hiányzó csomagra a `curl -fL` csak szűkszavú HTTP-hibát adna,
# amiből a felhasználó nem tudja, mi a teendő — és mivel a script `set -e`-vel
# fut, azonnal ki is lépne magyarázat nélkül.
echo "0/4 Kiadás ellenőrzése…"
HTTP_CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 -I "$URL" 2>/dev/null || echo "000")"
if [ "$HTTP_CODE" != "200" ]; then
  echo ""
  if [ "$HTTP_CODE" = "000" ]; then
    echo "Nem érem el a Lavox szervert. Ellenőrizd az internetkapcsolatot."
  else
    echo "Nincs letölthető Lavox Hub kiadás (a szerver $HTTP_CODE választ adott)."
    echo "Ez nem a te géped hibája — a telepítőcsomag még nincs feltöltve."
  fi
  echo ""
  echo "Írj a hello@lavox.app címre, vagy próbáld újra később."
  echo ""
  exit 1
fi

echo "1/4 Letöltés…"
curl -fL --progress-bar "$URL" -o "$TMP/LavoxHub.app.zip"

echo "2/4 Kicsomagolás…"
ditto -x -k "$TMP/LavoxHub.app.zip" "$TMP"
if [ ! -d "$TMP/Lavox Hub.app" ]; then
  echo ""
  echo "HIBA: a letöltött csomag nem tartalmaz 'Lavox Hub.app'-ot."
  echo "Szólj Dávidnak — valószínűleg a release-csomagolás hibás."
  exit 1
fi

echo "3/4 Telepítés…"
osascript -e 'quit app "Lavox Hub"' 2>/dev/null || true
sleep 1
rm -rf "$DST"
cp -R "$TMP/Lavox Hub.app" "$DST"
xattr -cr "$DST"

# Ellenőrizzük, hogy az aláírás túlélte a másolást. Ha nem, a Gatekeeper
# "sérült alkalmazás" hibával elutasítaná — jobb itt, érthetően elbukni.
if ! codesign --verify --deep --strict "$DST" 2>/dev/null; then
  echo ""
  echo "FIGYELEM: az aláírás-ellenőrzés nem ment át."
  echo "Az app elindulhat, de ha a macOS elutasítja, szólj Dávidnak."
fi

echo "4/4 Indítás…"
open "$DST"

echo ""
echo "== KÉSZ =="
echo "A Lavox Hub elindult."
echo ""
echo "Párosítás: nyisd meg a webappot (app.lavox.cloud), és a megjelenő"
echo "kódot írd be a Hub Beállítások → Beszélők panelén."
echo ""
echo "(Ez az ablak bezárható.)"
