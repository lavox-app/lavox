import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import {
  isPermissionGranted,
  requestPermission,
  sendNotification,
} from "@tauri-apps/plugin-notification";

const AUTO_RECORD_KEY = "lavox-auto-record";

/** Calendar-connection status from the RUST-side token store.
 *  The refresh token lives there (0600 file), not in localStorage.
 *  `configured` = whether the build contains a Google desktop-client ID;
 *  without it there is no point showing the sign-in button. */
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

/** Native sign-in: Rust opens the system browser, intercepts the loopback
 *  redirect, and exchanges it for access+REFRESH tokens. The call blocks until
 *  the user completes the Google consent flow (or times out after 3 minutes). */
export async function googleLogin(): Promise<{ email: string | null }> {
  return invoke<{ email: string | null }>("calendar_login");
}

export async function syncTokenToBackend() {
  // The token lives on the Rust side and refreshes itself; here we only
  // need to keep the auto-record setting in sync.
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
            ? `Starts in ${mins} min (${m.duration_minutes} min)`
            : `Starting now (${m.duration_minutes} min)`,
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
      console.error("Failed to start auto-record:", e);
    }
  });
}
