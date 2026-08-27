#!/usr/bin/env bash
# Downloads the diarization models (sherpa-onnx GitHub releases, MIT license).
# Target: server/models/. The Dockerfile runs it at build time; locally, run it by hand.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)/models"
mkdir -p "$DIR"
cd "$DIR"

SEG_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
# Note: the typo in the release tag ("recongition") is how k2-fsa published it.
EMB_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_large.onnx"

if [ ! -f sherpa-onnx-pyannote-segmentation-3-0/model.onnx ]; then
  echo "[1/2] pyannote segmentation-3.0 (ONNX, ~6 MB)..."
  curl -sL -o seg.tar.bz2 "$SEG_URL"
  tar xjf seg.tar.bz2
  rm seg.tar.bz2
else
  echo "[1/2] segmentation model already present."
fi

if [ ! -f nemo_en_titanet_large.onnx ]; then
  echo "[2/2] NeMo TitaNet-large embedding (~97 MB)..."
  curl -sL -O "$EMB_URL"
else
  echo "[2/2] embedding model already present."
fi

echo "Done: $DIR"
