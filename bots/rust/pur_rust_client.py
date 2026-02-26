#!/usr/bin/env python3
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(SCRIPT_DIR, "target", "release", "piranhas-bot")

if not os.path.isfile(BIN):
    sys.exit(f"Error: binary not found at {BIN}\nRun: cd {SCRIPT_DIR} && cargo build --release")

args = sys.argv[1:]

if "-h" not in args and "--host" not in args:
    args = ["-h", "localhost"] + args

if "-p" not in args and "--port" not in args:
    args = ["-p", "13050"] + args

os.execv(BIN, [BIN] + args)
