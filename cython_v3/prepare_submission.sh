#!/bin/sh
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

OUT_ZIP="${1:-cython_v3_submission.zip}"
TMP_DIR="$SCRIPT_DIR/.submission_tmp"
PYTHON_BIN="${PYTHON_BIN:-$SCRIPT_DIR/../.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="python3"
fi

./build_rust.sh
RUST_LIB="./rust_core/target/release/librust_core.so"
if [ ! -f "$RUST_LIB" ]; then
    echo "ERROR: $RUST_LIB wurde nicht gefunden." >&2
    exit 1
fi

rm -rf "$TMP_DIR"
mkdir -p "$TMP_DIR/cython_core"

"$PYTHON_BIN" - <<PY
import py_compile
from pathlib import Path

src = Path("$SCRIPT_DIR")
dst = Path("$TMP_DIR")

py_compile.compile(
    str(src / "client_cython.py"),
    cfile=str(dst / "client_cython.pyc"),
    doraise=True,
)
py_compile.compile(
    str(src / "cython_core" / "__init__.py"),
    cfile=str(dst / "cython_core" / "__init__.pyc"),
    doraise=True,
)
py_compile.compile(
    str(src / "cython_core" / "bridge.py"),
    cfile=str(dst / "cython_core" / "bridge.pyc"),
    doraise=True,
)
PY

cp ./start.sh "$TMP_DIR/"
cp ./run_client.sh "$TMP_DIR/"
cp "$RUST_LIB" "$TMP_DIR/librust_core.so"

(
    cd "$TMP_DIR"
    zip -r "$OUT_ZIP" \
        start.sh \
        run_client.sh \
        client_cython.pyc \
        librust_core.so \
        cython_core
)

mv "$TMP_DIR/$OUT_ZIP" "$SCRIPT_DIR/$OUT_ZIP"
rm -rf "$TMP_DIR"

echo "Created $SCRIPT_DIR/$OUT_ZIP"
