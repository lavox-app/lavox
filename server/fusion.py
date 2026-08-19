"""Meet CC ↔ Whisper fúzió.

Két független, hibázó forrás:
  - Whisper: pontos szöveg, de a diarizáció (SPEAKER_XX klaszterek) a kevert
    mono sávon túltöredezik és nem tud neveket.
  - Meet CC: valódi beszélő-NEVEK + időzítés, de a szöveg a Google ASR-je
    (gyengébb), és a felirat KÉSVE jelenik meg a beszédhez képest.

A fúzió elve: a nevet az idő-közelség ÉS a szöveg-kontextus együtt dönti el —
ahol a két forrás szövege egyezik (fuzzy), ott a CC-név nagy súllyal érvényes;
puszta idő-átfedés csak gyenge szavazat. Klaszter-szinten többségi név-hozzá-
rendelés (ez a túltöredezett klasztereket automatikusan összevonja), majd
szegmens-szintű felülbírálat erős egyéni szöveg-egyezésnél.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections import defaultdict

# A CC tipikusan a beszéd UTÁN jelenik meg — a kereső-ablak ezért aszimmetrikus.
WINDOW_BEFORE = 4.0   # s: caption ennyivel a szegmens kezdete előtt még számít
WINDOW_AFTER = 10.0   # s: caption ennyivel a szegmens vége után még számít
CLUSTER_MIN_SCORE = 1.2   # klaszter-átnevezéshez szükséges össz-szavazat
CLUSTER_MIN_MARGIN = 1.3  # a győztes névnek ennyiszer kell vernie a másodikat
SEGMENT_OVERRIDE_SIM = 0.55  # egyéni felülbíráláshoz kellő szöveg-hasonlóság


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"[^\w\sáéíóöőúüű]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _sim(a: str, b: str) -> float:
    """Tartalmazás-tudatos hasonlóság: a rövidebb benne van a hosszabban → 1.0."""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if len(a) >= 8 and (a in b or b in a):
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _time_w(ev_t: float, start: float, end: float) -> float:
    """Idő-súly: a szegmens (kissé kitolt) ablakában 1.0, kifelé lineáris esés."""
    if start - 1.0 <= ev_t <= end + 4.0:
        return 1.0
    if ev_t < start - 1.0:
        d = (start - 1.0) - ev_t
        return max(0.0, 1.0 - d / WINDOW_BEFORE)
    d = ev_t - (end + 4.0)
    return max(0.0, 1.0 - d / WINDOW_AFTER)


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").upper()
    return s or "NAMED"


def fuse_captions(segments: list[dict], speakers: list[dict] | None, captions: list[dict]):
    """A whisper-szegmensekhez valódi neveket rendel a CC-eseményekből.

    segments: [{start, end, text, speaker?}]  (a diarizáció utáni állapot)
    speakers: [{id, label, is_me}] vagy None
    captions: [{t: rel_s, type: "caption"|"active-speaker", name, text}]

    Visszaad: (segments, speakers, stats) — a speaker-mezők átírva, ahol a
    fúzió nevet talált; a nem azonosított klaszterek változatlanok maradnak.
    """
    cap_evs = [c for c in captions if c.get("type") == "caption" and (c.get("text") or "").strip()]
    act_evs = [c for c in captions if c.get("type") == "active-speaker" and c.get("name")]
    if not cap_evs and not act_evs:
        return segments, speakers, {"captions_used": 0, "named_segments": 0}

    # ACTIVE-SPEAKER-ONLY mód: a Meet "X beszél" jelzései CC bekapcsolása
    # NÉLKÜL is érkeznek az extensionből — így felirat nélkül is van névforrás.
    # Ilyenkor nincs szöveg-evidencia, csak idő-átfedés, ezért a klaszter-küszöb
    # alacsonyabb, de a győztes-margó szabály (ne hazudjunk nevet) marad.
    cluster_min_score = CLUSTER_MIN_SCORE if cap_evs else 0.75

    # 1) Szegmensenkénti név-szavazatok (idő × szöveg-kontextus).
    seg_votes: list[dict[str, float]] = []
    seg_best_sim: list[dict[str, float]] = []
    for seg in segments:
        votes: dict[str, float] = defaultdict(float)
        best_sim: dict[str, float] = defaultdict(float)
        for ev in cap_evs:
            tw = _time_w(float(ev["t"]), seg["start"], seg["end"])
            if tw <= 0.0:
                continue
            sim = _sim(seg.get("text", ""), ev.get("text", ""))
            # A szöveg-egyezés dominál: idő-átfedés önmagában csak gyenge jel.
            votes[ev["name"]] += tw * (0.25 + 0.75 * sim)
            if sim > best_sim[ev["name"]]:
                best_sim[ev["name"]] = sim
        for ev in act_evs:
            tw = _time_w(float(ev["t"]), seg["start"], seg["end"])
            if tw > 0.0:
                votes[ev["name"]] += 0.25 * tw
        seg_votes.append(dict(votes))
        seg_best_sim.append(dict(best_sim))

    # 2) Klaszter → név (többségi, margóval) — összevonja a széttöredezett
    #    klasztereket, mert több klaszter is ugyanarra a névre képződhet.
    cluster_votes: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for seg, votes in zip(segments, seg_votes):
        cl = seg.get("speaker", "SPEAKER_00")
        for name, sc in votes.items():
            cluster_votes[cl][name] += sc
    cluster_name: dict[str, str] = {}
    for cl, votes in cluster_votes.items():
        ranked = sorted(votes.items(), key=lambda kv: kv[1], reverse=True)
        if not ranked or ranked[0][1] < cluster_min_score:
            continue
        if len(ranked) > 1 and ranked[0][1] < CLUSTER_MIN_MARGIN * ranked[1][1]:
            continue  # nincs egyértelmű győztes — inkább ne hazudjunk nevet
        cluster_name[cl] = ranked[0][0]

    # 3) Szegmens-szintű hozzárendelés + erős egyéni felülbírálat.
    named_segments = 0
    for seg, votes, best in zip(segments, seg_votes, seg_best_sim):
        cl = seg.get("speaker", "SPEAKER_00")
        name = cluster_name.get(cl)
        if best:
            top = max(best.items(), key=lambda kv: kv[1])
            if top[1] >= SEGMENT_OVERRIDE_SIM and votes.get(top[0], 0) >= 0.5:
                name = top[0]  # a szöveg-kontextus közvetlenül azonosította
        if name:
            seg["speaker"] = f"NAME_{_slug(name)}"
            seg["speaker_name"] = name
            named_segments += 1

    # 4) Speakers lista újraépítése: nevesített + megmaradt klaszterek.
    used_ids: dict[str, dict] = {}
    for seg in segments:
        sid = seg.get("speaker", "SPEAKER_00")
        if sid.startswith("NAME_"):
            used_ids.setdefault(sid, {"id": sid, "label": seg.get("speaker_name", sid), "is_me": False})
    old = {s["id"]: s for s in (speakers or [])}
    for seg in segments:
        sid = seg.get("speaker", "SPEAKER_00")
        if not sid.startswith("NAME_"):
            used_ids.setdefault(sid, old.get(sid, {"id": sid, "label": sid, "is_me": False}))
    new_speakers = list(used_ids.values())

    stats = {
        "captions_used": len(cap_evs) + len(act_evs),
        "named_segments": named_segments,
        "total_segments": len(segments),
        "clusters_named": len(cluster_name),
    }
    return segments, new_speakers, stats
