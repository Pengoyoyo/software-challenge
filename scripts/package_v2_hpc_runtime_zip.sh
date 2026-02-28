#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT_ZIP="${1:-$ROOT_DIR/dist/software_challenge_v2_hpc_runtime_${STAMP}.zip}"
STAGE_DIR="$(mktemp -d /tmp/v2_hpc_runtime_stage_XXXXXX)"
PKG_DIR="$STAGE_DIR/Software-Challenge"

mkdir -p "$(dirname "$OUT_ZIP")"
mkdir -p "$PKG_DIR"

cleanup() {
    rm -rf "$STAGE_DIR"
}
trap cleanup EXIT

copy_item() {
    local rel="$1"
    local src="$ROOT_DIR/$rel"
    if [[ ! -e "$src" ]]; then
        echo "missing required path: $rel" >&2
        exit 1
    fi
    rsync -a \
        --exclude '__pycache__/' \
        --exclude '*.pyc' \
        --exclude '*.pyo' \
        --exclude '.pytest_cache/' \
        --exclude '.mypy_cache/' \
        "$src" "$PKG_DIR/"
}

# Minimal notwendige Laufzeitstruktur für den HPC-v2-Tuner.
copy_item "benchmark.py"
copy_item "requirements.txt"
copy_item "custom_bot_paths.json"
copy_item "README.MD"
copy_item "scripts"
copy_item "bots"
copy_item "opponent_bots"
copy_item "server"

(cd "$STAGE_DIR" && zip -r "$OUT_ZIP" "Software-Challenge" >/dev/null)

echo "created: $OUT_ZIP"
