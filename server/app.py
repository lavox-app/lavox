"""
Lavox Transcription API — self-hosted faster-whisper + sherpa-onnx diarizáció.
Deploy: docker compose up -d
Endpointok:
  POST   /api/transcribe            (multipart audio; ?diarize=true → beszélő-azonosítás)
  POST   /api/speakers              (enrollment: 10-30s hangminta + név)
  GET    /api/speakers              (workspace profiljai)
  DELETE /api/speakers/{speaker_id}
  POST   /api/meetings              (metaadat+átirat → presigned R2 upload URL-ek)
  PUT    /api/meetings/{id}/complete
  GET    /api/meetings              (lista)
  GET    /api/meetings/{id}         (teljes átirat + presigned lejátszási URL-ek)
  PATCH  /api/meetings/{id}
  DELETE /api/meetings/{id}

Multi-tenant: minden speaker-műveletet az X-Workspace-Id fejléc szkópoz
(alapértelmezés: "default" — lokál/self-hosted, egy-felhasználós mód).
"""

import asyncio
import hmac
import json
import os
import re
import tempfile
import time
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, File, Form, UploadFile, Query, HTTPException, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio

import accounts
import shares

# ── Lavox Memory (lokális, opcionális) ───────────────────────────────────────
# A memória-modul CSAK a felhasználó gépén él (SQLite ~/Lavox/memory alatt);
# a VPS-konténer nem tartalmazza (Dockerfile nem másolja) → ImportError → no-op.
# Így ugyanaz az app.py fut mindkét házban, drift nélkül.
try:
    import memory as _lavox_memory
    _MEMORY_OK = True
except Exception:
    _lavox_memory = None
    _MEMORY_OK = False


def _memory_ingest_background(rec: dict, segments: list) -> None:
    """Háttér-ingest a transzkripció után — a válasz nem várja meg.
    Hibája sosem érinti a fő folyamatot, csak logol."""
    try:
        db = _lavox_memory.connect()
        res = _lavox_memory.ingest_recording(db, rec, segments)
        print(f"[memory] ingest: {res}")
    except Exception as e:
        print(f"[memory] ingest FAILED (nem kritikus): {e}")


import diarize as diar
import fusion
import identify
import introductions
import meetings as mtg

API_KEY = os.environ.get("LAVOX_API_KEY", "")
MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small")
COMPUTE_TYPE = os.environ.get("COMPUTE_TYPE", "int8")
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "100"))
DIARIZE_ENABLED = os.environ.get("DIARIZE_ENABLED", "1") == "1"

# VAD: a korábbi (threshold=0.5, min_silence=500ms) beállítás halk/gyors
# beszédkezdeteket dobott el. Alacsonyabb küszöb + rövidebb csend-ablak +
# padding → kevesebb kimaradt beszéd; a hamis pozitívokat a whisper üres
# szegmensként úgyis eldobja.
VAD_PARAMETERS = dict(
    threshold=float(os.environ.get("VAD_THRESHOLD", "0.35")),
    min_silence_duration_ms=int(os.environ.get("VAD_MIN_SILENCE_MS", "300")),
    min_speech_duration_ms=int(os.environ.get("VAD_MIN_SPEECH_MS", "100")),
    speech_pad_ms=int(os.environ.get("VAD_SPEECH_PAD_MS", "400")),
)

model: WhisperModel | None = None
diarizer: "diar.Diarizer | None" = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model, diarizer
    print(f"Loading faster-whisper model '{MODEL_SIZE}' ({COMPUTE_TYPE})...")
    t0 = time.time()
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type=COMPUTE_TYPE)
    print(f"Model loaded in {time.time() - t0:.1f}s")
    if DIARIZE_ENABLED and diar.models_available():
        t0 = time.time()
        diarizer = diar.Diarizer()
        print(f"Diarizer loaded in {time.time() - t0:.1f}s")
    else:
        print("Diarizer DISABLED (env vagy hiányzó modellek — server/download_models.sh)")
    if mtg.available():
        try:
            mtg.init_schema()
            print("Meetings store READY (Postgres + R2)")
        except Exception as e:
            print(f"Meetings store FAILED to init: {e}")
        # A bucket-CORS best-effort: admin-jogú R2 token kell hozzá (a jelenlegi
        # Object R/W token nem tudja beállítani). CORS csak a böngészőből közvetlen
        # R2-feltöltéshez kell; a <video>/<audio> lejátszás anélkül is megy.
        try:
            mtg.ensure_bucket_cors()
            print("R2 bucket-CORS beállítva")
        except Exception as e:
            print(f"R2 bucket-CORS kihagyva (nem kritikus): {e}")
    else:
        print("Meetings store DISABLED (hiányzó LAVOX_PG_DSN / LAVOX_R2_* env)")
    if accounts.available():
        try:
            accounts.init_schema()
            print("Accounts READY (multi-tenant: fiókok + workspace-tagság)")
        except Exception as e:
            print(f"Accounts FAILED to init: {e}")
    else:
        print("Accounts DISABLED (self-hosted egy-felhasználós mód)")
    if shares.available():
        try:
            shares.init_schema()
            print("Shares READY (megosztható linkek, fiók nélküli megtekintés)")
        except Exception as e:
            print(f"Shares FAILED to init: {e}")
    else:
        print("Shares DISABLED (hiányzó LAVOX_PG_DSN / meetings-tár)")
    yield
    model = None
    diarizer = None


app = FastAPI(title="Lavox Transcription API", version="1.1.0", lifespan=lifespan)

# A hangar-dashboard (lokális Vite app) böngészőből hívja az API-t — CORS kell.
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5190",
        "http://127.0.0.1:5190",
        "https://lavox.app",
        "https://app.lavox.cloud",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


def check_auth(authorization: str | None):
    if not API_KEY:
        return
    if not authorization or authorization.replace("Bearer ", "") != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def check_workspace(workspace: str) -> str:
    try:
        diar._ws_dir(workspace)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return workspace


def authorize(
    authorization: str | None,
    workspace: str,
    acting_user_id: str | None = None,
) -> str:
    """Hitelesítés + workspace-jogosultság egy lépésben.

    Self-hosted mód (alapértelmezés): a régi viselkedés — megosztott API-kulcs
    (vagy semmi, ha nincs LAVOX_API_KEY), a workspace csak formailag ellenőrzött.

    Multi-tenant mód: a Bearer token egy userhez tartozik, és a szerver
    ellenőrzi, hogy tagja-e a kért workspace-nek. Enélkül az X-Workspace-Id
    fejléc átírásával bárki elérné bármelyik workspace adatát.
    """
    if not accounts.available():
        check_auth(authorization)
        return check_workspace(workspace)

    token = (authorization or "").removeprefix("Bearer ").strip()

    # Két hívó-típus:
    #  a) A webapp SZOLGÁLTATÁSKÉNT: a szolgáltatás-kulcsot küldi + X-Lavox-User-Id
    #     fejlécet. A kulcs csak a webapp szerverén létezik, sosem a böngészőben —
    #     így nem kell per-user tokent a session-be tenni (ahonnan kiszivárogna).
    #  b) A Lavox Hub / közvetlen kliens: saját per-user tokent küld.
    # Mindkét esetben a tagság-ellenőrzés ugyanaz.
    # A compare_digest STRINGEKKEL csak ASCII-t fogad; a Starlette viszont
    # latin-1-gyel dekódolja a fejléceket, tehát a kliens tetszőleges bájtot
    # betehet a tokenbe. Bájtokon összehasonlítva nincs TypeError, és a hívó
    # rendes 401-et kap 500 helyett (hitelesítés nélkül kiváltható hibaút volt).
    if API_KEY and acting_user_id and hmac.compare_digest(
        token.encode("utf-8"), API_KEY.encode("utf-8")
    ):
        principal = accounts.user_by_id(acting_user_id)
        if not principal:
            raise HTTPException(status_code=401, detail="Ismeretlen felhasználó")
    else:
        principal = accounts.user_by_token(token)
        if not principal:
            raise HTTPException(status_code=401, detail="Érvénytelen vagy hiányzó token")

    ws = check_workspace(workspace)
    owned = principal["workspaces"]
    member_ids = {w["id"] for w in owned}
    # A kliens elhagyhatja a fejlécet ("default") — ilyenkor a saját első
    # workspace-ére esünk vissza, nem a globális "default"-ra.
    if ws == "default" and owned:
        return owned[0]["id"]
    if ws not in member_ids:
        raise HTTPException(status_code=403, detail="Nincs jogosultság ehhez a workspace-hez")
    return ws


def require_diarizer() -> "diar.Diarizer":
    if diarizer is None:
        raise HTTPException(
            status_code=503,
            detail="Diarizáció nem elérhető (modellek hiányoznak vagy DIARIZE_ENABLED=0)",
        )
    return diarizer


def require_meetings():
    if not mtg.available():
        raise HTTPException(
            status_code=503,
            detail="Meeting-tár nem elérhető (hiányzó LAVOX_PG_DSN / LAVOX_R2_* env)",
        )


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "model": MODEL_SIZE,
        "compute_type": COMPUTE_TYPE,
        "diarization": diarizer is not None,
        "meetings": mtg.available(),
        "accounts": accounts.available(),
    }


# ── fiókok (csak multi-tenant módban; self-hosted telepítésen 404-et adnak) ────


class RegisterBody(BaseModel):
    email: str
    password: str
    first_name: str = ""
    last_name: str = ""


class LoginBody(BaseModel):
    email: str
    password: str


def require_accounts():
    if not accounts.available():
        raise HTTPException(
            status_code=404,
            detail="Ez a példány egy-felhasználós módban fut (nincs fiókkezelés).",
        )


# FIGYELEM: az auth-végpontok SZÁNDÉKOSAN `def` (nem `async def`) — a scrypt
# másodperc-nagyságrendű, blokkoló CPU-munka. `async def`-ben az event-loopot
# fogná, és néhány párhuzamos bejelentkezési kérés megbénítaná az egész
# szervert (hitelesítés nélküli DoS). A sima `def`-et a FastAPI threadpoolba teszi.

@app.post("/api/auth/register")
def auth_register(body: RegisterBody, request: Request):
    require_accounts()
    ip = request.client.host if request.client else "?"
    if accounts.too_many_attempts(f"reg:{ip}"):
        raise HTTPException(status_code=429, detail="Túl sok próbálkozás. Próbáld később.")
    accounts.record_attempt(f"reg:{ip}")
    try:
        return accounts.register(body.email, body.password, body.first_name, body.last_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/auth/login")
def auth_login(body: LoginBody, request: Request):
    require_accounts()
    ip = request.client.host if request.client else "?"
    email_key = f"login:{accounts._normalize_email(body.email)}"
    ip_key = f"login-ip:{ip}"

    # Fék e-mailre ÉS IP-re: az előbbi a célzott, az utóbbi a szórt próbálkozást fogja.
    if accounts.too_many_attempts(email_key) or accounts.too_many_attempts(ip_key):
        raise HTTPException(status_code=429, detail="Túl sok sikertelen próbálkozás. Próbáld később.")

    result = accounts.login(body.email, body.password)
    if not result:
        accounts.record_attempt(email_key)
        accounts.record_attempt(ip_key)
        # Szándékosan nem áruljuk el, az e-mail vagy a jelszó volt-e rossz.
        raise HTTPException(status_code=401, detail="Hibás e-mail vagy jelszó.")

    accounts.clear_attempts(email_key)
    return result


class OAuthBody(BaseModel):
    email: str
    first_name: str = ""
    last_name: str = ""
    provider: str = "oauth"


@app.post("/api/auth/oauth")
def auth_oauth(body: OAuthBody, authorization: str | None = Header(default=None)):
    """Külső szolgáltatóval (Google/Microsoft/Apple) belépett user fiókjának
    létrehozása vagy megkeresése.

    BIZTONSÁG: ezt a végpontot CSAK a webapp hívhatja a szolgáltatás-kulccsal.
    Az e-mail birtoklását a szolgáltató igazolta a webapp felé — a backend a
    webappban bízik. Kulcs nélkül bárki igényelhetne tetszőleges e-mail címet,
    és átvehetné vele egy meglévő fiók workspace-ét.
    """
    require_accounts()
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not API_KEY or not hmac.compare_digest(token.encode("utf-8"), API_KEY.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Csak szolgáltatás-kulccsal hívható.")
    try:
        return accounts.upsert_oauth_user(
            body.email, body.first_name, body.last_name, body.provider
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Hub-párosítás (device-code flow) ──────────────────────────────────────────


class ClaimBody(BaseModel):
    code: str


@app.post("/api/hub/pair/start")
def hub_pair_start(
    authorization: str | None = Header(default=None),
    x_workspace_id: str = Header(default="default"),
    x_lavox_user_id: str | None = Header(default=None),
):
    """A webapp kéri (szolgáltatás-kulcs + user-fejléc). Rövid életű pároztató kódot ad."""
    require_accounts()
    workspace = authorize(authorization, x_workspace_id, x_lavox_user_id)
    if not x_lavox_user_id:
        raise HTTPException(status_code=400, detail="Hiányzó felhasználó a párosításhoz.")
    return accounts.create_pairing_code(x_lavox_user_id, workspace)


@app.post("/api/hub/pair/claim")
def hub_pair_claim(body: ClaimBody, request: Request):
    """A HUB hívja, auth NÉLKÜL — a kód maga a titok. Beváltja eszköz-tokenre.
    Rate-limitelt, hogy a rövid kódot ne lehessen brute-force-olni."""
    require_accounts()
    ip = request.client.host if request.client else "?"
    if accounts.too_many_attempts(f"pair:{ip}"):
        raise HTTPException(status_code=429, detail="Túl sok próbálkozás. Próbáld később.")
    result = accounts.claim_pairing_code(body.code)
    if not result:
        accounts.record_attempt(f"pair:{ip}")
        raise HTTPException(status_code=404, detail="Érvénytelen vagy lejárt párosító kód.")
    return result


@app.post("/api/hub/heartbeat")
def hub_heartbeat(authorization: str | None = Header(default=None)):
    """A HUB periodikusan hívja az eszköz-tokenjével → 'online' marad."""
    require_accounts()
    token = (authorization or "").removeprefix("Bearer ").strip()
    result = accounts.record_hub_heartbeat(token)
    if not result:
        raise HTTPException(status_code=401, detail="Érvénytelen eszköz-token.")
    spaces = result.get("workspaces") or []
    return {"ok": True, "workspace": spaces[0]["id"] if spaces else None}


@app.get("/api/hub/status")
def hub_status_ep(
    authorization: str | None = Header(default=None),
    x_workspace_id: str = Header(default="default"),
    x_lavox_user_id: str | None = Header(default=None),
):
    """A webapp kérdezi: online-e a user Hubja?"""
    require_accounts()
    authorize(authorization, x_workspace_id, x_lavox_user_id)
    if not x_lavox_user_id:
        raise HTTPException(status_code=400, detail="Hiányzó felhasználó.")
    return accounts.hub_status(x_lavox_user_id)


@app.get("/api/auth/me")
def auth_me(authorization: str | None = Header(default=None)):
    require_accounts()
    principal = accounts.user_by_token((authorization or "").replace("Bearer ", "").strip())
    if not principal:
        raise HTTPException(status_code=401, detail="Érvénytelen vagy hiányzó token")
    return principal


@app.post("/api/auth/logout")
def auth_logout(authorization: str | None = Header(default=None)):
    require_accounts()
    accounts.revoke_token((authorization or "").replace("Bearer ", "").strip())
    return {"ok": True}


async def _read_upload(file: UploadFile) -> bytes:
    content = await file.read()
    size_mb = len(content) / (1024 * 1024)
    if size_mb > MAX_FILE_MB:
        raise HTTPException(status_code=413, detail=f"File too large ({size_mb:.1f} MB > {MAX_FILE_MB} MB)")
    return content


def _harvest_named_speakers(
    segments, speakers, sys_samples, workspace,
    mic_samples=None, mic_segments=None, me_name=None,
):
    """A megnevezett beszélők hangprofiljának automatikus tanulása.

    Egy beszélő akkor "megnevezett", ha a label NEM a generikus "Speaker N"
    forma — vagyis CC-fúzióból, enrollmentből vagy más névforrásból kapott
    valódi nevet. A hangját eltároljuk → legközelebb felirat nélkül is
    felismerjük. A mérgezés-védelem és a mintaszám-korlát a diarize.py-ban.
    """
    if not speakers:
        return None
    learned, skipped = [], 0

    # A felvevő ("én") profilja a mic-sávból — a legtisztább tanítóanyag.
    # Ha a kérés nem ad nevet, a meglévő is_me profil neve bővül (így a
    # SpeakersPanel-lel egyszer felvett profil magától erősödik tovább).
    if mic_samples is not None and mic_segments:
        effective_me = me_name
        if not effective_me:
            existing_me = next((p for p in diar.load_profiles(workspace) if p.get("is_me")), None)
            effective_me = existing_me["name"] if existing_me else None
        if effective_me and diar.harvest_me_profile(diarizer, mic_samples, mic_segments, workspace, effective_me):
            learned.append(effective_me)

    if sys_samples is None:
        return {"learned": learned, "skipped": skipped} if learned else None

    generic = re.compile(r"^(speaker|beszélő)\s*\d+$", re.IGNORECASE)
    for spk in speakers:
        label = (spk.get("label") or "").strip()
        if not label or generic.match(label) or spk.get("is_me"):
            continue
        spans = [(s["start"], s["end"]) for s in segments if s.get("speaker") == spk["id"]]
        if not spans:
            continue
        try:
            if diar.harvest_profile(diarizer, sys_samples, spans, workspace, label):
                learned.append(label)
            else:
                skipped += 1
        except Exception:
            skipped += 1  # egy beszélő hibája nem viheti el a többiek tanulását
    if not learned and not skipped:
        return None
    return {"learned": learned, "skipped": skipped}


def _whisper_segments(path: str, language: str | None):
    """Whisper-átirat egy fájlra → (szegmensek, info). Kétsávos módban sávonként."""
    segments_raw, info = model.transcribe(
        path,
        language=language,
        beam_size=5,
        vad_filter=True,
        vad_parameters=VAD_PARAMETERS,
    )
    segs = []
    for seg in segments_raw:
        segs.append({
            "start": round(seg.start, 2),
            "end": round(seg.end, 2),
            "text": seg.text.strip(),
        })
    return segs, info


@app.post("/api/transcribe")
async def transcribe(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    lang: str = Query(default="hu", description="ISO nyelv-kód (hu, en, auto)"),
    diarize: bool = Query(default=False, description="Beszélő-azonosítás bekapcsolása"),
    num_speakers: int = Query(default=-1, description="Beszélők száma, ha ismert (-1 = auto)"),
    captions_json: str | None = Form(default=None),
    # mic_file: a felvevő KÜLÖN mikrofon-sávja. Ha jön, kétsávos módban
    # dolgozunk: `file` = a többiek (rendszerhang), `mic_file` = a felvevő.
    mic_file: UploadFile | None = File(default=None),
    me_name: str | None = Form(default=None),
    harvest: bool = Form(default=True),
    # candidate_names: igazoltan jelenlévő nevek (naptár/Meet API) JSON-listája.
    # A szöveg-alapú név-következtetés CSAK ezekből oszthat nevet.
    candidate_names: str | None = Form(default=None),
    # auto_save: ha true, a transzkripció UTÁN a szerver MAGÁTÓL elmenti a
    # felvételt a felhőbe (Postgres + R2) — így a webappban azonnal megjelenik,
    # kézi feltöltés nélkül.
    auto_save: bool = Form(default=False),
    meeting_id: str | None = Form(default=None),
    title: str | None = Form(default=None),
    created_at: str | None = Form(default=None),
    rec_type: str = Form(default="meeting"),
    # A valós felvétel-hossz (mp) — a kliens tudja; enélkül a VAD-szűrt
    # beszédidő kerülne mentésre, ami rövidebb a tényleges hossznál.
    duration_sec: str | None = Form(default=None),
    authorization: str | None = Header(default=None),
    x_workspace_id: str = Header(default="default"),
    x_lavox_user_id: str | None = Header(default=None),
):
    workspace = authorize(authorization, x_workspace_id, x_lavox_user_id)
    if not model:
        raise HTTPException(status_code=503, detail="Model not loaded")
    if diarize:
        require_diarizer()

    content = await _read_upload(file)
    mic_content = await _read_upload(mic_file) if mic_file is not None else None
    two_track = mic_content is not None and diarize

    suffix = os.path.splitext(file.filename or "audio.wav")[1] or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
        tmp.write(content)
        tmp.flush()

        language = None if lang == "auto" else lang
        t0 = time.time()

        # A whisper/diarizáció szinkron és CPU-intenzív — to_thread-be tesszük,
        # hogy NE blokkolja az event loop-ot. Blokkolt loop mellett a kapcsolat
        # (a hosszú, néma feldolgozás alatt) idle-timeoutol → "empty reply";
        # külön szálon a loop kiszolgálja a socketet, a válasz a végén kimegy.
        segments, info = await asyncio.to_thread(_whisper_segments, tmp.name, language)
        full_text_parts = [s["text"] for s in segments]
        transcribe_time = time.time() - t0

        speakers = None
        diarize_time = None
        two_track_stats = None
        harvest_stats = None

        if two_track:
            # ── KÉT-SÁVOS ÚT ──────────────────────────────────────────────
            # `file` = a többiek (rendszerhang), `mic_file` = a felvevő.
            # A két sáv KÜLÖN marad → az "én vs. ők" kérdés determinisztikus,
            # platformtól függetlenül (Zoom, Teams, telefon is).
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as mtmp:
                mtmp.write(mic_content)
                mtmp.flush()
                mic_segments, _ = await asyncio.to_thread(_whisper_segments, mtmp.name, language)
                mic_samples = await asyncio.to_thread(decode_audio, mtmp.name, diar.SAMPLE_RATE)

            t0 = time.time()
            sys_samples = await asyncio.to_thread(decode_audio, tmp.name, diar.SAMPLE_RATE)
            segments, speakers, two_track_stats = await asyncio.to_thread(
                diar.merge_two_track,
                diarizer, mic_samples, sys_samples, mic_segments, segments,
                workspace, num_speakers,
            )
            diarize_time = round(time.time() - t0, 2)
            full_text_parts = [s["text"] for s in segments]
            samples_for_harvest = sys_samples
        elif diarize and segments:
            # ── EGYSÁVOS (visszafelé kompatibilis) ÚT ────────────────────
            t0 = time.time()
            samples_for_harvest = await asyncio.to_thread(decode_audio, tmp.name, diar.SAMPLE_RATE)
            segments, speakers = await asyncio.to_thread(
                diar.diarize_and_identify,
                diarizer, samples_for_harvest, segments, workspace, num_speakers
            )
            diarize_time = round(time.time() - t0, 2)
        else:
            samples_for_harvest = None

        # Meet CC fúzió: valódi beszélő-nevek a whisper-szegmensekhez
        # (idő + szöveg-kontextus alapján; a hibázó forrásokat egymással
        # validálja — lásd fusion.py).
        fusion_stats = None
        if captions_json:
            try:
                cap_payload = json.loads(captions_json)
                cap_events = cap_payload.get("events", cap_payload if isinstance(cap_payload, list) else [])
                segments, speakers, fusion_stats = fusion.fuse_captions(segments, speakers, cap_events)
            except Exception as e:
                fusion_stats = {"error": str(e)}

        # ── SZÖVEG-ALAPÚ NÉV-JELEK (önbemutatkozás + megszólítás) ───────
        # Determinisztikus regex-réteg a MÉG névtelen klaszterekre. Pool
        # (candidate_names) megléte esetén csak igazoltan jelenlévő név
        # osztható ki — kitalált név strukturálisan nem kerülhet be.
        intro_stats = None
        if speakers is not None:
            try:
                pool = json.loads(candidate_names) if candidate_names else None
                if pool is not None and not isinstance(pool, list):
                    pool = None
                speakers, intro_stats = introductions.apply_intro_votes(segments, speakers, pool)
            except Exception as e:
                intro_stats = {"error": str(e)}

        # ── OPCIONÁLIS LLM-réteg (alapból KI; LAVOX_LLM_KEY env kapcsolja) ──
        # Utolsó réteg a még névtelen klaszterekre, szigorú pool-kényszerrel.
        llm_stats = None
        if speakers is not None and identify.available():
            try:
                pool = json.loads(candidate_names) if candidate_names else []
                if isinstance(pool, list) and pool:
                    named = [s["label"] for s in speakers]
                    speakers, llm_stats = identify.identify_remaining(segments, speakers, pool, named)
            except Exception as e:
                llm_stats = {"error": str(e)}

        # ── HANGTANULÁS (harvest) ────────────────────────────────────────
        # Minden beszélő, aki BÁRHONNAN nevet kapott, hangprofilt is kap — így
        # a következő meetingen felirat nélkül is felismerhető. Kikapcsolható
        # (harvest=false), a mérgezés-védelem a diarize.py-ban van.
        if harvest and diarizer is not None:
            try:
                harvest_stats = await asyncio.to_thread(
                    _harvest_named_speakers,
                    segments, speakers, samples_for_harvest, workspace,
                    mic_samples if two_track else None,
                    mic_segments if two_track else None,
                    me_name,
                )
            except Exception as e:
                harvest_stats = {"error": str(e)}

        # ── AUTO-SAVE a felhőbe (Postgres + R2) ──────────────────────────
        # A transzkripció után MAGÁTÓL felkerül a webappba (nincs kézi
        # feltöltés). A `file` temp-fájlja audio-ként megy R2-be; a videót a
        # kliens tölti fel külön, ha van.
        save_stats = None
        if auto_save and mtg.available():
            try:
                mid = meeting_id or f"mtg_{int(info.duration)}_{len(segments)}"
                # A valós felvétel-hossz a kliensből jön (duration_sec form-mező);
                # az info.duration csak a VAD-szűrt beszédidő, ami rövidebb.
                real_dur = float(duration_sec) if duration_sec else info.duration
                save_stats = await asyncio.to_thread(
                    mtg.save_meeting_direct,
                    workspace,
                    mid,
                    {
                        "title": title or "Névtelen felvétel",
                        "type": rec_type,
                        "created_at": created_at,
                        "duration_sec": real_dur,
                        "speakers": speakers or [],
                        "transcript": segments,
                        "source": "auto",
                    },
                    {"audio": (tmp.name, "wav")},
                )
            except Exception as e:
                save_stats = {"error": str(e)}

    response = {
        "full_text": " ".join(full_text_parts),
        "segments": segments,
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
        "duration": round(info.duration, 2),
        "processing_time": round(transcribe_time, 2),
    }
    if speakers is not None:
        response["speakers"] = speakers
        response["diarization"] = {
            "engine": "sherpa-onnx/pyannote-seg3+titanet-large",
            "num_speakers": len(speakers),
            "processing_time": diarize_time,
        }
    if two_track_stats is not None:
        response["two_track"] = two_track_stats
    if fusion_stats is not None:
        response["fusion"] = fusion_stats
    if intro_stats is not None:
        response["intro"] = intro_stats
    if llm_stats is not None:
        response["llm"] = llm_stats
    if harvest_stats:
        response["harvest"] = harvest_stats
    if save_stats is not None:
        response["saved"] = save_stats

    # Lavox Memory: minden átirat magától a memóriába folyik (háttérben,
    # a válasz nem várja meg). Lokális gépen él; a VPS-en _MEMORY_OK=False.
    if _MEMORY_OK and segments:
        from datetime import datetime as _dt, timezone as _tz
        _rid = meeting_id or f"rec_{_dt.now(_tz.utc).strftime('%Y%m%d_%H%M%S')}"
        _rec = {
            "id": _rid,
            "kind": rec_type or "meeting",
            "title": title,
            "occurred_at": created_at or _dt.now(_tz.utc).isoformat(timespec="seconds"),
            "duration_sec": response.get("duration"),
            "participants": [
                s0.get("label") for s0 in (speakers or []) if isinstance(s0, dict) and s0.get("label")
            ],
            "meta": {"source": "auto_ingest"},
        }
        background_tasks.add_task(_memory_ingest_background, _rec, list(segments))
        response["memory"] = {"scheduled": True, "recording_id": _rid}
    return JSONResponse(response)


@app.post("/api/speakers")
async def enroll_speaker(
    file: UploadFile = File(...),
    name: str = Form(...),
    is_me: bool = Form(default=False),
    authorization: str | None = Header(default=None),
    x_workspace_id: str = Header(default="default"),
    x_lavox_user_id: str | None = Header(default=None),
):
    """Beszélő-enrollment: 10-30s tiszta hangminta → embedding-profil."""
    workspace = authorize(authorization, x_workspace_id, x_lavox_user_id)
    d = require_diarizer()
    if not name.strip():
        raise HTTPException(status_code=400, detail="A név nem lehet üres")

    content = await _read_upload(file)
    with tempfile.NamedTemporaryFile(suffix=os.path.splitext(file.filename or "s.wav")[1] or ".wav") as tmp:
        tmp.write(content)
        tmp.flush()
        # decode_audio + embed blokkoló CPU-munka — to_thread-be tesszük (ugyanaz
        # a minta, mint a transcribe-nál), hogy ne fagyassza az event loopot.
        samples = await asyncio.to_thread(decode_audio, tmp.name, sampling_rate=diar.SAMPLE_RATE)

    dur = len(samples) / diar.SAMPLE_RATE
    if dur < 5.0:
        raise HTTPException(status_code=400, detail=f"A minta túl rövid ({dur:.1f}s) — minimum 5, ideálisan 10-30 másodperc kell")
    if dur > 120.0:
        raise HTTPException(status_code=400, detail=f"A minta túl hosszú ({dur:.1f}s) — maximum 120 másodperc")

    embedding = await asyncio.to_thread(d.embed, samples)
    profile = diar.save_speaker(workspace, name, is_me, embedding)
    return JSONResponse({
        "id": profile["id"],
        "name": profile["name"],
        "is_me": profile["is_me"],
        "num_samples": len(profile["embeddings"]),
        "sample_duration": round(dur, 1),
    })


# FONTOS: list_speakers/remove_speaker (itt lent) és a meetings-endpointok
# (lent) SZÁNDÉKOSAN sima `def`-ek, NEM `async def`. Blokkoló psycopg-hívást
# futtatnak, amit FastAPI sima `def` esetén threadpoolba tesz. `async def`-ként
# az event loopon blokkolnának, és egyetlen lassú DB-pillanat az EGÉSZ szervert
# (a /health-et is) megfagyasztaná — ez történt 2026-08-03-án: egy korábbi,
# ennél a fixnél régebbi app.py-ra épült deploy visszaállította ezeket
# async-ra, ami néhány órás teljes deadlockot okozott éles környezetben. Ha
# ismét async-ra kerülnek (pl. egy régebbi verzióból történő deployon
# keresztül), a hiba visszatér. Deploy előtt mindig ellenőrizd git diff-fel.
@app.get("/api/speakers")
def list_speakers(
    authorization: str | None = Header(default=None),
    x_workspace_id: str = Header(default="default"),
    x_lavox_user_id: str | None = Header(default=None),
):
    workspace = authorize(authorization, x_workspace_id, x_lavox_user_id)
    profiles = diar.load_profiles(workspace)
    return JSONResponse({
        "speakers": [
            {"id": p["id"], "name": p["name"], "is_me": p.get("is_me", False), "num_samples": len(p["embeddings"])}
            for p in profiles
        ]
    })


@app.delete("/api/speakers/{speaker_id}")
def remove_speaker(
    speaker_id: str,
    authorization: str | None = Header(default=None),
    x_workspace_id: str = Header(default="default"),
    x_lavox_user_id: str | None = Header(default=None),
):
    workspace = authorize(authorization, x_workspace_id, x_lavox_user_id)
    try:
        deleted = diar.delete_speaker(workspace, speaker_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Nincs ilyen beszélő-profil")
    return JSONResponse({"deleted": speaker_id})


# ---------------------------------------------------------------------------
# Meetings — metaadat Postgresben, média R2-ben (presigned URL-ek)
# ---------------------------------------------------------------------------

@app.post("/api/meetings")
def create_meeting(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_workspace_id: str = Header(default="default"),
    x_lavox_user_id: str | None = Header(default=None),
):
    workspace = authorize(authorization, x_workspace_id, x_lavox_user_id)
    require_meetings()
    try:
        return JSONResponse(mtg.create_meeting(workspace, payload))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/meetings/{meeting_id}/complete")
def complete_meeting(
    meeting_id: str,
    authorization: str | None = Header(default=None),
    x_workspace_id: str = Header(default="default"),
    x_lavox_user_id: str | None = Header(default=None),
):
    workspace = authorize(authorization, x_workspace_id, x_lavox_user_id)
    require_meetings()
    try:
        return JSONResponse(mtg.complete_meeting(workspace, meeting_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Nincs ilyen meeting")


@app.get("/api/meetings")
def list_meetings(
    authorization: str | None = Header(default=None),
    x_workspace_id: str = Header(default="default"),
    x_lavox_user_id: str | None = Header(default=None),
):
    workspace = authorize(authorization, x_workspace_id, x_lavox_user_id)
    require_meetings()
    return JSONResponse({"meetings": mtg.list_meetings(workspace)})


@app.get("/api/meetings/{meeting_id}")
def get_meeting(
    meeting_id: str,
    authorization: str | None = Header(default=None),
    x_workspace_id: str = Header(default="default"),
    x_lavox_user_id: str | None = Header(default=None),
):
    workspace = authorize(authorization, x_workspace_id, x_lavox_user_id)
    require_meetings()
    row = mtg.get_meeting(workspace, meeting_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Nincs ilyen meeting")
    return JSONResponse(row)


@app.patch("/api/meetings/{meeting_id}")
def patch_meeting(
    meeting_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_workspace_id: str = Header(default="default"),
    x_lavox_user_id: str | None = Header(default=None),
):
    workspace = authorize(authorization, x_workspace_id, x_lavox_user_id)
    require_meetings()
    if not mtg.patch_meeting(workspace, meeting_id, payload):
        raise HTTPException(status_code=404, detail="Nincs ilyen meeting vagy nincs frissíthető mező")
    return JSONResponse({"updated": meeting_id})


@app.delete("/api/meetings/{meeting_id}")
def delete_meeting(
    meeting_id: str,
    authorization: str | None = Header(default=None),
    x_workspace_id: str = Header(default="default"),
    x_lavox_user_id: str | None = Header(default=None),
):
    workspace = authorize(authorization, x_workspace_id, x_lavox_user_id)
    require_meetings()
    if not mtg.delete_meeting(workspace, meeting_id):
        raise HTTPException(status_code=404, detail="Nincs ilyen meeting")
    # A megosztás a meetinggel együtt hal — különben egy törölt meeting linkje
    # "élő" maradna a DB-ben (a resolve ugyan 404-et adna, de ne tartsuk).
    try:
        shares.revoke_share(workspace, meeting_id)
    except Exception:
        pass
    return JSONResponse({"deleted": meeting_id})


# ---------------------------------------------------------------------------
# Megosztható linkek — a Personal tier értéke: fiók nélküli megtekintés.
#
# A /api/shared/{token} az EGYETLEN hitelesítés nélküli adat-végpont. Ezért:
#   - a token a titok (256 bit), a DB csak SHA-256 lenyomatot tárol
#   - a válasz SZŰKÍTETT vetület (shares._PUBLIC_FIELDS) — workspace, meet_code,
#     participants, evaluation SOHA nem megy ki
#   - IP-alapú rate limit a találgatás ellen
#   - minden hibaeset EGYSÉGES 404 (ne lehessen megkülönböztetni lejártat a
#     nem létezőtől)
# Ezek sima `def`-ek (nem async) — blokkoló psycopg-t futtatnak; lásd a fenti
# meetings-blokk figyelmeztetését.
# ---------------------------------------------------------------------------

def require_shares():
    if not shares.available():
        raise HTTPException(status_code=503, detail="Megosztás nem elérhető")


@app.post("/api/meetings/{meeting_id}/share")
def create_share(
    meeting_id: str,
    authorization: str | None = Header(default=None),
    x_workspace_id: str = Header(default="default"),
    x_lavox_user_id: str | None = Header(default=None),
):
    workspace = authorize(authorization, x_workspace_id, x_lavox_user_id)
    require_shares()
    res = shares.create_or_get_share(workspace, meeting_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Nincs ilyen meeting")
    return JSONResponse(res)


@app.post("/api/meetings/{meeting_id}/share/rotate")
def rotate_share(
    meeting_id: str,
    authorization: str | None = Header(default=None),
    x_workspace_id: str = Header(default="default"),
    x_lavox_user_id: str | None = Header(default=None),
):
    """Új link kiadása a régi visszavonásával — ha a régi kiszivárgott."""
    workspace = authorize(authorization, x_workspace_id, x_lavox_user_id)
    require_shares()
    res = shares.rotate_share(workspace, meeting_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Nincs ilyen meeting")
    return JSONResponse(res)


@app.get("/api/meetings/{meeting_id}/share")
def get_share(
    meeting_id: str,
    authorization: str | None = Header(default=None),
    x_workspace_id: str = Header(default="default"),
    x_lavox_user_id: str | None = Header(default=None),
):
    workspace = authorize(authorization, x_workspace_id, x_lavox_user_id)
    require_shares()
    return JSONResponse(shares.share_status(workspace, meeting_id))


@app.delete("/api/meetings/{meeting_id}/share")
def delete_share(
    meeting_id: str,
    authorization: str | None = Header(default=None),
    x_workspace_id: str = Header(default="default"),
    x_lavox_user_id: str | None = Header(default=None),
):
    workspace = authorize(authorization, x_workspace_id, x_lavox_user_id)
    require_shares()
    return JSONResponse({"revoked": shares.revoke_share(workspace, meeting_id)})


def _client_ip(request: Request) -> str:
    """A HÍVÓ valódi IP-je.

    A konténer nginx mögött fut, ezért a `request.client.host` MINDIG az nginx
    konténer címe (172.19.0.x) — arra kulcsolt rate limit egyetlen közös vödröt
    adna az összes látogatónak, és egyetlen találgató kizárná az összes valódi
    nézőt. Az nginx a valódi címet `X-Real-IP`-ben küldi (lásd
    /opt/utter/nginx.conf `proxy_set_header X-Real-IP $remote_addr`).

    A fejlécet CSAK azért bízhatjuk meg, mert a :8040 port kizárólag
    localhoston hallgat: kívülről nem lehet közvetlenül, hamisított fejléccel
    megszólítani. Ha ez valaha változik, ez a feltevés is elesik.
    """
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        # A lánc utolsó eleme az, amit a MI nginxünk fűzött hozzá.
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


class MemoryIngestBody(BaseModel):
    text: str
    kind: str = "dictation"
    title: str | None = None
    id: str | None = None
    occurred_at: str | None = None


@app.post("/api/memory/ingest")
def memory_ingest(
    body: MemoryIngestBody,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
):
    """A Hub diktálás-hookja: a kész diktátum a memóriába folyik.

    A meetingek a /api/transcribe végén automatikusan ingestelődnek; a
    diktálás viszont a Hub Rust-oldalán (whisper.cpp) készül, nem megy át
    ezen a szerveren — ezért kell ez a külön beviteli kapu. Lokális gépen él
    (a VPS-en _MEMORY_OK=False → 503), a :8040 csak localhostra kötött.
    """
    check_auth(authorization)
    if not _MEMORY_OK:
        raise HTTPException(status_code=503, detail="Lavox Memory nem elérhető ezen a példányon")
    text = (body.text or "").strip()
    if len(text) < 25:
        return JSONResponse({"skipped": "túl rövid diktátum"})
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc)
    rid = body.id or f"dict_{now.strftime('%Y%m%d_%H%M%S')}"
    rec = {
        "id": rid,
        "kind": body.kind or "dictation",
        "title": body.title or (text[:60] + ("…" if len(text) > 60 else "")),
        "occurred_at": body.occurred_at or now.isoformat(timespec="seconds"),
        "meta": {"source": "dictation_hook"},
    }
    background_tasks.add_task(_memory_ingest_background, rec, [{"text": text}])
    return JSONResponse({"scheduled": True, "recording_id": rid})


@app.get("/api/shared/{token}")
def view_shared(token: str, request: Request):
    """PUBLIKUS — nincs hitelesítés, a link birtoklása a jogosultság."""
    require_shares()
    key = f"share:{_client_ip(request)}"
    if accounts.too_many_attempts(key):
        raise HTTPException(status_code=429, detail="Túl sok kérés, próbáld később")

    data = shares.resolve_share(token)
    if data is None:
        # Csak a SIKERTELEN próbálkozás számít a limitbe — így a valódi
        # linkjét sokszor megnyitó néző nem tiltja ki magát, a találgató igen.
        accounts.record_attempt(key)
        raise HTTPException(status_code=404, detail="Ez a link nem érvényes")
    return JSONResponse(data)
