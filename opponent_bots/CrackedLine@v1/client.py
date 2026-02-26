from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
BIN = ROOT / "target" / "release" / "piranhas-rs-engine"


def _is_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _reexec_into_venv_if_available() -> None:
    if _is_truthy(os.getenv("CLIENT_NO_REEXEC")):
        return
    if not VENV_PYTHON.exists():
        return

    current = Path(sys.executable).resolve()
    venv = VENV_PYTHON.resolve()
    if current == venv:
        return

    env = os.environ.copy()
    env["CLIENT_NO_REEXEC"] = "1"
    os.execvpe(str(venv), [str(venv), str(ROOT / "client.py"), *sys.argv[1:]], env)


def _newest_source_mtime() -> float:
    newest = 0.0
    for path in [ROOT / "Cargo.toml", ROOT / "logic.py", ROOT / "rust_bridge.py", ROOT / "state_adapter.py"]:
        if path.exists():
            newest = max(newest, path.stat().st_mtime)

    src_dir = ROOT / "src"
    if src_dir.exists():
        for path in src_dir.rglob("*.rs"):
            newest = max(newest, path.stat().st_mtime)
    return newest


def _ensure_rust_build() -> None:
    if _is_truthy(os.getenv("CLIENT_SKIP_BUILD")):
        return

    needs_build = not BIN.exists()
    if not needs_build:
        needs_build = BIN.stat().st_mtime < _newest_source_mtime()

    if not needs_build and not _is_truthy(os.getenv("CLIENT_FORCE_BUILD")):
        return

    cmd = ["cargo", "build", "--release", "--manifest-path", str(ROOT / "Cargo.toml")]
    subprocess.run(cmd, cwd=ROOT, check=True)


def _run_logic() -> int:
    env = os.environ.copy()
    env.setdefault("PIRANHAS_DEBUG", "1")
    cmd = [sys.executable, "logic.py", *sys.argv[1:]]
    return subprocess.run(cmd, cwd=ROOT, env=env).returncode


def main() -> int:
    _reexec_into_venv_if_available()

    try:
        _ensure_rust_build()
    except FileNotFoundError as exc:
        print(f"[client] missing build tool: {exc}", file=sys.stderr)
        print("[client] install rust/cargo or set CLIENT_SKIP_BUILD=1", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"[client] rust build failed with exit code {exc.returncode}", file=sys.stderr)
        return exc.returncode

    return _run_logic()


if __name__ == "__main__":
    raise SystemExit(main())
