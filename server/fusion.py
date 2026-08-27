"""Meet CC ↔ Whisper fusion.

Two independent, fallible sources:
  - Whisper: accurate text, but the diarization (SPEAKER_XX clusters)
    over-fragments on the mixed mono track and knows no names.
  - Meet CC: real speaker NAMES + timing, but the text is Google's ASR
    (weaker), and the caption appears DELAYED relative to the speech.

The fusion principle: the name is decided by time proximity AND text context
together, where the two sources' text matches (fuzzy), the CC name applies
with high weight; mere time overlap is only a weak vote. Majority name
assignment at the cluster level (which automatically merges over-fragmented
clusters), then segment-level override on a strong individual text match.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections import defaultdict

# The CC typically appears AFTER the speech, hence the asymmetric search window.
WINDOW_BEFORE = 4.0   # s: a caption this far before the segment start still counts
WINDOW_AFTER = 10.0   # s: a caption this far after the segment end still counts
CLUSTER_MIN_SCORE = 1.2   # total votes required to rename a cluster
CLUSTER_MIN_MARGIN = 1.3  # the winning name must beat the runner-up by this factor
SEGMENT_OVERRIDE_SIM = 0.55  # text similarity required for an individual override


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    # the áéíóöőúüű characters are functional data: they keep Hungarian
    # accented letters intact when normalizing Hungarian transcript text
    s = re.sub(r"[^\w\sáéíóöőúüű]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _sim(a: str, b: str) -> float:
    """Containment-aware similarity: the shorter contained in the longer → 1.0."""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if len(a) >= 8 and (a in b or b in a):
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _time_w(ev_t: float, start: float, end: float) -> float:
    """Time weight: 1.0 within the segment's (slightly extended) window,
    linear falloff outside."""
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
    """Assigns real names to whisper segments from the CC events.

    segments: [{start, end, text, speaker?}]  (state after diarization)
    speakers: [{id, label, is_me}] or None
    captions: [{t: rel_s, type: "caption"|"active-speaker", name, text}]

    Returns: (segments, speakers, stats), speaker fields rewritten where the
    fusion found a name; unidentified clusters remain unchanged.
    """
    cap_evs = [c for c in captions if c.get("type") == "caption" and (c.get("text") or "").strip()]
    act_evs = [c for c in captions if c.get("type") == "active-speaker" and c.get("name")]
    if not cap_evs and not act_evs:
        return segments, speakers, {"captions_used": 0, "named_segments": 0}

    # ACTIVE-SPEAKER-ONLY mode: Meet's "X is speaking" signals arrive from the
    # extension even WITHOUT captions enabled, so there is a name source even
    # without subtitles. In that case there is no text evidence, only time
    # overlap, so the cluster threshold is lower, but the winner-margin rule
    # (never fabricate a name) stays.
    cluster_min_score = CLUSTER_MIN_SCORE if cap_evs else 0.75

    # 1) Per-segment name votes (time × text context).
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
            # Text match dominates: time overlap by itself is only a weak signal.
            votes[ev["name"]] += tw * (0.25 + 0.75 * sim)
            if sim > best_sim[ev["name"]]:
                best_sim[ev["name"]] = sim
        for ev in act_evs:
            tw = _time_w(float(ev["t"]), seg["start"], seg["end"])
            if tw > 0.0:
                votes[ev["name"]] += 0.25 * tw
        seg_votes.append(dict(votes))
        seg_best_sim.append(dict(best_sim))

    # 2) Cluster → name (majority, with margin), merges fragmented clusters,
    #    since multiple clusters may map to the same name.
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
            continue  # no clear winner, better not to fabricate a name
        cluster_name[cl] = ranked[0][0]

    # 3) Segment-level assignment + strong individual override.
    named_segments = 0
    for seg, votes, best in zip(segments, seg_votes, seg_best_sim):
        cl = seg.get("speaker", "SPEAKER_00")
        name = cluster_name.get(cl)
        if best:
            top = max(best.items(), key=lambda kv: kv[1])
            if top[1] >= SEGMENT_OVERRIDE_SIM and votes.get(top[0], 0) >= 0.5:
                name = top[0]  # identified directly by the text context
        if name:
            seg["speaker"] = f"NAME_{_slug(name)}"
            seg["speaker_name"] = name
            named_segments += 1

    # 4) Rebuild the speakers list: named + remaining clusters.
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
