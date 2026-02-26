from __future__ import annotations

import atexit
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any

from rust_bridge import RustEngineProcess, format_telemetry
from state_adapter import EMPTY, RED, ExternalState, Move, from_game_state, moves_to_lookup

try:
    from socha.starter import Starter
    from socha.api.networking.game_client import IClientHandler
except Exception:  # pragma: no cover - optional runtime dependency for tests
    Starter = None  # type: ignore[assignment]

    class IClientHandler:  # type: ignore[override]
        pass


class Logic(IClientHandler):
    def __init__(self) -> None:
        self.game_state = None

    def on_update(self, game_state: Any) -> None:
        self.game_state = game_state

    def calculate_move(self) -> Any:  # pragma: no cover
        raise NotImplementedError


class PiranhasBot(Logic):
    def __init__(self) -> None:
        super().__init__()  # type: ignore[misc]
        self.engine = RustEngineProcess()
        configured_hard_limit = int(os.getenv("PIRANHAS_MOVE_HARD_CAP_NS", "1800000000"))
        self.hard_limit_ns = min(configured_hard_limit, 1_850_000_000)
        self.return_reserve_ns = int(os.getenv("PIRANHAS_RETURN_RESERVE_NS", "180000000"))
        self.min_search_window_ns = int(os.getenv("PIRANHAS_MIN_SEARCH_WINDOW_NS", "5000000"))
        self.engine_io_margin_ns = int(os.getenv("PIRANHAS_ENGINE_IO_MARGIN_NS", "60000000"))
        self.log_guard_ns = int(os.getenv("PIRANHAS_LOG_GUARD_NS", "40000000"))
        self.prewarm_timeout_ns = int(os.getenv("PIRANHAS_PREWARM_TIMEOUT_NS", "1600000000"))
        self.save_logs = os.getenv("PIRANHAS_SAVE_LOGS", "1") != "0"
        self.log_dir = os.getenv("PIRANHAS_LOG_DIR", "artifacts/game_logs")
        self.log_file_path = os.getenv("PIRANHAS_LOG_FILE", "").strip()
        self._log_file: Any | None = None
        self._open_log_file()
        self.debug_enabled = os.getenv("PIRANHAS_DEBUG", "1") != "0"
        self._printed_startup = False
        self._prewarmed = False
        self._prewarm_engine()
        atexit.register(self._close_engine)
        atexit.register(self._close_log_file)

    def _prewarm_engine(self) -> None:
        if self._prewarmed:
            return
        try:
            self.engine.start()
            # Trigger lazy loading paths (opening book / policy cache / tables) once
            # before the first timed move.
            warm_state = ExternalState(board=[EMPTY] * 100, player_to_move=RED, turn=0)
            self.engine.search(
                state=warm_state,
                budget_ns=1_000_000,
                root_moves_encoded=None,
                timeout_ns=self.prewarm_timeout_ns,
            )
            self._prewarmed = True
        except Exception as exc:
            if self.debug_enabled:
                self._emit_line(f"[BOT] prewarm skipped: {exc}")

    def _close_engine(self) -> None:
        try:
            self.engine.close()
        except Exception:
            pass

    def _open_log_file(self) -> None:
        if not self.save_logs:
            return

        try:
            if self.log_file_path:
                path = Path(self.log_file_path).expanduser()
                if not path.is_absolute():
                    path = Path(__file__).resolve().parent / path
            else:
                stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
                path = Path(__file__).resolve().parent / self.log_dir / f"game_{stamp}_{os.getpid()}.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = path.open("a", encoding="utf-8", buffering=1)
            self.log_file_path = str(path)
        except Exception as exc:
            self._log_file = None
            print(f"[BOT] warning: log file disabled ({exc})", file=sys.stderr, flush=True)

    def _close_log_file(self) -> None:
        handle = self._log_file
        self._log_file = None
        if handle is None:
            return
        try:
            handle.flush()
            handle.close()
        except Exception:
            pass

    def _emit_line(self, line: str) -> None:
        print(line, file=sys.stderr)
        handle = getattr(self, "_log_file", None)
        if handle is None:
            return
        try:
            handle.write(line + "\n")
        except Exception:
            self._log_file = None

    def _flush_logs(self) -> None:
        try:
            sys.stderr.flush()
        except Exception:
            pass
        handle = getattr(self, "_log_file", None)
        if handle is None:
            return
        try:
            handle.flush()
        except Exception:
            self._log_file = None

    def _search_with_deadline(
        self,
        *,
        state: Any,
        budget_ns: int,
        root_encoded: list[int],
        timeout_ns: int,
    ) -> Any | None:
        result_box: dict[str, Any] = {}
        error_box: dict[str, Exception] = {}
        engine = self.engine

        def worker() -> None:
            try:
                result_box["result"] = engine.search(
                    state=state,
                    budget_ns=budget_ns,
                    root_moves_encoded=root_encoded,
                    timeout_ns=timeout_ns,
                )
            except Exception as exc:
                error_box["error"] = exc

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(max(0.001, timeout_ns / 1_000_000_000))

        if thread.is_alive():
            if self.engine is engine:
                self.engine = RustEngineProcess(
                    root=engine.root,
                    binary=engine.binary,
                    env_overrides=getattr(engine, "env_overrides", None),
                )
            try:
                abort_fn = getattr(engine, "force_abort", None)
                if callable(abort_fn):
                    abort_fn()
                else:
                    engine.close()
            except Exception:
                pass
            return None

        if "error" in error_box:
            raise error_box["error"]
        return result_box.get("result")

    def _debug_startup(self) -> None:
        if self._printed_startup or not self.debug_enabled:
            return
        self._printed_startup = True
        self._emit_line(
            f"[BOT] startup rust_engine={self.engine.binary} "
            f"hard_limit_ns={self.hard_limit_ns} reserve_ns={self.return_reserve_ns} "
            f"min_search_window_ns={self.min_search_window_ns} io_margin_ns={self.engine_io_margin_ns} "
            f"log_guard_ns={self.log_guard_ns}"
        )
        if self.log_file_path:
            self._emit_line(
                f"[BOT] game_log_file={self.log_file_path}"
            )
        self._flush_logs()

    def _fallback(self, move_lookup: dict[tuple[int, int], Any]) -> Any:
        key = min(move_lookup)
        return move_lookup[key]

    @staticmethod
    def _sq_to_xy(square: int) -> tuple[int, int]:
        return square % 10, square // 10

    @staticmethod
    def _direction_name_and_arrow(from_sq: int, to_sq: int) -> tuple[str, str]:
        fx, fy = PiranhasBot._sq_to_xy(from_sq)
        tx, ty = PiranhasBot._sq_to_xy(to_sq)
        dx = tx - fx
        dy = ty - fy
        sx = 0 if dx == 0 else (1 if dx > 0 else -1)
        sy = 0 if dy == 0 else (1 if dy > 0 else -1)
        mapping: dict[tuple[int, int], tuple[str, str]] = {
            (-1, -1): ("UpLeft", "↖"),
            (0, -1): ("Up", "↑"),
            (1, -1): ("UpRight", "↗"),
            (-1, 0): ("Left", "←"),
            (1, 0): ("Right", "→"),
            (-1, 1): ("DownLeft", "↙"),
            (0, 1): ("Down", "↓"),
            (1, 1): ("DownRight", "↘"),
        }
        return mapping.get((sx, sy), ("Unknown", "•"))

    def _print_move_block(
        self,
        *,
        turn: int,
        legal_count: int,
        team: int,
        chosen_key: tuple[int, int],
        traces: list[Any],
    ) -> None:
        self._emit_line(f"=== Zug {turn} ===")
        self._emit_line(f"Rust Search: {legal_count} moves, team={team}")
        for trace in traces:
            elapsed_s = trace.elapsed_ns_delta / 1_000_000_000
            self._emit_line(
                f"d{trace.depth}: {trace.score} | {trace.nodes_delta}n {trace.tt_hits_delta}h {trace.nps_iter}nps {elapsed_s:.2f}s"
            )
        from_sq, to_sq = chosen_key
        x, y = self._sq_to_xy(from_sq)
        direction_name, arrow = self._direction_name_and_arrow(from_sq, to_sq)
        self._emit_line(f"-> ({x}, {y}) ({direction_name} {arrow})")
        self._flush_logs()

    def calculate_move(self) -> Any:
        self._debug_startup()

        move_start_ns = time.monotonic_ns()
        hard_deadline = move_start_ns + self.hard_limit_ns

        game_state = getattr(self, "game_state", None)
        if game_state is None:
            return None

        possible_moves = list(game_state.possible_moves())
        if not possible_moves:
            return None

        move_lookup = moves_to_lookup(possible_moves, board=getattr(game_state, "board", None))
        if not move_lookup:
            if self.debug_enabled:
                self._emit_line(
                    "[BOT] warning: could not map legal moves, fallback to first socha move"
                )
            return possible_moves[0]

        now_ns = time.monotonic_ns()
        if now_ns + self.min_search_window_ns >= hard_deadline:
            if self.debug_enabled:
                self._emit_line("[BOT] fallback before search: hardcap guard")
            return self._fallback(move_lookup)

        try:
            state = from_game_state(game_state)
        except Exception as exc:
            if self.debug_enabled:
                self._emit_line(f"[BOT] state conversion failed: {exc}")
            return self._fallback(move_lookup)

        now_ns = time.monotonic_ns()
        remaining_hard = hard_deadline - now_ns
        if remaining_hard <= self.min_search_window_ns:
            return self._fallback(move_lookup)

        budget_ns = remaining_hard - self.return_reserve_ns
        if budget_ns < self.min_search_window_ns:
            budget_ns = self.min_search_window_ns
        if budget_ns >= remaining_hard:
            budget_ns = max(self.min_search_window_ns, remaining_hard - 1_000_000)

        root_keys = sorted(move_lookup)
        root_encoded = [((frm << 7) | to) for frm, to in root_keys]

        chosen_key: tuple[int, int] | None = None
        note = ""
        traces: list[Any] = []
        result_team = int(state.player_to_move)
        result_legal_count = len(root_keys)

        try:
            request_timeout_ns = hard_deadline - time.monotonic_ns() - self.engine_io_margin_ns
            if request_timeout_ns <= 1_000_000:
                if self.debug_enabled:
                    self._emit_line(
                        "[BOT] fallback before rust call: insufficient io timeout budget"
                    )
                return self._fallback(move_lookup)
            result = self._search_with_deadline(
                state=state,
                budget_ns=budget_ns,
                root_encoded=root_encoded,
                timeout_ns=request_timeout_ns,
            )
            if result is None:
                if self.debug_enabled:
                    self._emit_line("[BOT] fallback: search watchdog timeout")
                return self._fallback(move_lookup)
            traces = list(result.iterations)
            result_team = int(result.team)
            result_legal_count = int(result.legal_root_count)
            if result.has_move:
                key = (result.from_sq, result.to_sq)
                if key in move_lookup:
                    chosen_key = key
                else:
                    note = "unmapped_move"
            else:
                note = "no_move"

            if self.debug_enabled:
                wall_elapsed = time.monotonic_ns() - move_start_ns
                margin_ms = (self.hard_limit_ns - wall_elapsed) / 1_000_000
                mapped = chosen_key is not None
                if time.monotonic_ns() + self.log_guard_ns < hard_deadline:
                    self._emit_line(
                        f"[BOT] turn={state.turn} mapped={mapped} "
                        f"wall_ms={wall_elapsed / 1_000_000:.2f} margin_ms={margin_ms:.2f} "
                        f"{format_telemetry(result)}"
                        + (f" note={note}" if note else "")
                    )
        except Exception as exc:
            if self.debug_enabled:
                self._emit_line(f"[BOT] rust search failed: {exc}")

        if chosen_key is None:
            chosen_key = min(move_lookup)

        if time.monotonic_ns() + 2_000_000 >= hard_deadline:
            return move_lookup[chosen_key]

        traces_to_print = traces
        if time.monotonic_ns() + self.log_guard_ns >= hard_deadline:
            traces_to_print = []

        self._print_move_block(
            turn=int(getattr(state, "turn", 0)),
            legal_count=result_legal_count,
            team=result_team,
            chosen_key=chosen_key,
            traces=traces_to_print,
        )

        return move_lookup[chosen_key]


if __name__ == "__main__":
    if Starter is None:
        raise SystemExit("socha is not installed. Install runtime dependency `socha`.")
    Starter(logic=PiranhasBot())
