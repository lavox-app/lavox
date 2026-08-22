"""
Speaker diarization + enrollment — sherpa-onnx (ONNX runtime, PyTorch-free).

Engine: pyannote segmentation-3.0 (ONNX) + NeMo TitaNet-large speaker embedding.
Benchmark (2026-07-08, 100s HU test, 3 speakers): 98.1% frame accuracy,
490 MB peak RAM, RTF 0.08 — details in STATUS.md.

Multi-tenant: the enrollment profiles live in a separate directory per
workspace (SPEAKER_DB_DIR/<workspace_id>/); no workspace can see another's.
"""

import json
import os
import re
import threading
import uuid

import numpy as np
import sherpa_onnx

MODELS_DIR = os.environ.get("DIARIZE_MODELS_DIR", os.path.join(os.path.dirname(__file__), "models"))
SEG_MODEL = os.path.join(MODELS_DIR, "sherpa-onnx-pyannote-segmentation-3-0", "model.onnx")
EMBED_MODEL = os.path.join(MODELS_DIR, "nemo_en_titanet_large.onnx")
SPEAKER_DB_DIR = os.environ.get("SPEAKER_DB_DIR", os.path.join(os.path.dirname(__file__), "data", "speakers"))

# Enrollment match threshold. Measurement: real match 0.92+, worst false 0.54.
SIM_THRESHOLD = float(os.environ.get("DIARIZE_SIM_THRESHOLD", "0.60"))
# Clustering threshold (when num_speakers is unknown).
CLUSTER_THRESHOLD = float(os.environ.get("DIARIZE_CLUSTER_THRESHOLD", "0.5"))
# The embedding of segments shorter than this is unreliable → temporal smoothing.
MIN_CONFIDENT_SEC = 1.0

SAMPLE_RATE = 16000

_WS_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def models_available() -> bool:
    return os.path.isfile(SEG_MODEL) and os.path.isfile(EMBED_MODEL)


class Diarizer:
    """sherpa-onnx diarization + embedding extraction, loaded once per process."""

    def __init__(self, num_threads: int = 2):
        if not models_available():
            raise RuntimeError(
                f"Diarization models are missing: {SEG_MODEL} / {EMBED_MODEL} — "
                "see server/download_models.sh"
            )
        self._lock = threading.Lock()
        self._num_threads = num_threads
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=EMBED_MODEL, num_threads=num_threads)
        )

    def _make_sd(self, num_clusters: int) -> sherpa_onnx.OfflineSpeakerDiarization:
        # The cluster count is fixed in the config, so we instantiate per
        # request; the ONNX runtime reads the model files from the OS cache,
        # which is cheap.
        return sherpa_onnx.OfflineSpeakerDiarization(
            sherpa_onnx.OfflineSpeakerDiarizationConfig(
                segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                    pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=SEG_MODEL),
                    num_threads=self._num_threads,
                ),
                embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                    model=EMBED_MODEL, num_threads=self._num_threads
                ),
                clustering=sherpa_onnx.FastClusteringConfig(
                    num_clusters=num_clusters,
                    threshold=CLUSTER_THRESHOLD if num_clusters == -1 else 0,
                ),
                min_duration_on=0.2,
                min_duration_off=0.5,
            )
        )

    def diarize(self, samples: np.ndarray, num_speakers: int = -1) -> list[dict]:
        """16 kHz mono float32 → [{start, end, cluster}] sorted by time."""
        with self._lock:
            sd = self._make_sd(num_speakers)
            result = sd.process(samples).sort_by_start_time()
        return [{"start": r.start, "end": r.end, "cluster": r.speaker} for r in result]

    def embed(self, samples: np.ndarray) -> np.ndarray:
        with self._lock:
            stream = self._extractor.create_stream()
            stream.accept_waveform(SAMPLE_RATE, samples)
            stream.input_finished()
            emb = np.array(self._extractor.compute(stream), dtype=np.float32)
        norm = np.linalg.norm(emb)
        return emb / norm if norm > 0 else emb


# ---------------------------------------------------------------------------
# Enrollment store (isolated per workspace)
# ---------------------------------------------------------------------------

def _ws_dir(workspace: str) -> str:
    if not _WS_RE.match(workspace):
        raise ValueError("Invalid workspace identifier (A-Za-z0-9_- , max 64).")
    return os.path.join(SPEAKER_DB_DIR, workspace)


def _profile_path(workspace: str, speaker_id: str) -> str:
    if not _WS_RE.match(speaker_id):
        raise ValueError("Invalid speaker identifier.")
    return os.path.join(_ws_dir(workspace), f"{speaker_id}.json")


def load_profiles(workspace: str) -> list[dict]:
    d = _ws_dir(workspace)
    if not os.path.isdir(d):
        return []
    profiles = []
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".json"):
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                profiles.append(json.load(f))
    return profiles


def save_speaker(workspace: str, name: str, is_me: bool, embedding: np.ndarray) -> dict:
    """New profile, or for an existing name, append a new sample to it."""
    d = _ws_dir(workspace)
    os.makedirs(d, exist_ok=True)
    for p in load_profiles(workspace):
        if p["name"].strip().lower() == name.strip().lower():
            p["embeddings"].append(embedding.tolist())
            p["is_me"] = is_me
            with open(_profile_path(workspace, p["id"]), "w", encoding="utf-8") as f:
                json.dump(p, f, ensure_ascii=False)
            return p
    profile = {
        "id": f"spk_{uuid.uuid4().hex[:8]}",
        "name": name.strip(),
        "is_me": is_me,
        "embeddings": [embedding.tolist()],
    }
    with open(_profile_path(workspace, profile["id"]), "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False)
    return profile


def delete_speaker(workspace: str, speaker_id: str) -> bool:
    path = _profile_path(workspace, speaker_id)
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def _centroid(embeddings: list[list[float]]) -> np.ndarray:
    c = np.mean(np.array(embeddings, dtype=np.float32), axis=0)
    norm = np.linalg.norm(c)
    return c / norm if norm > 0 else c


def _normed(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def profile_similarity(profile: dict, cluster_emb: np.ndarray) -> float:
    """Cluster↔profile similarity, CHANNEL-AWARE.

    A centroid-only match loses when the profile's samples come from a
    different channel than the audio being measured (enrollment = raw
    microphone, meeting = Meet/Zoom codec on the system-audio track). So
    besides the centroid we also check the BEST INDIVIDUAL sample: if the
    profile contains a sample matching the current channel, it hits, while
    the centroid would blur into the average of the channels.
    """
    embs = profile.get("embeddings") or []
    if not embs:
        return -1.0
    best = float(np.dot(cluster_emb, _centroid(embs)))
    for e in embs:
        sim = float(np.dot(cluster_emb, _normed(np.array(e, dtype=np.float32))))
        if sim > best:
            best = sim
    return best


# ---------------------------------------------------------------------------
# Diarization + identification + segment assignment
# ---------------------------------------------------------------------------

def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


# Limits of harvest (automatic voice learning).
HARVEST_MIN_SEC = float(os.environ.get("DIARIZE_HARVEST_MIN_SEC", "8"))  # this much speech is needed for a profile
HARVEST_MAX_EMB = int(os.environ.get("DIARIZE_HARVEST_MAX_EMB", "10"))   # max samples per profile (FIFO)
HARVEST_POISON_SIM = float(os.environ.get("DIARIZE_HARVEST_POISON_SIM", "0.45"))  # poisoning defense


def harvest_profile(
    diarizer: Diarizer,
    samples: np.ndarray,
    spans: list[tuple[float, float]],
    workspace: str,
    name: str,
    is_me: bool = False,
) -> dict | None:
    """LEARNING a voice profile from the speech of an already-named speaker.

    This is the engine of the "Otter flow": if a cluster received a name from
    anywhere (Meet CC, official participant list, manual rename), we store
    its voice — at the next meeting we already recognize it, without CC or
    anything else.

    Protections:
      - only learns from sufficiently long, contiguous speech (HARVEST_MIN_SEC),
      - POISONING DEFENSE: if the name already has a profile, the new sample
        is only added if it substantially resembles it (otherwise a wrong
        name assignment would permanently ruin the profile),
      - bounded sample count per profile (the longest ones stay).
    """
    usable = [(s, e) for s, e in spans if e - s >= 1.0]
    total = sum(e - s for s, e in usable)
    if total < HARVEST_MIN_SEC:
        return None
    # We sample from the longest stretches (max ~30s) and embed PIECE BY
    # PIECE: the ONNX embedding model expects a fixed-size input, a long
    # concatenated sample throws a broadcast error. The duration-weighted,
    # normalized average of the piece embeddings goes into the profile (as
    # with the cluster centroid).
    usable.sort(key=lambda p: -(p[1] - p[0]))
    embs: list[tuple[np.ndarray, float]] = []
    acc = 0.0
    for s, e in usable:
        # Embed in pieces of 2-15 seconds.
        pos = s
        while pos < e and acc < 30.0:
            end = min(pos + 15.0, e)
            if end - pos >= 2.0:
                seg = samples[int(pos * SAMPLE_RATE):int(end * SAMPLE_RATE)]
                try:
                    embs.append((diarizer.embed(seg), end - pos))
                    acc += end - pos
                except Exception:
                    pass  # one faulty piece must not sink the whole learning
            pos = end
        if acc >= 30.0:
            break
    if not embs:
        return None
    w = np.array([d for _, d in embs], dtype=np.float32)
    m = np.average(np.stack([e for e, _ in embs]), axis=0, weights=w)
    norm = np.linalg.norm(m)
    emb = m / norm if norm > 0 else m

    existing = next(
        (p for p in load_profiles(workspace) if p["name"].strip().lower() == name.strip().lower()),
        None,
    )
    if existing is not None:
        if profile_similarity(existing, emb) < HARVEST_POISON_SIM:
            return None  # does not fit this voice — probably a wrong name
        if len(existing.get("embeddings") or []) >= HARVEST_MAX_EMB:
            return None  # the profile is already strong enough
    return save_speaker(workspace, name, is_me, emb)


def harvest_me_profile(
    diarizer: Diarizer,
    mic_samples: np.ndarray,
    mic_segments: list[dict],
    workspace: str,
    name: str,
) -> dict | None:
    """Automatically build the RECORDER's profile from the mic track.

    mic.wav is by definition exclusively the user's voice, so it is the
    cleanest possible training material — and it comes from the same channel
    as later recordings. This makes the 20-second read-aloud enrollment
    (SpeakersPanel) optional.
    """
    if not name.strip():
        return None
    spans = [(s["start"], s["end"]) for s in mic_segments]
    return harvest_profile(diarizer, mic_samples, spans, workspace, name, is_me=True)


def _rms(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    a = x.astype(np.float32)
    return float(np.sqrt(np.mean(a * a)))


# Threshold for speaker audio bleeding into the mic track: a mic segment only
# counts as GENUINE own speech if its energy is this many times that of the
# same window of the system track. With headphones the bleed is ~0, with
# loudspeakers it is significant.
BLEED_RATIO = float(os.environ.get("DIARIZE_BLEED_RATIO", "1.5"))


def merge_two_track(
    diarizer: Diarizer,
    mic_samples: np.ndarray,
    system_samples: np.ndarray,
    mic_segments: list[dict],
    system_segments: list[dict],
    workspace: str,
    num_speakers: int = -1,
) -> tuple[list[dict], list[dict], dict]:
    """TWO-TRACK processing — the system's strongest, platform-independent signal.

    The recording is made on two separate tracks: `mic` = EXCLUSIVELY the
    recorder's voice, `system` = EXCLUSIVELY the others. If these are mixed
    into one file (as the old mix_tracks did), this deterministic knowledge
    is lost and the diarization has to guess. Here we keep it:

      - the system track is diarized ON ITS OWN (one fewer speaker, cleaner
        clusters) → the existing enrollment identification runs on it,
      - every segment of the mic track belongs to the recorder (the `is_me`
        profile's name, if any),
      - loudspeaker bleed is filtered out by energy ratio.

    Returns: (merged segments, speakers list, statistics).
    """
    # 1) The others' track: full diarization + enrollment identification.
    if system_segments and system_samples.size > 0:
        sys_segs, speakers = diarize_and_identify(
            diarizer, system_samples, system_segments, workspace, num_speakers
        )
    else:
        sys_segs, speakers = [], []

    # 2) The recorder's name: the is_me profile, if any; otherwise a neutral "ME".
    me_profile = next((p for p in load_profiles(workspace) if p.get("is_me")), None)
    me_id = me_profile["id"] if me_profile else "ME"
    # "Te" = Hungarian for "You". Kept: user-visible fallback speaker label of
    # the Hungarian-language product UI, stored in transcripts.
    me_label = me_profile["name"] if me_profile else "Te"

    # 3) Bleed filtering: a mic segment belongs to the recorder only if its
    #    energy substantially exceeds the same window of the system track.
    kept, dropped = [], 0
    for s in mic_segments:
        i0, i1 = int(s["start"] * SAMPLE_RATE), int(s["end"] * SAMPLE_RATE)
        r_mic = _rms(mic_samples[i0:i1]) if mic_samples.size else 0.0
        r_sys = _rms(system_samples[i0:i1]) if system_samples.size else 0.0
        if r_sys > 0 and r_mic < BLEED_RATIO * r_sys:
            dropped += 1  # loudspeaker bleed, not own speech
            continue
        kept.append({"start": s["start"], "end": s["end"], "text": s["text"], "speaker": me_id})

    # 4) Merge by time.
    merged = sorted(sys_segs + kept, key=lambda x: x["start"])
    if kept and not any(sp["id"] == me_id for sp in speakers):
        speakers = [{"id": me_id, "label": me_label, "is_me": True}] + speakers

    stats = {
        "mic_segments": len(kept),
        "mic_dropped_bleed": dropped,
        "system_segments": len(sys_segs),
        "me_identified": me_profile is not None,
    }
    return merged, speakers, stats


def diarize_and_identify(
    diarizer: Diarizer,
    samples: np.ndarray,
    whisper_segments: list[dict],
    workspace: str,
    num_speakers: int = -1,
) -> tuple[list[dict], list[dict]]:
    """Main pipeline. whisper_segments: [{start, end, text}] (seconds).

    Returns: (segments with a speaker field, speakers list {id, label, is_me}).
    """
    turns = diarizer.diarize(samples, num_speakers)
    if not turns:
        return (
            [dict(s, speaker="SPEAKER_00") for s in whisper_segments],
            [{"id": "SPEAKER_00", "label": "Speaker 1", "is_me": False}],
        )

    # Cluster centroids from the turns' embeddings (weighted by duration).
    cluster_embs: dict[int, list[tuple[np.ndarray, float]]] = {}
    for t in turns:
        dur = t["end"] - t["start"]
        if dur < 0.3:
            continue
        seg = samples[int(t["start"] * SAMPLE_RATE):int(t["end"] * SAMPLE_RATE)]
        cluster_embs.setdefault(t["cluster"], []).append((diarizer.embed(seg), dur))
    centroids: dict[int, np.ndarray] = {}
    for c, pairs in cluster_embs.items():
        w = np.array([d for _, d in pairs], dtype=np.float32)
        m = np.average(np.stack([e for e, _ in pairs]), axis=0, weights=w)
        norm = np.linalg.norm(m)
        centroids[c] = m / norm if norm > 0 else m

    # Identification against enrollment profiles (cosine; the vectors are normalized).
    profiles = load_profiles(workspace)
    cluster_speaker: dict[int, dict] = {}
    unknown_counter = 0
    total_dur: dict[int, float] = {}
    for t in turns:
        total_dur[t["cluster"]] = total_dur.get(t["cluster"], 0.0) + (t["end"] - t["start"])
    # A profile may be assigned to only ONE cluster (the cluster with the
    # longest speaking time gets it) — otherwise the same name could settle
    # onto multiple clusters.
    claimed: set[str] = set()
    for c in sorted(centroids, key=lambda c: -total_dur.get(c, 0.0)):
        best_p, best_sim = None, SIM_THRESHOLD
        for p in profiles:
            if p["id"] in claimed:
                continue
            sim = profile_similarity(p, centroids[c])
            if sim >= best_sim:
                best_p, best_sim = p, sim
        if best_p is not None:
            claimed.add(best_p["id"])
        if best_p is not None:
            cluster_speaker[c] = {
                "id": best_p["id"],
                "label": best_p["name"],
                "is_me": bool(best_p.get("is_me", False)),
                "similarity": round(best_sim, 3),
            }
        else:
            cluster_speaker[c] = {
                "id": f"SPEAKER_{unknown_counter:02d}",
                "label": f"Speaker {unknown_counter + 1}",
                "is_me": False,
                "similarity": None,
            }
            unknown_counter += 1
    # Turn clusters without an embedding (too short): make a speaker of them too.
    for t in turns:
        if t["cluster"] not in cluster_speaker:
            cluster_speaker[t["cluster"]] = {
                "id": f"SPEAKER_{unknown_counter:02d}",
                "label": f"Speaker {unknown_counter + 1}",
                "is_me": False,
                "similarity": None,
            }
            unknown_counter += 1

    # Whisper segment → speaker: the cluster with the largest temporal overlap.
    out_segments = []
    for s in whisper_segments:
        best_c, best_ov = None, 0.0
        for t in turns:
            ov = _overlap(s["start"], s["end"], t["start"], t["end"])
            if ov > best_ov:
                best_c, best_ov = t["cluster"], ov
        if best_c is None:
            # no overlap (e.g. VAD mismatch) → nearest turn
            best_c = min(
                turns,
                key=lambda t: min(abs(t["start"] - s["end"]), abs(s["start"] - t["end"])),
            )["cluster"]
        dur = s["end"] - s["start"]
        confident = dur >= MIN_CONFIDENT_SEC and best_ov >= dur * 0.5
        # A short cluster unambiguously identified via enrollment may stay.
        if not confident and cluster_speaker[best_c]["similarity"] is not None and dur >= MIN_CONFIDENT_SEC:
            confident = True
        # Mini cluster (<2s total speaking time) without an enrollment match:
        # typically the unreliable embedding of a short interjection → falls
        # under smoothing.
        if cluster_speaker[best_c]["similarity"] is None and total_dur.get(best_c, 0.0) < 2.0:
            confident = False
        out_segments.append(dict(s, _cluster=best_c, _confident=confident))

    # Temporal smoothing: short/uncertain segment → neighbouring dominant speaker.
    for i, s in enumerate(out_segments):
        if s["_confident"]:
            continue
        prev_c = next((out_segments[j] for j in range(i - 1, -1, -1) if out_segments[j]["_confident"]), None)
        next_c = next((out_segments[j] for j in range(i + 1, len(out_segments)) if out_segments[j]["_confident"]), None)
        if prev_c is None and next_c is None:
            continue
        if prev_c is not None and next_c is not None:
            if prev_c["_cluster"] == next_c["_cluster"]:
                s["_cluster"] = prev_c["_cluster"]
            else:
                # the dominant neighbour (more total speaking time) wins
                s["_cluster"] = max(
                    (prev_c["_cluster"], next_c["_cluster"]),
                    key=lambda c: total_dur.get(c, 0.0),
                )
        else:
            s["_cluster"] = (prev_c or next_c)["_cluster"]

    # Assemble the output; only speakers actually used end up in the list.
    used = []
    final_segments = []
    for s in out_segments:
        spk = cluster_speaker[s["_cluster"]]
        if spk["id"] not in [u["id"] for u in used]:
            used.append(spk)
        final_segments.append(
            {"start": s["start"], "end": s["end"], "text": s["text"], "speaker": spk["id"]}
        )
    speakers = [{"id": u["id"], "label": u["label"], "is_me": u["is_me"]} for u in used]
    return final_segments, speakers
