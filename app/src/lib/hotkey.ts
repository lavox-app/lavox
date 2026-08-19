// Diktálás-trigger kombó: a Rust hotkey.rs / lib.rs get_hotkey/set_hotkey párja.
import { invoke } from "@tauri-apps/api/core";

export type ModName = "fn" | "ctrl" | "shift" | "alt" | "cmd";

export interface HotkeyCombo {
  mods: ModName[];
  key: string | null; // jelenleg csak "Space" vagy null
}

export const DEFAULT_HOTKEY: HotkeyCombo = { mods: ["fn"], key: null };

// Néhány preset a Settings gyors-gombokhoz.
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

/** Emberi jelölés, pl. {mods:["ctrl","shift"],key:"Space"} → "⌃⇧Space". */
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
 * Böngésző-oldali capture: egy KeyboardEvent-ből kiolvassa a modifiereket + a fő
 * billentyűt. A Fn-t a böngésző NEM adja meg megbízhatóan → a Fn-t preset-gombbal
 * választja a user, a capture a többi kombóra (Ctrl/Shift/Alt/Cmd + Space) való.
 */
export function comboFromEvent(e: KeyboardEvent): HotkeyCombo | null {
  const mods: ModName[] = [];
  if (e.ctrlKey) mods.push("ctrl");
  if (e.shiftKey) mods.push("shift");
  if (e.altKey) mods.push("alt");
  if (e.metaKey) mods.push("cmd");
  // Fő billentyű: jelenleg csak a Space támogatott a backendben.
  const key = e.code === "Space" ? "Space" : null;
  // Legalább egy modifier VAGY egy fő billentyű kell.
  if (mods.length === 0 && !key) return null;
  return { mods, key };
}
