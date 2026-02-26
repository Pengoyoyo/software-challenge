#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PIRANHAS_MOVE_HARD_CAP_NS="${PIRANHAS_MOVE_HARD_CAP_NS:-1800000000}"
exec "$SCRIPT_DIR/crackedline-bot" "$@"
