"""Name signals from the transcript text: self-introductions and addressing.

Deterministic (regex), LLM-free layer. Two signal types:

  1. SELF-INTRODUCTION — the speaker names their OWN cluster:
     "Kovács Péter vagyok", "itt Anna", "my name is John", "I'm Sarah"
  2. ADDRESSING — gives a (weak) signal for the OTHER cluster: if right
     after "Ádám, mit gondolsz?" ("Ádám, what do you think?") a different
     cluster speaks, that cluster is probably Ádám.

Safety rule: when a pool exists, a name may ONLY be assigned from the
candidate pool (calendar attendees, Meet participant list) — so a mistyped
or invented name structurally cannot get in. Without a pool, only
self-introduction is active (at least it is the speaker's own claim);
addressing is not.
"""

from __future__ import annotations

import re
import unicodedata

# Hungarian + English self-introduction patterns. The name is 1-3 capitalized
# words.
#
# IMPORTANT: the capitalization requirement (uppercase start) is the main
# defense against invented "names" (e.g. "itt van egy demo" must not yield a
# speaker named "Van Egy Demo"). re.IGNORECASE applies to the WHOLE pattern,
# so putting it on the keyword (itt/én/my name is) would also destroy the
# capitalization protection of the name part. Hence the case-insensitive
# keyword and the case-sensitive name are matched SEPARATELY — see _kw()
# below, which makes only the keyword part ignorecase.
#
# The Hungarian accented letters in _NAME and the Hungarian keywords below
# ("vagyok", "itt", "én") are functional data matched against Hungarian
# transcripts — do not translate them.
_NAME = r"([A-ZÁÉÍÓÖŐÚÜŰ][a-záéíóöőúüű]+(?:\s+[A-ZÁÉÍÓÖŐÚÜŰ][a-záéíóöőúüű]+){0,2})"


def _kw(word: str) -> str:
    """Case-insensitive keyword, BUT the following {_NAME} stays case-sensitive."""
    return "".join(f"[{c.upper()}{c.lower()}]" if c.isalpha() else re.escape(c) for c in word)


_INTRO_PATTERNS = [
    re.compile(rf"\b{_NAME}\s+{_kw('vagyok')}\b"),
    re.compile(rf"\b{_kw('itt')}\s+{_NAME}\b"),
    re.compile(rf"\b{_kw('én')}\s+{_NAME}\s+{_kw('vagyok')}\b"),
    re.compile(rf"\b{_kw('my name is')}\s+{_NAME}\b"),
    re.compile(rf"\b{_kw('I')}'?{_kw('m')}\s+{_NAME}\b"),
    re.compile(rf"\b{_kw('this is')}\s+{_NAME}\b"),
]
# Addressing: a name at the start of a sentence + comma + a question/request in the continuation.
_ADDRESS_PATTERN = re.compile(rf"(?:^|[.!?]\s+){_NAME}\s*,")

# Self-introductions are only searched in the first this-many seconds (later it would be noise).
INTRO_WINDOW_SEC = 180.0


def _deacc(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def _pool_match(name: str, pool: list[str] | None) -> str | None:
    """Aligns the extracted name to the candidate pool (accent/order tolerant).

    Without a pool the name is returned unchanged (we only call it that way
    for self-introductions) — when a pool exists, ONLY a pool name may be
    given out.
    """
    if pool is None:
        return name
    n_words = set(_deacc(name).split())
    for cand in pool:
        c_words = set(_deacc(cand).split())
        if n_words & c_words and (n_words <= c_words or c_words <= n_words):
            return cand  # the canonical form from the pool wins
    return None


def find_intro_votes(
    segments: list[dict],
    candidate_pool: list[str] | None = None,
) -> dict[str, dict[str, float]]:
    """Cluster → {name: vote} from the text signals.

    The votes carry weights compatible with fusion.py's cluster votes:
    self-introduction = strong (2.0), addressing follow-up = weak (0.5).
    """
    votes: dict[str, dict[str, float]] = {}

    def add(cluster: str, name: str, w: float) -> None:
        votes.setdefault(cluster, {})
        votes[cluster][name] = votes[cluster].get(name, 0.0) + w

    # 1) Self-introduction — the speaker names their own cluster.
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

    # 2) Addressing — only when a pool exists (too risky otherwise).
    if candidate_pool:
        for i, seg in enumerate(segments[:-1]):
            m = _ADDRESS_PATTERN.search(seg.get("text") or "")
            if not m:
                continue
            name = _pool_match(m.group(1).strip(), candidate_pool)
            if not name:
                continue
            nxt = segments[i + 1]
            # The addressed person is the NEXT speaker answering from a DIFFERENT cluster.
            if nxt.get("speaker") != seg.get("speaker") and nxt["start"] - seg["end"] < 6.0:
                add(nxt.get("speaker", ""), name, 0.5)

    return votes


# "beszélő" is functional data: it matches Hungarian generic speaker labels
# ("Beszélő 1") produced by the diarization pipeline. Do not translate.
_GENERIC = re.compile(r"^(speaker|beszélő)\s*\d+$", re.IGNORECASE)


def apply_intro_votes(
    segments: list[dict],
    speakers: list[dict] | None,
    candidate_pool: list[str] | None = None,
) -> tuple[list[dict] | None, dict]:
    """Writes the names derived from text signals onto the STILL UNNAMED clusters.

    Only renames clusters with a generic label ("Speaker N") — it NEVER
    overrides the result of higher layers (CC fusion, voice profile).
    Winner-margin rule: in doubtful cases it stays unnamed.
    """
    if not speakers:
        return speakers, {"renamed": 0}
    votes = find_intro_votes(segments, candidate_pool)
    renamed = 0
    for spk in speakers:
        if not _GENERIC.match((spk.get("label") or "").strip()):
            continue  # already has a real name — leave it alone
        v = votes.get(spk["id"])
        if not v:
            continue
        ranked = sorted(v.items(), key=lambda kv: kv[1], reverse=True)
        top_name, top_score = ranked[0]
        if top_score < 1.5:
            continue  # requires a self-introduction (2.0) or multiple concurring signals
        if len(ranked) > 1 and top_score < 1.3 * ranked[1][1]:
            continue  # no clear winner
        spk["label"] = top_name
        renamed += 1
    return speakers, {"renamed": renamed}
