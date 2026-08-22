// Dictation trigger combo: counterpart of get_hotkey/set_hotkey in Rust hotkey.rs / lib.rs.
import { invoke } from "@tauri-apps/api/core";

export type ModName = "fn" | "ctrl" | "shift" | "alt" | "cmd";

export interface HotkeyCombo {
  mods: ModName[];
  key: string | null; // currently only "Space" or null
}

export const DEFAULT_HOTKEY: HotkeyCombo = { mods: ["fn"], key: null };

// A few presets for the Settings quick buttons.
export const HOTKEY_PRESETS: { label: string; combo: HotkeyCombo }[] = [
  { label: "Fn", combo: { mods: ["fn"], key: null } },
  { label: "⌃⇧Space", combo: { mods: ["ctrl", "shift"], key: "Space" } },
  { label: "⌥Space", combo: { mods: ["alt"], key: "Space" } },
];

const MOD_SYMBOL: Record<ModName, string> = {
  fn: "Fn",
  ctrl: "⌃",
  shift: "⇧",
  alt: "⌥",
  cmd: "⌘",
};
const MOD_ORDER: ModName[] = ["fn", "ctrl", "alt", "shift", "cmd"];

/** Human-readable notation, e.g. {mods:["ctrl","shift"],key:"Space"} → "⌃⇧Space". */
export function formatCombo(c: HotkeyCombo): string {
  const mods = MOD_ORDER.filter((m) => c.mods.includes(m)).map((m) => MOD_SYMBOL[m]);
  const key = c.key ? c.key : "";
  const joined = mods.join("") + key;
  return joined || "—";
}

export async function getHotkey(): Promise<HotkeyCombo> {
  try {
    return await invoke<HotkeyCombo>("get_hotkey");
  } catch {
    return DEFAULT_HOTKEY;
  }
}

export async function setHotkey(combo: HotkeyCombo): Promise<void> {
  await invoke("set_hotkey", { combo });
}

/**
 * Browser-side capture: reads the modifiers + the main key from a KeyboardEvent.
 * The browser does NOT report Fn reliably → the user picks Fn via a preset
 * button; capture is for the other combos (Ctrl/Shift/Alt/Cmd + Space).
 */
export function comboFromEvent(e: KeyboardEvent): HotkeyCombo | null {
  const mods: ModName[] = [];
  if (e.ctrlKey) mods.push("ctrl");
  if (e.shiftKey) mods.push("shift");
  if (e.altKey) mods.push("alt");
  if (e.metaKey) mods.push("cmd");
  // Main key: currently only Space is supported by the backend.
  const key = e.code === "Space" ? "Space" : null;
  // At least one modifier OR a main key is required.
  if (mods.length === 0 && !key) return null;
  return { mods, key };
}
