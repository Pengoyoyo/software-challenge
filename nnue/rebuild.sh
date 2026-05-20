#!/bin/bash
# Exports model.pt → weights.bin → rebuilds rust_v3 with NNUE embedded.
# Usage: ./nnue/rebuild.sh [path/to/model.pt]
# Default model path: nnue/data/model.pt

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$SCRIPT_DIR/.."
MODEL="${1:-$SCRIPT_DIR/data/model.pt}"
WEIGHTS="$ROOT/bots/rust_v3/src/weights.bin"

if [ ! -f "$MODEL" ]; then
    echo "ERROR: model not found at $MODEL"
    echo "Usage: ./nnue/rebuild.sh [path/to/model.pt]"
    exit 1
fi

echo "=== NNUE Rebuild ==="
echo "Model:   $MODEL"
echo "Weights: $WEIGHTS"
echo ""

# 1. Export weights
echo "[1/2] Exporting weights..."
# Use venv python if available, else fall back to system python3
PYTHON="python3"
if [ -f "$SCRIPT_DIR/training/.venv/bin/python" ]; then
    PYTHON="$SCRIPT_DIR/training/.venv/bin/python"
elif [ -f "$SCRIPT_DIR/.venv/bin/python" ]; then
    PYTHON="$SCRIPT_DIR/.venv/bin/python"
fi
"$PYTHON" "$SCRIPT_DIR/training/export.py" --model "$MODEL" --out "$WEIGHTS" --l1 128 --l2 16

# 2. Rebuild Rust binary
echo ""
echo "[2/2] Building rust_v3 with NNUE..."
cargo build --release --manifest-path "$ROOT/bots/rust_v3/Cargo.toml"

echo ""
echo "Done! Binary: $ROOT/bots/rust_v3/target/release/piranhas-bot-v2"
