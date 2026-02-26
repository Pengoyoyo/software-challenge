#!/usr/bin/env python3
"""
Wrapper für CrackedLine@v1 (Simon's Rust+Python Bot).
Führt logic.py mit dem system Python aus (socha muss installiert sein).
Die Rust-Binary piranhas-rs-engine wird automatisch gefunden.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BIN = ROOT / "target" / "release" / "piranhas-rs-engine"

if not BIN.exists():
    sys.exit(f"[run.py] Rust binary nicht gefunden: {BIN}\nBauen mit: cd {ROOT} && cargo build --release")

# Wechsle ins Bot-Verzeichnis damit relative Imports in logic.py funktionieren
os.chdir(ROOT)

# Füge das Verzeichnis zum Python-Pfad hinzu
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Client_skip_build damit logic.py nicht nochmal versucht zu bauen
os.environ["CLIENT_SKIP_BUILD"] = "1"

# logic.py direkt ausführen
logic_path = ROOT / "logic.py"
exec(
    compile(logic_path.read_text(encoding="utf-8"), str(logic_path), "exec"),
    {"__name__": "__main__", "__file__": str(logic_path)},
)
