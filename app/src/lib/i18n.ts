// UI-nyelv (felület nyelve) — gettext-minta: a kódban a MAGYAR string a forrás,
// a t() az EN szótárból fordít; ismeretlen kulcs → a magyar forrás jelenik meg.
// Default: ENGLISH (EN-first termék). Nyelvváltás = reload.
//
// Használat:
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
