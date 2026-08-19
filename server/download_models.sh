#!/usr/bin/env bash
# Diarizációs modellek letöltése (sherpa-onnx GitHub releases, MIT licenc).
# Cél: server/models/ — a Dockerfile build-time futtatja, lokálban kézzel is jó.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)/models"
mkdir -p "$DIR"
cd "$DIR"

SEG_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
# Figyelem: a release-tag elgépelése ("recongition") a k2-fsa oldalon van így.
EMB_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_large.onnx"

if [ ! -f sherpa-onnx-pyannote-segmentation-3-0/model.onnx ]; then
  echo "[1/2] pyannote segmentation-3.0 (ONNX, ~6 MB)..."
  curl -sL -o seg.tar.bz2 "$SEG_URL"
  tar xjf seg.tar.bz2
  rm seg.tar.bz2
else
  echo "[1/2] segmentation model megvan."
fi

if [ ! -f nemo_en_titanet_large.onnx ]; then
  echo "[2/2] NeMo TitaNet-large embedding (~97 MB)..."
  curl -sL -O "$EMB_URL"
else
  echo "[2/2] embedding model megvan."
fi

echo "Kész: $DIR"
