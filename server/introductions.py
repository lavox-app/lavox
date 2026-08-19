"""Önbemutatkozás- és megszólalás-alapú név-jelek a transzkript szövegéből.

Determinisztikus (regex), LLM-mentes réteg. Két jel-típus:

  1. ÖNBEMUTATKOZÁS — a beszélő a SAJÁT klaszterének ad nevet:
     "Kovács Péter vagyok", "itt Anna", "my name is John", "I'm Sarah"
  2. MEGSZÓLÍTÁS — a MÁSIK klaszternek ad (gyenge) jelet: ha "Ádám, mit
     gondolsz?" után közvetlenül másik klaszter szólal meg, az valószínűleg Ádám.

Biztonsági szabály: név CSAK a candidate-poolból (naptár-résztvevők, Meet
résztvevő-lista) osztható ki, ha van pool — így elgépelt/kitalált név
strukturálisan nem kerülhet be. Pool nélkül csak az önbemutatkozás él
(az legalább a beszélő saját állítása), a megszólítás nem.
"""

from __future__ import annotations

import re
import unicodedata

# Magyar + angol önbemutatkozás-minták. A név 1-3 kapitalizált szó.
#
# FONTOS: a kapitalizáció-követelmény (nagybetűs kezdés) a fő védelem kitalált
# "nevek" ellen (pl. "itt van egy demo" ne legyen "Van Egy Demo" nevű beszélő).
# re.IGNORECASE az EGÉSZ mintára vonatkozik, tehát ha ráraknánk a kulcsszóra
# (itt/én/my name is), a névrész kapitalizáció-védelme is elveszne. Ezért a
# case-insensitive kulcsszót és a case-sensitive nevet KÜLÖN illesztjük — lásd
# _kw() lent, ami csak a kulcsszó-részt teszi ignorecase-szé.
_NAME = r"([A-ZÁÉÍÓÖŐÚÜŰ][a-záéíóöőúüű]+(?:\s+[A-ZÁÉÍÓÖŐÚÜŰ][a-záéíóöőúüű]+){0,2})"


def _kw(word: str) -> str:
    """Case-insensitive kulcsszó, DE a rákövetkező {_NAME} marad case-sensitive."""
    return "".join(f"[{c.upper()}{c.lower()}]" if c.isalpha() else re.escape(c) for c in word)


_INTRO_PATTERNS = [
    re.compile(rf"\b{_NAME}\s+{_kw('vagyok')}\b"),
    re.compile(rf"\b{_kw('itt')}\s+{_NAME}\b"),
    re.compile(rf"\b{_kw('én')}\s+{_NAME}\s+{_kw('vagyok')}\b"),
    re.compile(rf"\b{_kw('my name is')}\s+{_NAME}\b"),
    re.compile(rf"\b{_kw('I')}'?{_kw('m')}\s+{_NAME}\b"),
    re.compile(rf"\b{_kw('this is')}\s+{_NAME}\b"),
]
# Megszólítás: mondat elején álló név + vessző + kérdés/felszólítás-jel a folytatásban.
_ADDRESS_PATTERN = re.compile(rf"(?:^|[.!?]\s+){_NAME}\s*,")

# Az első ennyi másodpercben keresünk önbemutatkozást (utána már zaj lenne).
INTRO_WINDOW_SEC = 180.0


def _deacc(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def _pool_match(name: str, pool: list[str] | None) -> str | None:
    """A kinyert nevet a jelölt-poolhoz igazítja (ékezet/sorrend-toleráns).

    Pool nélkül a nevet változatlanul visszaadjuk (csak önbemutatkozásnál hívjuk
    így) — pool megléte esetén CSAK pool-beli név adható ki.
    """
    if pool is None:
        return name
    n_words = set(_deacc(name).split())
    for cand in pool:
        c_words = set(_deacc(cand).split())
        if n_words & c_words and (n_words <= c_words or c_words <= n_words):
            return cand  # a pool-beli kanonikus alak nyer
    return None


def find_intro_votes(
    segments: list[dict],
    candidate_pool: list[str] | None = None,
) -> dict[str, dict[str, float]]:
    """Klaszter → {név: szavazat} a szöveg-jelekből.

    A szavazatok a fusion.py klaszter-szavazataival kompatibilis súlyúak:
    önbemutatkozás = erős (2.0), megszólítás-követés = gyenge (0.5).
    """
    votes: dict[str, dict[str, float]] = {}

    def add(cluster: str, name: str, w: float) -> None:
        votes.setdefault(cluster, {})
        votes[cluster][name] = votes[cluster].get(name, 0.0) + w

    # 1) Önbemutatkozás — a beszélő a saját klaszterét nevezi meg.
    for seg in segments:
        if seg["start"] > INTRO_WINDOW_SEC:
            break
        text = seg.get("text") or ""
        for pat in _INTRO_PATTERNS:
            m = pat.search(text)
            if m:
                name = _pool_match(m.group(1).strip(), candidate_pool)
                if name:
                    add(seg.get("speaker", ""), name, 2.0)

    # 2) Megszólítás — csak pool megléte esetén (különben túl kockázatos).
    if candidate_pool:
        for i, seg in enumerate(segments[:-1]):
            m = _ADDRESS_PATTERN.search(seg.get("text") or "")
            if not m:
                continue
            name = _pool_match(m.group(1).strip(), candidate_pool)
            if not name:
                continue
            nxt = segments[i + 1]
            # A megszólított a KÖVETKEZŐ, MÁSIK klaszterből válaszoló beszélő.
            if nxt.get("speaker") != seg.get("speaker") and nxt["start"] - seg["end"] < 6.0:
                add(nxt.get("speaker", ""), name, 0.5)

    return votes


_GENERIC = re.compile(r"^(speaker|beszélő)\s*\d+$", re.IGNORECASE)


def apply_intro_votes(
    segments: list[dict],
    speakers: list[dict] | None,
    candidate_pool: list[str] | None = None,
) -> tuple[list[dict] | None, dict]:
    """A szöveg-jelekből származó neveket ráírja a MÉG NÉVTELEN klaszterekre.

    Csak generikus címkéjű ("Speaker N") klasztert nevez át — magasabb rétegek
    (CC-fúzió, voice-profil) eredményét SOHA nem írja felül. Győztes-margó
    szabály: kétes esetben inkább névtelen marad.
    """
    if not speakers:
        return speakers, {"renamed": 0}
    votes = find_intro_votes(segments, candidate_pool)
    renamed = 0
    for spk in speakers:
        if not _GENERIC.match((spk.get("label") or "").strip()):
            continue  # már van valódi neve — nem nyúlunk hozzá
        v = votes.get(spk["id"])
        if not v:
            continue
        ranked = sorted(v.items(), key=lambda kv: kv[1], reverse=True)
        top_name, top_score = ranked[0]
        if top_score < 1.5:
            continue  # önbemutatkozás (2.0) vagy több egybehangzó jel kell
        if len(ranked) > 1 and top_score < 1.3 * ranked[1][1]:
            continue  # nincs egyértelmű győztes
        spk["label"] = top_name
        renamed += 1
    return speakers, {"renamed": renamed}
