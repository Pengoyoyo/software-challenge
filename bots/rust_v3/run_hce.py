#!/usr/bin/env python3
"""Bot mit Hand-crafted Evaluation (kein NNUE)."""
import os, sys
from pathlib import Path

RUST_BIN = str(Path(__file__).parent / "target" / "release" / "piranhas-bot-v2")

args = sys.argv[1:]
port = None
host = "localhost"
reservation = None

i = 0
while i < len(args):
    if args[i] in ("--port", "-p") and i + 1 < len(args):
        port = args[i + 1]; i += 2
    elif args[i] in ("--host", "-h") and i + 1 < len(args):
        host = args[i + 1]; i += 2
    elif args[i] in ("--reservation", "-r") and i + 1 < len(args):
        reservation = args[i + 1]; i += 2
    else:
        i += 1

cmd = [RUST_BIN, "-h", host, "-p", str(port), "--no-nnue"]
if reservation:
    cmd += ["-r", reservation]

os.execv(RUST_BIN, cmd)
