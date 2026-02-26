from __future__ import annotations

import ctypes
from pathlib import Path
from typing import Iterable


class RustEngineBridge:
    def __init__(self) -> None:
        self._lib = self._load_library()
        self._bind_symbols()
        self._engine = self._lib.engine_new()
        if not self._engine:
            raise RuntimeError("engine_new() returned null pointer")

    @staticmethod
    def _candidate_paths() -> list[Path]:
        base = Path(__file__).resolve().parent.parent
        names = ["librust_core.so", "librust_core.dylib", "rust_core.dll"]
        roots = [
            base / "rust_core" / "target" / "release",
            base / "cython_core",
            base,
        ]

        paths: list[Path] = []
        for root in roots:
            for name in names:
                paths.append(root / name)
        return paths

    @classmethod
    def _load_library(cls) -> ctypes.CDLL:
        for path in cls._candidate_paths():
            if path.exists():
                return ctypes.CDLL(str(path))

        searched = "\n".join(str(p) for p in cls._candidate_paths())
        raise FileNotFoundError(
            "Rust engine library not found. Build it first with `./build_rust.sh`. "
            f"Searched:\n{searched}"
        )

    def _bind_symbols(self) -> None:
        self._lib.engine_new.restype = ctypes.c_void_p

        self._lib.engine_free.argtypes = [ctypes.c_void_p]
        self._lib.engine_free.restype = None

        self._lib.engine_choose_move.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_uint8,
            ctypes.c_uint16,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.POINTER(ctypes.c_uint8),
        ]
        self._lib.engine_choose_move.restype = ctypes.c_int

    def choose_move(
        self,
        board_codes: Iterable[int],
        current_player: int,
        turn: int,
        time_ms: int,
    ) -> tuple[int, int] | None:
        values = list(board_codes)
        if len(values) != 100:
            raise ValueError(f"Expected 100 board entries, got {len(values)}")

        board_arr = (ctypes.c_uint8 * 100)(*[(v & 0xFF) for v in values])
        out_from = ctypes.c_uint8(0)
        out_to = ctypes.c_uint8(0)

        ok = self._lib.engine_choose_move(
            self._engine,
            board_arr,
            ctypes.c_uint8(2 if current_player == 2 else 1),
            ctypes.c_uint16(max(0, min(turn, 65535))),
            ctypes.c_uint32(max(1, time_ms)),
            ctypes.byref(out_from),
            ctypes.byref(out_to),
        )

        if ok != 1:
            return None

        return int(out_from.value), int(out_to.value)

    def close(self) -> None:
        if getattr(self, "_engine", None):
            self._lib.engine_free(self._engine)
            self._engine = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
