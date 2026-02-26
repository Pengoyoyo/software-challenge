#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$ROOT_DIR/build_rust.sh"

cd "$ROOT_DIR"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/../.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="python"
fi

"$PYTHON_BIN" setup.py build_ext --inplace
