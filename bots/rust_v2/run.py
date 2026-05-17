#!/usr/bin/env python3
"""Thin wrapper: translates --port N  →  -h localhost -p N for the Rust binary."""
import os, sys
from pathlib import Path

RUST_BIN = str(Path(__file__).parent / "target" / "release" / "piranhas-bot-v2")

args = sys.argv[1:]
port = None
i = 0
while i < len(args):
    if args[i] == "--port" and i + 1 < len(args):
        port = args[i + 1]
        i += 2
    else:
        i += 1

cmd = [RUST_BIN, "-h", "localhost", "-p", str(port)] if port else [RUST_BIN]
os.execv(RUST_BIN, cmd)
