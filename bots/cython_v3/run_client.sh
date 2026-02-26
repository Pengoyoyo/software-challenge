#!/bin/sh
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f "./librust_core.so" ]; then
    echo "ERROR: librust_core.so fehlt. Bitte Paket neu bauen." >&2
    exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    if command -v python >/dev/null 2>&1; then
        PYTHON_BIN="python"
    else
        echo "ERROR: Kein Python-Interpreter gefunden (python3/python)." >&2
        exit 1
    fi
fi

if [ ! -f "./client_cython.pyc" ]; then
    echo "ERROR: client_cython.pyc fehlt. Bitte Paket neu bauen." >&2
    exit 1
fi

exec "$PYTHON_BIN" ./client_cython.pyc "$@"
