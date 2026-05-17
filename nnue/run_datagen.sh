#!/bin/bash
# Parallel datagen runner
# Usage: ./run_datagen.sh <threads> <games_per_thread> <time_ms> <out.bin>
# Example: ./run_datagen.sh 8 1000 200 data/train.bin

set -e

THREADS=${1:-4}
GAMES=${2:-1000}
TIME_MS=${3:-200}
OUT=${4:-data/train.bin}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATAGEN="$SCRIPT_DIR/datagen"

if [ ! -x "$DATAGEN" ]; then
    echo "ERROR: datagen binary not found at $DATAGEN"
    exit 1
fi

mkdir -p "$(dirname "$OUT")"
mkdir -p "$SCRIPT_DIR/tmp_shards"

echo "============================================"
echo " Parallel datagen"
echo " Threads:          $THREADS"
echo " Games per thread: $GAMES"
echo " Time per pos:     ${TIME_MS}ms"
echo " Total games:      $((THREADS * GAMES))"
echo " Output:           $OUT"
echo "============================================"
echo ""

PIDS=()
for i in $(seq 1 $THREADS); do
    SHARD="$SCRIPT_DIR/tmp_shards/shard_${i}.bin"
    "$DATAGEN" "$GAMES" "$TIME_MS" "$SHARD" &
    PIDS+=($!)
    echo "[Thread $i] PID $! started"
done

echo ""
echo "Waiting for all threads to finish..."

FAILED=0
for i in "${!PIDS[@]}"; do
    PID=${PIDS[$i]}
    if wait "$PID"; then
        echo "[Thread $((i+1))] done"
    else
        echo "[Thread $((i+1))] FAILED (PID $PID)"
        FAILED=1
    fi
done

if [ $FAILED -ne 0 ]; then
    echo "ERROR: one or more threads failed"
    exit 1
fi

echo ""
echo "Merging shards into $OUT..."
cat "$SCRIPT_DIR/tmp_shards"/shard_*.bin >> "$OUT"
rm -rf "$SCRIPT_DIR/tmp_shards"

BYTES=$(wc -c < "$OUT")
SAMPLES=$((BYTES / 107))
echo "Done. $SAMPLES samples total in $OUT ($(du -sh "$OUT" | cut -f1))"
