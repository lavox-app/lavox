# Lavox Meet Capture (Chrome extension)

Captures Google Meet **captions, participants and active-speaker events** and hands
them to Lavox Hub (`127.0.0.1:5192`) and the Lavox web app. It is the source of
speaker *names* in Meet calls; the Hub records the audio itself and fuses the two.

Nothing leaves the browser except to `127.0.0.1` and the Lavox web app origin.

## Install (unpacked)

1. Open `chrome://extensions` and enable **Developer mode**.
2. Click **Load unpacked** and pick this folder.
3. Join a Meet call and turn on captions (CC). The badge turns green once captions
   are being captured; the popup shows the live status.

## Notes

- Some UI-label lists in `meet-observer.js` deliberately contain both Hungarian and
  English Google Meet strings: they filter out control labels in both locales.
- Stored meeting events older than 48 hours are cleaned up automatically.
