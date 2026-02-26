#!/bin/sh
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="$SCRIPT_DIR/Cargo.toml"
BIN="$SCRIPT_DIR/target/release/piranhas-bot"

needs_rebuild=false
if [ ! -x "$BIN" ]; then
    needs_rebuild=true
else
    if find "$SCRIPT_DIR/src" "$MANIFEST" "$SCRIPT_DIR/Cargo.lock" -type f -newer "$BIN" | grep -q .; then
        needs_rebuild=true
    fi
fi

if [ "$needs_rebuild" = true ]; then
    cargo build --release --manifest-path "$MANIFEST"
fi

exec "$BIN" "$@"
