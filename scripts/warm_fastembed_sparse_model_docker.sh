#!/usr/bin/env bash
set -euo pipefail

MODEL_NAME="${FASTEMBED_SPARSE_MODEL:-prithivida/Splade_PP_en_v1}"
CACHE_DIR="${FASTEMBED_CACHE_DIR:-$HOME/.cache/fastembed}"
PY_IMAGE="${FASTEMBED_WARMUP_IMAGE:-python:3.12-slim}"

mkdir -p "${CACHE_DIR}"

docker_args=(
  run --rm
  -v "${CACHE_DIR}:/fastembed_cache"
  -e "FASTEMBED_CACHE_PATH=/fastembed_cache"
  -e "HF_HOME=/fastembed_cache"
  -e "HUGGINGFACE_HUB_CACHE=/fastembed_cache/hub"
  -e "FASTEMBED_SPARSE_MODEL=${MODEL_NAME}"
)

for key in HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy HF_ENDPOINT; do
  if [[ -n "${!key:-}" ]]; then
    docker_args+=(-e "${key}=${!key}")
  fi
done

echo "[FastEmbed Warmup] model=${MODEL_NAME}"
echo "[FastEmbed Warmup] cache=${CACHE_DIR}"
echo "[FastEmbed Warmup] image=${PY_IMAGE}"

docker "${docker_args[@]}" "${PY_IMAGE}" sh -lc '
python -m pip install --no-cache-dir --quiet "fastembed>=0.7.4,<0.8" &&
python - <<'"'"'PY'"'"'
import os
from fastembed import SparseTextEmbedding

model = os.getenv("FASTEMBED_SPARSE_MODEL", "prithivida/Splade_PP_en_v1")
encoder = SparseTextEmbedding(model_name=model)
list(encoder.embed(["warmup sparse retrieval model"]))
print("[FastEmbed Warmup] download and local cache ready:", model)
PY
'

