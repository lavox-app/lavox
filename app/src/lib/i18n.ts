// UI language — gettext pattern: the HUNGARIAN string in the code is the
// source (and the dictionary key), t() translates from the EN dictionary;
// unknown key → the Hungarian source is shown. Because the Hungarian strings
// act as lookup keys, they must stay byte-identical wherever t() is called.
// Default: ENGLISH (EN-first product). Switching language = reload.
//
// Usage:
//   import { t } from "./lib/i18n";
//   <button>{t("Nyelv")}</button>
//   t("Kész — {n} szegmens").replace("{n}", String(n))

import { EN } from "./i18n-en";

export type UiLang = "en" | "hu";

const STORAGE_KEY = "lavox-ui-lang";

export function getUiLang(): UiLang {
  try {
    return localStorage.getItem(STORAGE_KEY) === "hu" ? "hu" : "en";
  } catch {
    return "en";
  }
}

export function setUiLang(lang: UiLang): void {
  localStorage.setItem(STORAGE_KEY, lang);
  window.location.reload();
}

const LANG: UiLang = getUiLang();

export function t(hu: string): string {
  return LANG === "hu" ? hu : EN[hu] ?? hu;
}
