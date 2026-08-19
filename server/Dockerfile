FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download models at build time (not at every container start)
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"

COPY download_models.sh .
RUN apt-get update && apt-get install -y --no-install-recommends curl bzip2 \
    && ./download_models.sh \
    && apt-get purge -y curl bzip2 && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# FIGYELEM: minden ÚJ modult fel kell venni ide, különben a konténer
# ModuleNotFoundError-ral indul — 2026-07-24-én az accounts.py kihagyása
# okozott éles kiesést.
COPY app.py accounts.py diarize.py meetings.py fusion.py introductions.py identify.py shares.py ./

EXPOSE 8040

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8040", "--workers", "1"]
