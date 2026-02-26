#!/usr/bin/env python3
"""Package all bots into competition-ready zip files (no source code)."""

import subprocess
import sys
import zipfile
from pathlib import Path

BASE = Path(__file__).parent.parent
OUT = BASE / "submissions"
OUT.mkdir(exist_ok=True)

# ── Dependency wheels (reused from existing my_player.zip) ───────────────────
print("Extracting dependency wheels from my_player.zip...")
with zipfile.ZipFile(BASE / "submissions" / "my_player.zip") as zf:
    wheel_entries = [n for n in zf.namelist() if "dependencies/" in n and n.endswith(".whl")]
    wheels = {Path(n).name: zf.read(n) for n in wheel_entries}
print(f"  Found: {list(wheels.keys())}")

# ── start.sh templates ───────────────────────────────────────────────────────
START_SH_PY = """\
#!/bin/sh
set -e
export XDG_CACHE_HOME=./my_player/.pip_cache
export PYTHONPATH=./my_player/packages:./my_player:$PYTHONPATH

pip install --no-index --find-links=./my_player/dependencies/ \\
    socha xsdata setuptools \\
    --target=./my_player/packages/ \\
    --cache-dir=./my_player/.pip_cache

python3 ./my_player/logic.py "$@"
"""

START_SH_PYC = START_SH_PY.replace("logic.py", "logic.pyc")

START_SH_RUST = """\
#!/bin/sh
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
chmod +x "$SCRIPT_DIR/my_player/piranhas-bot"
exec "$SCRIPT_DIR/my_player/piranhas-bot" "$@"
"""


# ── Helpers ──────────────────────────────────────────────────────────────────
def add_deps(zf: zipfile.ZipFile) -> None:
    for name, data in wheels.items():
        zf.writestr(f"my_player/dependencies/{name}", data)
    zf.mkdir("my_player/.pip_cache")


def add_so_files(zf: zipfile.ZipFile, src_dir: Path, zip_prefix: str) -> None:
    for so in sorted(src_dir.glob("*.so")):
        zf.write(so, f"{zip_prefix}/{so.name}")
        print(f"  + {so.name}")


def finish(name: str) -> None:
    size = (OUT / name).stat().st_size // 1024
    print(f"  -> {name} ({size} KB)")


# ── cython_v1 ────────────────────────────────────────────────────────────────
print("\nPackaging cython_v1...")
with zipfile.ZipFile(OUT / "submission_cython_v1.zip", "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("my_player/start.sh", START_SH_PY)
    zf.write(BASE / "bots/cython_v1/client_cython.py", "my_player/logic.py")
    zf.writestr("my_player/cython_core/__init__.py", "")
    add_so_files(zf, BASE / "bots/cython_v1/cython_core", "my_player/cython_core")
    add_deps(zf)
finish("submission_cython_v1.zip")

# ── cython_v2 ────────────────────────────────────────────────────────────────
print("\nPackaging cython_v2...")
with zipfile.ZipFile(OUT / "submission_cython_v2.zip", "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("my_player/start.sh", START_SH_PY)
    zf.write(BASE / "bots/cython_v2/client_cython.py", "my_player/logic.py")
    zf.writestr("my_player/cython_core/__init__.py", "")
    add_so_files(zf, BASE / "bots/cython_v2/cython_core", "my_player/cython_core")
    add_deps(zf)
finish("submission_cython_v2.zip")

# ── cython_v3 ────────────────────────────────────────────────────────────────
print("\nBuilding cython_v3 bridge_cy.so...")
venv_python = BASE / ".venv/bin/python"
build_python = str(venv_python) if venv_python.exists() else sys.executable
result = subprocess.run(
    [build_python, "setup.py", "build_ext", "--inplace"],
    cwd=BASE / "bots/cython_v3",
    capture_output=True,
    text=True,
)
bridge_so = next((BASE / "bots/cython_v3/cython_core").glob("bridge_cy*.so"), None)
if result.returncode != 0 or not bridge_so:
    print(f"  WARNING: build failed, falling back to bridge.py")
    bridge_so = None
else:
    print(f"  Built: {bridge_so.name}")

print("Packaging cython_v3...")
with zipfile.ZipFile(OUT / "submission_cython_v3.zip", "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("my_player/start.sh", START_SH_PY)
    zf.write(BASE / "bots/cython_v3/client_cython.py", "my_player/logic.py")
    zf.writestr("my_player/cython_core/__init__.py", "")
    if bridge_so:
        zf.write(bridge_so, f"my_player/cython_core/{bridge_so.name}")
        print(f"  + {bridge_so.name}")
    else:
        zf.write(BASE / "bots/cython_v3/cython_core/bridge.py", "my_player/cython_core/bridge.py")
        print("  + bridge.py (fallback)")
    zf.write(BASE / "bots/cython_v3/rust_core/target/release/librust_core.so", "my_player/librust_core.so")
    print("  + librust_core.so")
    add_deps(zf)
finish("submission_cython_v3.zip")

# ── cpp_client ───────────────────────────────────────────────────────────────
print("\nCompiling cpp_client Python files to bytecode...")
subprocess.run(
    [sys.executable, "-m", "compileall", "-b", "-q",
     str(BASE / "bots/cpp/bot"),
     str(BASE / "bots/cpp/logic.py")],
    check=True,
)

print("Packaging cpp_client...")
with zipfile.ZipFile(OUT / "submission_cpp_client.zip", "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("my_player/start.sh", START_SH_PYC)
    zf.write(BASE / "bots/cpp/logic.pyc", "my_player/logic.pyc")
    for pyc in sorted((BASE / "bots/cpp/bot").glob("*.pyc")):
        zf.write(pyc, f"my_player/bot/{pyc.name}")
        print(f"  + bot/{pyc.name}")
    so = BASE / "bots/cpp/build/cp312-cp312-linux_x86_64/_piranhas_core.cpython-312-x86_64-linux-gnu.so"
    zf.write(so, f"my_player/{so.name}")
    print(f"  + {so.name}")
    add_deps(zf)
finish("submission_cpp_client.zip")

# ── rust_bot (pure Rust binary) ──────────────────────────────────────────────
print("\nBuilding rust_bot...")
result = subprocess.run(
    ["cargo", "build", "--release"],
    cwd=BASE / "bots/rust",
    capture_output=True,
    text=True,
)
if result.returncode != 0:
    print(f"  WARNING: cargo build failed:\n{result.stderr[-500:]}")
else:
    print("  Built OK")

rust_bin = BASE / "bots/rust/target/release/piranhas-bot"
print("Packaging rust_bot...")
with zipfile.ZipFile(OUT / "submission_rust_bot.zip", "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("my_player/start.sh", START_SH_RUST)
    zf.write(rust_bin, "my_player/piranhas-bot")
    print(f"  + piranhas-bot")
    zf.write(BASE / "bots/rust/pur_rust_client.py", "my_player/pur_rust_client.py")
    print(f"  + pur_rust_client.py")
finish("submission_rust_bot.zip")

# ── Summary ──────────────────────────────────────────────────────────────────
print("\n" + "=" * 50)
for path in sorted(OUT.glob("submission_*.zip")):
    size = path.stat().st_size // 1024
    print(f"\n{path.name} ({size} KB)")
    with zipfile.ZipFile(path) as zf:
        entries = [
            n for n in zf.namelist()
            if not n.endswith("/") and ".pip_cache" not in n and "dependencies" not in n
        ]
        for e in entries:
            print(f"  {e}")
