import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import {
  isPermissionGranted,
  requestPermission,
  sendNotification,
} from "@tauri-apps/plugin-notification";

const AUTO_RECORD_KEY = "lavox-auto-record";

/** A naptár-kapcsolat állapota a RUST-oldali token-tárból.
 *  A refresh token ott él (0600-as fájl), nem localStorage-ban.
 *  `configured` = a build tartalmaz-e Google desktop-kliens azonosítót;
 *  enélkül a bejelentkezés-gombot nincs értelme megmutatni. */
export type CalendarStatus = {
  configured: boolean;
  connected: boolean;
  email: string | null;
};

export async function calendarStatus(): Promise<CalendarStatus> {
  try {
    return await invoke<CalendarStatus>("calendar_status");
  } catch {
    return { configured: false, connected: false, email: null };
  }
}

export async function clearAuth(): Promise<void> {
  await invoke("calendar_logout").catch(() => {});
}

export function loadAutoRecord(): boolean {
  return localStorage.getItem(AUTO_RECORD_KEY) === "true";
}

export function saveAutoRecord(enabled: boolean) {
  localStorage.setItem(AUTO_RECORD_KEY, String(enabled));
  invoke("set_auto_record", { enabled }).catch(() => {});
}

/** Natív bejelentkezés: a Rust nyitja a rendszer-böngészőt, elfogja a
 *  loopback-redirectet, és access+REFRESH tokenre vált. A hívás addig blokkol,
 *  amíg a user végigmegy a Google hozzájáruláson (vagy 3 perc után lejár). */
export async function googleLogin(): Promise<{ email: string | null }> {
  return invoke<{ email: string | null }>("calendar_login");
}

export async function syncTokenToBackend() {
  // A token a Rust-oldalon él és magától frissül; itt már csak az
  // auto-record beállítást kell szinkronban tartani.
  const autoRecord = loadAutoRecord();
  await invoke("set_auto_record", { enabled: autoRecord });
}

export type MeetingEvent = {
  id: string;
  title: string;
  start: string;
  end: string;
  seconds_until: number;
  duration_minutes: number;
};

export function listenMeetingStarting(
  callback: (meeting: MeetingEvent) => void
) {
  return listen<MeetingEvent>("meeting-starting", async (event) => {
    let granted = await isPermissionGranted();
    if (!granted) {
      const perm = await requestPermission();
      granted = perm === "granted";
    }
    if (granted) {
      const m = event.payload;
      const mins = Math.max(0, Math.round(m.seconds_until / 60));
      sendNotification({
        title: `Meeting: ${m.title}`,
        body:
          mins > 0
            ? `${mins} perc múlva indul (${m.duration_minutes} perces)`
            : `Most kezdődik (${m.duration_minutes} perces)`,
      });
    }
    callback(event.payload);
  });
}

export function listenAutoRecordStart(callback: (meeting: MeetingEvent) => void) {
  return listen<MeetingEvent>("auto-record-start", async (event) => {
    try {
      await invoke("start_meeting_record");
      callback(event.payload);
    } catch (e) {
      console.error("Auto-record indítás sikertelen:", e);
    }
  });
}
