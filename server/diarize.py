"""
Speaker diarization + enrollment — sherpa-onnx (ONNX runtime, PyTorch-mentes).

Motor: pyannote segmentation-3.0 (ONNX) + NeMo TitaNet-large speaker embedding.
Benchmark (2026-07-08, 100s HU teszt, 3 beszélő): 98,1% frame-pontosság,
490 MB csúcs-RAM, RTF 0,08 — részletek a STATUS.md-ben.

Multi-tenant: az enrollment-profilok workspace-enként külön könyvtárban élnek
(SPEAKER_DB_DIR/<workspace_id>/), egyik workspace sem lát rá a másikéra.
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

# Enrollment-találat küszöb. Mérés: valós találat 0,92+, legrosszabb hamis 0,54.
SIM_THRESHOLD = float(os.environ.get("DIARIZE_SIM_THRESHOLD", "0.60"))
# Klaszterezési küszöb (num_speakers ismeretlen esetén).
CLUSTER_THRESHOLD = float(os.environ.get("DIARIZE_CLUSTER_THRESHOLD", "0.5"))
# Ennél rövidebb szegmens embeddingje megbízhatatlan → temporal smoothing.
MIN_CONFIDENT_SEC = 1.0

SAMPLE_RATE = 16000

_WS_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def models_available() -> bool:
    return os.path.isfile(SEG_MODEL) and os.path.isfile(EMBED_MODEL)


class Diarizer:
    """sherpa-onnx diarizáció + embedding-kinyerés, process-szinten egyszer betöltve."""

    def __init__(self, num_threads: int = 2):
        if not models_available():
            raise RuntimeError(
                f"Diarizációs modellek hiányoznak: {SEG_MODEL} / {EMBED_MODEL} — "
                "lásd server/download_models.sh"
            )
        self._lock = threading.Lock()
        self._num_threads = num_threads
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=EMBED_MODEL, num_threads=num_threads)
        )

    def _make_sd(self, num_clusters: int) -> sherpa_onnx.OfflineSpeakerDiarization:
        # A klaszterszám a configban rögzül, ezért kérésenként példányosítunk;
        # a modell-fájlokat az ONNX runtime OS-cache-ből olvassa, ez olcsó.
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
        """16 kHz mono float32 → [{start, end, cluster}] idő szerint rendezve."""
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
# Enrollment-tár (workspace-enként elkülönítve)
# ---------------------------------------------------------------------------

def _ws_dir(workspace: str) -> str:
    if not _WS_RE.match(workspace):
        raise ValueError("Érvénytelen workspace azonosító (A-Za-z0-9_- , max 64).")
    return os.path.join(SPEAKER_DB_DIR, workspace)


def _profile_path(workspace: str, speaker_id: str) -> str:
    if not _WS_RE.match(speaker_id):
        raise ValueError("Érvénytelen speaker azonosító.")
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
    """Új profil, vagy azonos név esetén új minta hozzáfűzése a meglévőhöz."""
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
    """Klaszter↔profil hasonlóság CSATORNA-TUDATOSAN.

    A centroid-only match veszít, ha a profil mintái más csatornáról származnak,
    mint a mérendő hang (enrollment = nyers mikrofon, meeting = Meet/Zoom-codec
    a rendszerhang-sávon). Ezért a centroid mellett a LEGJOBB EGYEDI mintát is
    nézzük: ha a profilban van a jelenlegi csatornához illő minta, az eltalál,
    míg a centroid a csatornák átlagába mosódna.
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
# Diarizálás + azonosítás + szegmens-hozzárendelés
# ---------------------------------------------------------------------------

def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


# Harvest (automatikus hangtanulás) korlátai.
HARVEST_MIN_SEC = float(os.environ.get("DIARIZE_HARVEST_MIN_SEC", "8"))  # ennyi beszéd kell egy profilhoz
HARVEST_MAX_EMB = int(os.environ.get("DIARIZE_HARVEST_MAX_EMB", "10"))   # profilonként max minta (FIFO)
HARVEST_POISON_SIM = float(os.environ.get("DIARIZE_HARVEST_POISON_SIM", "0.45"))  # mérgezés-védelem


def harvest_profile(
    diarizer: Diarizer,
    samples: np.ndarray,
    spans: list[tuple[float, float]],
    workspace: str,
    name: str,
    is_me: bool = False,
) -> dict | None:
    """Hangprofil TANULÁSA egy már megnevezett beszélő beszédéből.

    Ez az "Otter-flow" motorja: ha egy klaszter bárhonnan nevet kapott (Meet CC,
    hivatalos résztvevő-lista, kézi átnevezés), a hangját eltároljuk — a
    következő meetingen már felismerjük, CC és minden más nélkül.

    Védelmek:
      - csak elég hosszú, összefüggő beszédből tanul (HARVEST_MIN_SEC),
      - MÉRGEZÉS-VÉDELEM: ha a névhez már van profil, az új minta csak akkor
        kerül be, ha érdemben hasonlít rá (különben egy téves névhozzárendelés
        tartósan elrontaná a profilt),
      - profilonként korlátozott mintaszám (a leghosszabbak maradnak).
    """
    usable = [(s, e) for s, e in spans if e - s >= 1.0]
    total = sum(e - s for s, e in usable)
    if total < HARVEST_MIN_SEC:
        return None
    # A leghosszabb részletekből mintát veszünk (max ~30s), és DARABONKÉNT
    # embeddelünk: az ONNX embedding-modell fix méretű bemenetet vár, a hosszú
    # összefűzött minta broadcast-hibát dob. A darab-embeddingek időtartam-
    # súlyozott, normált átlaga megy a profilba (mint a klaszter-centroidnál).
    usable.sort(key=lambda p: -(p[1] - p[0]))
    embs: list[tuple[np.ndarray, float]] = []
    acc = 0.0
    for s, e in usable:
        # 2-15 mp-es darabokra vágva embeddelünk.
        pos = s
        while pos < e and acc < 30.0:
            end = min(pos + 15.0, e)
            if end - pos >= 2.0:
                seg = samples[int(pos * SAMPLE_RATE):int(end * SAMPLE_RATE)]
                try:
                    embs.append((diarizer.embed(seg), end - pos))
                    acc += end - pos
                except Exception:
                    pass  # egy hibás darab nem viheti el a teljes tanulást
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
            return None  # nem erre a hangra illik — valószínűleg téves név
        if len(existing.get("embeddings") or []) >= HARVEST_MAX_EMB:
            return None  # a profil már elég erős
    return save_speaker(workspace, name, is_me, emb)


def harvest_me_profile(
    diarizer: Diarizer,
    mic_samples: np.ndarray,
    mic_segments: list[dict],
    workspace: str,
    name: str,
) -> dict | None:
    """A FELVEVŐ profiljának automatikus építése a mic-sávból.

    A mic.wav definíció szerint kizárólag a felhasználó hangja, ezért ez a
    legtisztább lehetséges tanítóanyag — és ugyanabból a csatornából jön, mint
    a későbbi felvételek. Ezzel a 20 másodperces felolvasós enrollment
    (SpeakersPanel) opcionálissá válik.
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


# A mic-sávra átszűrődő hangszóró-hang (bleed) küszöbe: a mic-szegmens csak
# akkor számít VALÓDI saját beszédnek, ha energiája ennyiszerese a system-sáv
# azonos ablakának. Fülhallgatóval a bleed ~0, hangszóróval jelentős.
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
    """KÉT-SÁVOS feldolgozás — a rendszer legerősebb, platform-független jele.

    A felvétel két külön sávon készül: `mic` = KIZÁRÓLAG a felvevő hangja,
    `system` = KIZÁRÓLAG a többiek. Ha ezeket összekeverjük egy fájlba (ahogy a
    régi mix_tracks tette), ez a determinisztikus tudás elvész, és a
    diarizációnak találgatnia kell. Itt megtartjuk:

      - a system-sáv MAGÁBAN diarizálódik (eggyel kevesebb beszélő, tisztább
        klaszterek) → a meglévő enrollment-azonosítás fut rá,
      - a mic-sáv minden szegmense a felvevőé (`is_me` profil neve, ha van),
      - a hangszóró-áthallás (bleed) energia-aránnyal kiszűrve.

    Vissza: (összefésült szegmensek, speakers lista, statisztika).
    """
    # 1) A többiek sávja: teljes diarizáció + enrollment-azonosítás.
    if system_segments and system_samples.size > 0:
        sys_segs, speakers = diarize_and_identify(
            diarizer, system_samples, system_segments, workspace, num_speakers
        )
    else:
        sys_segs, speakers = [], []

    # 2) A felvevő neve: az is_me profil, ha van; különben semleges "ME".
    me_profile = next((p for p in load_profiles(workspace) if p.get("is_me")), None)
    me_id = me_profile["id"] if me_profile else "ME"
    me_label = me_profile["name"] if me_profile else "Te"

    # 3) Bleed-szűrés: a mic-szegmens csak akkor a felvevőé, ha energiája
    #    érdemben meghaladja a system-sáv azonos ablakát.
    kept, dropped = [], 0
    for s in mic_segments:
        i0, i1 = int(s["start"] * SAMPLE_RATE), int(s["end"] * SAMPLE_RATE)
        r_mic = _rms(mic_samples[i0:i1]) if mic_samples.size else 0.0
        r_sys = _rms(system_samples[i0:i1]) if system_samples.size else 0.0
        if r_sys > 0 and r_mic < BLEED_RATIO * r_sys:
            dropped += 1  # hangszóró-áthallás, nem saját beszéd
            continue
        kept.append({"start": s["start"], "end": s["end"], "text": s["text"], "speaker": me_id})

    # 4) Összefésülés idő szerint.
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
    """Fő pipeline. whisper_segments: [{start, end, text}] (másodperc).

    Vissza: (segments speaker-mezővel, speakers lista {id, label, is_me}).
    """
    turns = diarizer.diarize(samples, num_speakers)
    if not turns:
        return (
            [dict(s, speaker="SPEAKER_00") for s in whisper_segments],
            [{"id": "SPEAKER_00", "label": "Speaker 1", "is_me": False}],
        )

    # Klaszter-centroidok a turnök embeddingjeiből (időtartammal súlyozva).
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

    # Azonosítás enrollment-profilok ellen (cosine; a vektorok normáltak).
    profiles = load_profiles(workspace)
    cluster_speaker: dict[int, dict] = {}
    unknown_counter = 0
    total_dur: dict[int, float] = {}
    for t in turns:
        total_dur[t["cluster"]] = total_dur.get(t["cluster"], 0.0) + (t["end"] - t["start"])
    # Egy profil csak EGY klaszterhez rendelhető (a leghosszabb beszédidejű
    # klaszter kapja meg) — különben ugyanaz a név több klaszterre is ráülhet.
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
    # Turnök klasztere embedding nélkül (túl rövid): legyen belőle is speaker.
    for t in turns:
        if t["cluster"] not in cluster_speaker:
            cluster_speaker[t["cluster"]] = {
                "id": f"SPEAKER_{unknown_counter:02d}",
                "label": f"Speaker {unknown_counter + 1}",
                "is_me": False,
                "similarity": None,
            }
            unknown_counter += 1

    # Whisper-szegmens → beszélő: legnagyobb időbeli átfedésű klaszter.
    out_segments = []
    for s in whisper_segments:
        best_c, best_ov = None, 0.0
        for t in turns:
            ov = _overlap(s["start"], s["end"], t["start"], t["end"])
            if ov > best_ov:
                best_c, best_ov = t["cluster"], ov
        if best_c is None:
            # nincs átfedés (pl. VAD-eltérés) → legközelebbi turn
            best_c = min(
                turns,
                key=lambda t: min(abs(t["start"] - s["end"]), abs(s["start"] - t["end"])),
            )["cluster"]
        dur = s["end"] - s["start"]
        confident = dur >= MIN_CONFIDENT_SEC and best_ov >= dur * 0.5
        # Rövid, de enrollment-tal egyértelműen azonosított klaszter maradhat.
        if not confident and cluster_speaker[best_c]["similarity"] is not None and dur >= MIN_CONFIDENT_SEC:
            confident = True
        # Mini-klaszter (<2s össz-beszédidő) enrollment-találat nélkül: tipikusan
        # egy rövid közbeszólás megbízhatatlan embeddingje → smoothing alá esik.
        if cluster_speaker[best_c]["similarity"] is None and total_dur.get(best_c, 0.0) < 2.0:
            confident = False
        out_segments.append(dict(s, _cluster=best_c, _confident=confident))

    # Temporal smoothing: rövid/bizonytalan szegmens → szomszédos domináns beszélő.
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
                # a domináns (több össz-beszédidejű) szomszéd nyer
                s["_cluster"] = max(
                    (prev_c["_cluster"], next_c["_cluster"]),
                    key=lambda c: total_dur.get(c, 0.0),
                )
        else:
            s["_cluster"] = (prev_c or next_c)["_cluster"]

    # Kimenet összeállítása; csak a ténylegesen használt beszélők kerülnek a listába.
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
