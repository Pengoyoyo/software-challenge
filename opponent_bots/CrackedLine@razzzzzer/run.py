#!/usr/bin/env python3
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(SCRIPT_DIR, "FINAL", "crackedline-bot")

if not os.path.isfile(BIN):
    sys.exit(f"Error: binary not found at {BIN}")

os.environ.setdefault("PIRANHAS_MOVE_HARD_CAP_NS", "1800000000")

args = sys.argv[1:]

if "-h" not in args and "--host" not in args:
    args = ["-h", "localhost"] + args

if "-p" not in args and "--port" not in args:
    args = ["-p", "13050"] + args

os.execv(BIN, [BIN] + args)
