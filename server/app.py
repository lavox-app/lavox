"""
Lavox Transcription API: self-hosted faster-whisper + sherpa-onnx diarization.
Deploy: docker compose up -d
Endpoints:
  POST   /api/transcribe            (multipart audio; ?diarize=true → speaker identification)
  POST   /api/speakers              (enrollment: 10-30s voice sample + name)
  GET    /api/speakers              (the workspace's profiles)
  DELETE /api/speakers/{speaker_id}
  POST   /api/meetings              (metadata+transcript → presigned R2 upload URLs)
  PUT    /api/meetings/{id}/complete
  GET    /api/meetings              (list)
  GET    /api/meetings/{id}         (full transcript + presigned playback URLs)
  PATCH  /api/meetings/{id}
  DELETE /api/meetings/{id}

Multi-tenant: every speaker operation is scoped by the X-Workspace-Id header
(default: "default", local/self-hosted, single-user mode).
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

# ── Lavox Memory (local, optional) ───────────────────────────────────────────
# The memory module lives ONLY on the user's machine (SQLite under
# ~/Lavox/memory); the VPS container does not include it (the Dockerfile does
# not copy it) → ImportError → no-op. This way the same app.py runs in both
# homes, without drift.
try:
    import memory as _lavox_memory
    _MEMORY_OK = True
except Exception:
    _lavox_memory = None
    _MEMORY_OK = False


def _memory_ingest_background(rec: dict, segments: list) -> None:
    """Background ingest after transcription, the response does not wait for it.
    Its failure never affects the main flow, it only logs."""
    try:
        db = _lavox_memory.connect()
        res = _lavox_memory.ingest_recording(db, rec, segments)
        print(f"[memory] ingest: {res}")
    except Exception as e:
        print(f"[memory] ingest FAILED (non-critical): {e}")


import diarize as diar
import fusion
import identify
import introductions
import dictionary
import meetings as mtg

API_KEY = os.environ.get("LAVOX_API_KEY", "")
MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small")
COMPUTE_TYPE = os.environ.get("COMPUTE_TYPE", "int8")
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "100"))
DIARIZE_ENABLED = os.environ.get("DIARIZE_ENABLED", "1") == "1"

# VAD: the previous settings (threshold=0.5, min_silence=500ms) dropped
# quiet/fast speech onsets. Lower threshold + shorter silence window +
# padding → less missed speech; whisper drops the false positives as empty
# segments anyway.
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
        print("Diarizer DISABLED (disabled by env, or models missing: run server/download_models.sh)")
    if mtg.available():
        try:
            mtg.init_schema()
            print("Meetings store READY (Postgres + R2)")
        except Exception as e:
            print(f"Meetings store FAILED to init: {e}")
        # Bucket CORS is best-effort: it needs an admin-privileged R2 token
        # (the current Object R/W token cannot set it). CORS is only needed
        # for direct browser-to-R2 uploads; <video>/<audio> playback works
        # without it.
        try:
            mtg.ensure_bucket_cors()
            print("R2 bucket CORS configured")
        except Exception as e:
            print(f"R2 bucket CORS skipped (non-critical): {e}")
    else:
        print("Meetings store DISABLED (missing LAVOX_PG_DSN / LAVOX_R2_* env)")
    if accounts.available():
        try:
            accounts.init_schema()
            print("Accounts READY (multi-tenant: accounts + workspace membership)")
        except Exception as e:
            print(f"Accounts FAILED to init: {e}")
    else:
        print("Accounts DISABLED (self-hosted single-user mode)")
    if shares.available():
        try:
            shares.init_schema()
            print("Shares READY (shareable links, viewing without an account)")
        except Exception as e:
            print(f"Shares FAILED to init: {e}")
    else:
        print("Shares DISABLED (missing LAVOX_PG_DSN / meetings store)")
    yield
    model = None
    diarizer = None


app = FastAPI(title="Lavox Transcription API", version="1.1.0", lifespan=lifespan)

# The hangar-dashboard (local Vite app) calls the API from the browser, CORS is needed.
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
    """Authentication + workspace authorization in one step.

    Self-hosted mode (default): the old behavior, shared API key (or
    nothing, if there is no LAVOX_API_KEY), the workspace is only formally
    validated.

    Multi-tenant mode: the Bearer token belongs to a user, and the server
    checks whether they are a member of the requested workspace. Without
    this, anyone could reach any workspace's data by rewriting the
    X-Workspace-Id header.
    """
    if not accounts.available():
        check_auth(authorization)
        return check_workspace(workspace)

    token = (authorization or "").removeprefix("Bearer ").strip()

    # Two caller types:
    #  a) The webapp AS A SERVICE: it sends the service key + the
    #     X-Lavox-User-Id header. The key only exists on the webapp's server,
    #     never in the browser, so no per-user token has to go into the
    #     session (from where it could leak).
    #  b) The Lavox Hub / direct client: sends its own per-user token.
    # The membership check is the same in both cases.
    # compare_digest on STRINGS only accepts ASCII; Starlette, however,
    # decodes headers as latin-1, so the client can put arbitrary bytes into
    # the token. Comparing bytes avoids the TypeError, and the caller gets a
    # proper 401 instead of 500 (this was an error path triggerable without
    # authentication).
    if API_KEY and acting_user_id and hmac.compare_digest(
        token.encode("utf-8"), API_KEY.encode("utf-8")
    ):
        principal = accounts.user_by_id(acting_user_id)
        if not principal:
            raise HTTPException(status_code=401, detail="Unknown user")
    else:
        principal = accounts.user_by_token(token)
        if not principal:
            raise HTTPException(status_code=401, detail="Invalid or missing token")

    ws = check_workspace(workspace)
    owned = principal["workspaces"]
    member_ids = {w["id"] for w in owned}
    # The client may omit the header ("default"), in that case we fall back
    # to their own first workspace, not the global "default".
    if ws == "default" and owned:
        return owned[0]["id"]
    if ws not in member_ids:
        raise HTTPException(status_code=403, detail="No permission for this workspace")
    return ws


def require_diarizer() -> "diar.Diarizer":
    if diarizer is None:
        raise HTTPException(
            status_code=503,
            detail="Diarization is not available (models missing or DIARIZE_ENABLED=0)",
        )
    return diarizer


def require_meetings():
    if not mtg.available():
        raise HTTPException(
            status_code=503,
            detail="Meeting store is not available (missing LAVOX_PG_DSN / LAVOX_R2_* env)",
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


# ── accounts (multi-tenant mode only; on self-hosted installations they return 404) ──


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
            detail="This instance runs in single-user mode (no account management).",
        )


# WARNING: the auth endpoints are DELIBERATELY `def` (not `async def`):
# scrypt is second-scale, blocking CPU work. In an `async def` it would hold
# the event loop, and a few concurrent login requests would paralyze the
# whole server (unauthenticated DoS). FastAPI puts a plain `def` into the
# threadpool.

@app.post("/api/auth/register")
def auth_register(body: RegisterBody, request: Request):
    require_accounts()
    ip = request.client.host if request.client else "?"
    if accounts.too_many_attempts(f"reg:{ip}"):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
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

    # Brake on e-mail AND IP: the former catches targeted, the latter spread-out attempts.
    if accounts.too_many_attempts(email_key) or accounts.too_many_attempts(ip_key):
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again later.")

    result = accounts.login(body.email, body.password)
    if not result:
        accounts.record_attempt(email_key)
        accounts.record_attempt(ip_key)
        # Deliberately do not reveal whether the e-mail or the password was wrong.
        raise HTTPException(status_code=401, detail="Incorrect e-mail or password.")

    accounts.clear_attempts(email_key)
    return result


class OAuthBody(BaseModel):
    email: str
    first_name: str = ""
    last_name: str = ""
    provider: str = "oauth"


@app.post("/api/auth/oauth")
def auth_oauth(body: OAuthBody, authorization: str | None = Header(default=None)):
    """Create or look up the account of a user logged in via an external
    provider (Google/Microsoft/Apple).

    SECURITY: this endpoint may ONLY be called by the webapp with the service
    key. Ownership of the e-mail was proven by the provider to the webapp;
    the backend trusts the webapp. Without the key, anyone could claim an
    arbitrary e-mail address and take over an existing account's workspace
    with it.
    """
    require_accounts()
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not API_KEY or not hmac.compare_digest(token.encode("utf-8"), API_KEY.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Callable only with the service key.")
    try:
        return accounts.upsert_oauth_user(
            body.email, body.first_name, body.last_name, body.provider
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Hub pairing (device-code flow) ────────────────────────────────────────────


class ClaimBody(BaseModel):
    code: str


@app.post("/api/hub/pair/start")
def hub_pair_start(
    authorization: str | None = Header(default=None),
    x_workspace_id: str = Header(default="default"),
    x_lavox_user_id: str | None = Header(default=None),
):
    """Requested by the webapp (service key + user header). Returns a short-lived pairing code."""
    require_accounts()
    workspace = authorize(authorization, x_workspace_id, x_lavox_user_id)
    if not x_lavox_user_id:
        raise HTTPException(status_code=400, detail="Missing user for pairing.")
    return accounts.create_pairing_code(x_lavox_user_id, workspace)


@app.post("/api/hub/pair/claim")
def hub_pair_claim(body: ClaimBody, request: Request):
    """Called by the HUB, WITHOUT auth, the code itself is the secret.
    Redeems it for a device token. Rate-limited so the short code cannot be
    brute-forced."""
    require_accounts()
    ip = request.client.host if request.client else "?"
    if accounts.too_many_attempts(f"pair:{ip}"):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
    result = accounts.claim_pairing_code(body.code)
    if not result:
        accounts.record_attempt(f"pair:{ip}")
        raise HTTPException(status_code=404, detail="Invalid or expired pairing code.")
    return result


@app.post("/api/hub/heartbeat")
def hub_heartbeat(authorization: str | None = Header(default=None)):
    """Called periodically by the HUB with its device token → it stays 'online'."""
    require_accounts()
    token = (authorization or "").removeprefix("Bearer ").strip()
    result = accounts.record_hub_heartbeat(token)
    if not result:
        raise HTTPException(status_code=401, detail="Invalid device token.")
    spaces = result.get("workspaces") or []
    return {"ok": True, "workspace": spaces[0]["id"] if spaces else None}


@app.get("/api/hub/status")
def hub_status_ep(
    authorization: str | None = Header(default=None),
    x_workspace_id: str = Header(default="default"),
    x_lavox_user_id: str | None = Header(default=None),
):
    """Asked by the webapp: is the user's Hub online?"""
    require_accounts()
    authorize(authorization, x_workspace_id, x_lavox_user_id)
    if not x_lavox_user_id:
        raise HTTPException(status_code=400, detail="Missing user.")
    return accounts.hub_status(x_lavox_user_id)


@app.get("/api/auth/me")
def auth_me(authorization: str | None = Header(default=None)):
    require_accounts()
    principal = accounts.user_by_token((authorization or "").replace("Bearer ", "").strip())
    if not principal:
        raise HTTPException(status_code=401, detail="Invalid or missing token")
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
    """Automatic learning of the named speakers' voice profiles.

    A speaker is "named" when the label is NOT the generic "Speaker N" form,
    i.e. it received a real name from CC fusion, enrollment, or another name
    source. We store their voice → next time we recognize them even without
    captions. The poisoning defense and the sample-count limit live in
    diarize.py.
    """
    if not speakers:
        return None
    learned, skipped = [], 0

    # The recorder's ("me") profile from the mic track, the cleanest training
    # material. If the request gives no name, the existing is_me profile's
    # name is extended (so a profile once recorded via the SpeakersPanel
    # keeps strengthening by itself).
    if mic_samples is not None and mic_segments:
        effective_me = me_name
        if not effective_me:
            existing_me = next((p for p in diar.load_profiles(workspace) if p.get("is_me")), None)
            effective_me = existing_me["name"] if existing_me else None
        if effective_me and diar.harvest_me_profile(diarizer, mic_samples, mic_segments, workspace, effective_me):
            learned.append(effective_me)

    if sys_samples is None:
        return {"learned": learned, "skipped": skipped} if learned else None

    # "beszélő" is functional data: it matches Hungarian generic speaker
    # labels ("Beszélő 1") produced by the pipeline. Do not translate.
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
            skipped += 1  # one speaker's failure must not sink the others' learning
    if not learned and not skipped:
        return None
    return {"learned": learned, "skipped": skipped}


def _whisper_segments(path: str, language: str | None):
    """Whisper transcript for one file → (segments, info). In two-track mode, per track."""
    segments_raw, info = model.transcribe(
        path,
        language=language,
        beam_size=5,
        vad_filter=True,
        vad_parameters=VAD_PARAMETERS,
        # personal dictionary → decoder vocabulary biasing (names, jargon)
        hotwords=dictionary.hotwords_string(),
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
    lang: str = Query(default="hu", description="ISO language code (hu, en, auto)"),
    diarize: bool = Query(default=False, description="Enable speaker identification"),
    num_speakers: int = Query(default=-1, description="Number of speakers if known (-1 = auto)"),
    captions_json: str | None = Form(default=None),
    # mic_file: the recorder's SEPARATE microphone track. If present, we work
    # in two-track mode: `file` = the others (system audio), `mic_file` = the recorder.
    mic_file: UploadFile | None = File(default=None),
    me_name: str | None = Form(default=None),
    harvest: bool = Form(default=True),
    # candidate_names: JSON list of names verifiably present (calendar/Meet API).
    # Text-based name inference may ONLY assign names from these.
    candidate_names: str | None = Form(default=None),
    # auto_save: if true, AFTER transcription the server saves the recording
    # to the cloud (Postgres + R2) BY ITSELF, so it appears in the webapp
    # immediately, without a manual upload.
    auto_save: bool = Form(default=False),
    meeting_id: str | None = Form(default=None),
    title: str | None = Form(default=None),
    created_at: str | None = Form(default=None),
    rec_type: str = Form(default="meeting"),
    # The real recording length (s), the client knows it; without it the
    # VAD-filtered speech time would be saved, which is shorter than the
    # actual length.
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

        # Whisper/diarization is synchronous and CPU-intensive, we put it in
        # to_thread so it does NOT block the event loop. With a blocked loop
        # the connection idle-timeouts (during the long, silent processing) →
        # "empty reply"; on a separate thread the loop serves the socket and
        # the response goes out at the end.
        segments, info = await asyncio.to_thread(_whisper_segments, tmp.name, language)
        full_text_parts = [s["text"] for s in segments]
        transcribe_time = time.time() - t0

        speakers = None
        diarize_time = None
        two_track_stats = None
        harvest_stats = None

        if two_track:
            # ── TWO-TRACK PATH ────────────────────────────────────────────
            # `file` = the others (system audio), `mic_file` = the recorder.
            # The two tracks stay SEPARATE → the "me vs. them" question is
            # deterministic, platform-independent (Zoom, Teams, phone too).
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
            # ── SINGLE-TRACK (backwards-compatible) PATH ─────────────────
            t0 = time.time()
            samples_for_harvest = await asyncio.to_thread(decode_audio, tmp.name, diar.SAMPLE_RATE)
            segments, speakers = await asyncio.to_thread(
                diar.diarize_and_identify,
                diarizer, samples_for_harvest, segments, workspace, num_speakers
            )
            diarize_time = round(time.time() - t0, 2)
        else:
            samples_for_harvest = None

        # Meet CC fusion: real speaker names for the whisper segments
        # (based on time + text context; validates the fallible sources
        # against each other, see fusion.py).
        fusion_stats = None
        if captions_json:
            try:
                cap_payload = json.loads(captions_json)
                cap_events = cap_payload.get("events", cap_payload if isinstance(cap_payload, list) else [])
                segments, speakers, fusion_stats = fusion.fuse_captions(segments, speakers, cap_events)
            except Exception as e:
                fusion_stats = {"error": str(e)}

        # ── TEXT-BASED NAME SIGNALS (self-introduction + addressing) ────
        # Deterministic regex layer for the clusters STILL unnamed. When a
        # pool (candidate_names) exists, only verifiably present names may
        # be assigned, an invented name structurally cannot get in.
        intro_stats = None
        if speakers is not None:
            try:
                pool = json.loads(candidate_names) if candidate_names else None
                if pool is not None and not isinstance(pool, list):
                    pool = None
                speakers, intro_stats = introductions.apply_intro_votes(segments, speakers, pool)
            except Exception as e:
                intro_stats = {"error": str(e)}

        # ── OPTIONAL LLM layer (OFF by default; toggled by the LAVOX_LLM_KEY env) ──
        # Last layer for the still-unnamed clusters, with a strict pool constraint.
        llm_stats = None
        if speakers is not None and identify.available():
            try:
                pool = json.loads(candidate_names) if candidate_names else []
                if isinstance(pool, list) and pool:
                    named = [s["label"] for s in speakers]
                    speakers, llm_stats = identify.identify_remaining(segments, speakers, pool, named)
            except Exception as e:
                llm_stats = {"error": str(e)}

        # ── VOICE LEARNING (harvest) ─────────────────────────────────────
        # Every speaker who received a name from ANYWHERE also gets a voice
        # profile, so at the next meeting they are recognizable even without
        # captions. Can be disabled (harvest=false); the poisoning defense
        # lives in diarize.py.
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

        # ── AUTO-SAVE to the cloud (Postgres + R2) ───────────────────────
        # After transcription it lands in the webapp BY ITSELF (no manual
        # upload). The temp file of `file` goes to R2 as audio; the client
        # uploads the video separately, if any.
        save_stats = None
        if auto_save and mtg.available():
            try:
                mid = meeting_id or f"mtg_{int(info.duration)}_{len(segments)}"
                # The real recording length comes from the client (duration_sec
                # form field); info.duration is only the VAD-filtered speech
                # time, which is shorter.
                real_dur = float(duration_sec) if duration_sec else info.duration
                save_stats = await asyncio.to_thread(
                    mtg.save_meeting_direct,
                    workspace,
                    mid,
                    {
                        # "Névtelen felvétel" = "Untitled recording", user-visible
                        # default title of the Hungarian-language product UI.
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

    # Lavox Memory: every transcript flows into the memory by itself (in the
    # background, the response does not wait). Lives on the local machine; on
    # the VPS _MEMORY_OK=False.
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
    """Speaker enrollment: 10-30s clean voice sample → embedding profile."""
    workspace = authorize(authorization, x_workspace_id, x_lavox_user_id)
    d = require_diarizer()
    if not name.strip():
        raise HTTPException(status_code=400, detail="The name must not be empty")

    content = await _read_upload(file)
    with tempfile.NamedTemporaryFile(suffix=os.path.splitext(file.filename or "s.wav")[1] or ".wav") as tmp:
        tmp.write(content)
        tmp.flush()
        # decode_audio + embed is blocking CPU work, we put it in to_thread
        # (same pattern as in transcribe) so it does not freeze the event loop.
        samples = await asyncio.to_thread(decode_audio, tmp.name, sampling_rate=diar.SAMPLE_RATE)

    dur = len(samples) / diar.SAMPLE_RATE
    if dur < 5.0:
        raise HTTPException(status_code=400, detail=f"The sample is too short ({dur:.1f}s): minimum 5 seconds, ideally 10-30")
    if dur > 120.0:
        raise HTTPException(status_code=400, detail=f"The sample is too long ({dur:.1f}s): maximum 120 seconds")

    embedding = await asyncio.to_thread(d.embed, samples)
    profile = diar.save_speaker(workspace, name, is_me, embedding)
    return JSONResponse({
        "id": profile["id"],
        "name": profile["name"],
        "is_me": profile["is_me"],
        "num_samples": len(profile["embeddings"]),
        "sample_duration": round(dur, 1),
    })


# IMPORTANT: list_speakers/remove_speaker (below) and the meetings endpoints
# (below) are DELIBERATELY plain `def`s, NOT `async def`. They run blocking
# psycopg calls, which FastAPI puts into a threadpool for a plain `def`. As
# `async def` they would block on the event loop, and a single slow DB moment
# would freeze the WHOLE server (including /health), this happened on
# 2026-08-03: a deploy built on an app.py older than this fix reverted these
# to async, causing a total deadlock of several hours in production. If they
# become async again (e.g. via a deploy from an older version), the bug
# returns. Always check with git diff before deploying.
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
        raise HTTPException(status_code=404, detail="No such speaker profile")
    return JSONResponse({"deleted": speaker_id})


# ---------------------------------------------------------------------------
# Meetings: metadata in Postgres, media in R2 (presigned URLs)
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
        raise HTTPException(status_code=404, detail="No such meeting")


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
        raise HTTPException(status_code=404, detail="No such meeting")
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
        raise HTTPException(status_code=404, detail="No such meeting or no updatable field")
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
        raise HTTPException(status_code=404, detail="No such meeting")
    # The share dies with the meeting, otherwise a deleted meeting's link
    # would remain "live" in the DB (resolve would return 404, but let's not
    # keep it).
    try:
        shares.revoke_share(workspace, meeting_id)
    except Exception:
        pass
    return JSONResponse({"deleted": meeting_id})


# ---------------------------------------------------------------------------
# Shareable links (the value of the Personal tier): viewing without an account.
#
# /api/shared/{token} is the ONLY unauthenticated data endpoint. Therefore:
#   - the token is the secret (256 bits), the DB stores only a SHA-256 digest
#   - the response is a RESTRICTED projection (shares._PUBLIC_FIELDS):
#     workspace, meet_code, participants, evaluation NEVER go out
#   - IP-based rate limit against guessing
#   - every failure case is a UNIFORM 404 (an expired one must not be
#     distinguishable from a non-existent one)
# These are plain `def`s (not async), they run blocking psycopg; see the
# warning at the meetings block above.
# ---------------------------------------------------------------------------

def require_shares():
    if not shares.available():
        raise HTTPException(status_code=503, detail="Sharing is not available")


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
        raise HTTPException(status_code=404, detail="No such meeting")
    return JSONResponse(res)


@app.post("/api/meetings/{meeting_id}/share/rotate")
def rotate_share(
    meeting_id: str,
    authorization: str | None = Header(default=None),
    x_workspace_id: str = Header(default="default"),
    x_lavox_user_id: str | None = Header(default=None),
):
    """Issue a new link while revoking the old one, if the old one leaked."""
    workspace = authorize(authorization, x_workspace_id, x_lavox_user_id)
    require_shares()
    res = shares.rotate_share(workspace, meeting_id)
    if res is None:
        raise HTTPException(status_code=404, detail="No such meeting")
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
    """The CALLER's real IP.

    The container runs behind nginx, so `request.client.host` is ALWAYS the
    nginx container's address (172.19.0.x), a rate limit keyed on that would
    give all visitors a single shared bucket, and one guesser would lock out
    all genuine viewers. nginx sends the real address in `X-Real-IP` (see
    /opt/utter/nginx.conf `proxy_set_header X-Real-IP $remote_addr`).

    We can trust the header ONLY because port :8040 listens exclusively on
    localhost: it cannot be reached directly from outside with a forged
    header. If that ever changes, this assumption falls too.
    """
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        # The last element of the chain is what OUR nginx appended.
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
    """The Hub's dictation hook: the finished dictation flows into the memory.

    Meetings are ingested automatically at the end of /api/transcribe;
    dictation, however, is produced on the Hub's Rust side (whisper.cpp) and
    does not pass through this server, hence this separate intake gate.
    Lives on the local machine (on the VPS _MEMORY_OK=False → 503), :8040 is
    bound to localhost only.
    """
    check_auth(authorization)
    if not _MEMORY_OK:
        raise HTTPException(status_code=503, detail="Lavox Memory is not available on this instance")
    text = (body.text or "").strip()
    if len(text) < 25:
        return JSONResponse({"skipped": "dictation too short"})
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
    """PUBLIC, no authentication, possession of the link is the authorization."""
    require_shares()
    key = f"share:{_client_ip(request)}"
    if accounts.too_many_attempts(key):
        raise HTTPException(status_code=429, detail="Too many requests, try again later")

    data = shares.resolve_share(token)
    if data is None:
        # Only FAILED attempts count toward the limit, so a viewer opening
        # their genuine link many times does not ban themselves, a guesser does.
        accounts.record_attempt(key)
        raise HTTPException(status_code=404, detail="This link is not valid")
    return JSONResponse(data)


# ── Personal dictation dictionary ────────────────────────────────────────────

class DictTermBody(BaseModel):
    term: str
    misheard: str | None = None


class DictLearnBody(BaseModel):
    raw: str
    corrected: str


@app.get("/api/dictionary")
def dictionary_get(authorization: str | None = Header(default=None)):
    check_auth(authorization)
    return JSONResponse(dictionary.load())


@app.post("/api/dictionary/term")
def dictionary_add_term(
    body: DictTermBody, authorization: str | None = Header(default=None)
):
    check_auth(authorization)
    try:
        entry = dictionary.add_term(body.term, misheard=body.misheard, source="manual")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(entry)


@app.delete("/api/dictionary/term/{term}")
def dictionary_delete_term(
    term: str, authorization: str | None = Header(default=None)
):
    check_auth(authorization)
    if not dictionary.remove_term(term):
        raise HTTPException(status_code=404, detail="No such term")
    return JSONResponse({"removed": term})


@app.post("/api/dictionary/learn")
def dictionary_learn(
    body: DictLearnBody, authorization: str | None = Header(default=None)
):
    """The Hub's correction hook: the user edited a dictation, the diff teaches
    the dictionary (term-like replacements only, see dictionary.py rules)."""
    check_auth(authorization)
    pairs = dictionary.learn_from_correction(body.raw, body.corrected)
    return JSONResponse({"learned": pairs})
