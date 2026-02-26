#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rust_bridge import RustEngineProcess
from state_adapter import (
    BLUE,
    BLUE_1,
    BLUE_2,
    BLUE_3,
    EMPTY,
    ExternalState,
    RED,
    RED_1,
    RED_2,
    RED_3,
    make_piece,
    opponent,
)

BOARD_SIZE = 10
NUM_SQUARES = BOARD_SIZE * BOARD_SIZE
ROUND_CONNECT_NONE = 0
ROUND_CONNECT_BOTH = 3


def xy_to_sq(x: int, y: int) -> int:
    return y * BOARD_SIZE + x


def splitmix64(state: int) -> tuple[int, int]:
    state = (state + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
    z = state
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    z ^= z >> 31
    return state, z & 0xFFFFFFFFFFFFFFFF


def setup_initial_board(seed: int) -> list[int]:
    board = [EMPTY] * NUM_SQUARES

    edge_values = [1, 2, 1, 3, 1, 2, 1, 3]
    for y in range(1, 9):
        v = edge_values[y - 1]
        board[xy_to_sq(0, y)] = make_piece(RED, v)
        board[xy_to_sq(9, y)] = make_piece(RED, v)
    for x in range(1, 9):
        v = edge_values[x - 1]
        board[xy_to_sq(x, 0)] = make_piece(BLUE, v)
        board[xy_to_sq(x, 9)] = make_piece(BLUE, v)

    rng_state = 0x9E3779B97F4A7C15 ^ (seed & 0xFFFFFFFF)

    def next_inner_square() -> int:
        nonlocal rng_state
        while True:
            rng_state, r = splitmix64(rng_state)
            x = 2 + (r % 6)
            y = 2 + ((r >> 8) % 6)
            sq = xy_to_sq(int(x), int(y))
            if board[sq] == EMPTY:
                return sq

    board[next_inner_square()] = 7
    board[next_inner_square()] = 7
    return board


def owner_of_cell(cell: int) -> int:
    value = int(cell)
    if RED_1 <= value <= RED_3:
        return RED
    if BLUE_1 <= value <= BLUE_3:
        return BLUE
    return 0


def piece_value(cell: int) -> int:
    value = int(cell)
    if RED_1 <= value <= RED_3:
        return value - RED_1 + 1
    if BLUE_1 <= value <= BLUE_3:
        return value - BLUE_1 + 1
    return 0


def neighbors(square: int) -> list[int]:
    x = square % BOARD_SIZE
    y = square // BOARD_SIZE
    out: list[int] = []
    for dx, dy in (
        (-1, -1),
        (0, -1),
        (1, -1),
        (-1, 0),
        (1, 0),
        (-1, 1),
        (0, 1),
        (1, 1),
    ):
        nx = x + dx
        ny = y + dy
        if 0 <= nx < BOARD_SIZE and 0 <= ny < BOARD_SIZE:
            out.append(xy_to_sq(nx, ny))
    return out


NEIGHBORS = [neighbors(sq) for sq in range(NUM_SQUARES)]


def is_connected(board: list[int], player: int) -> bool:
    pieces = [sq for sq, cell in enumerate(board) if owner_of_cell(cell) == player]
    if len(pieces) <= 1:
        return True

    seen = {pieces[0]}
    stack = [pieces[0]]
    while stack:
        cur = stack.pop()
        for nb in NEIGHBORS[cur]:
            if nb in seen:
                continue
            if owner_of_cell(board[nb]) == player:
                seen.add(nb)
                stack.append(nb)
    return len(seen) == len(pieces)


def largest_component_value(board: list[int], player: int) -> int:
    seen = [False] * NUM_SQUARES
    best = 0
    for sq in range(NUM_SQUARES):
        if seen[sq] or owner_of_cell(board[sq]) != player:
            continue
        seen[sq] = True
        stack = [sq]
        total = 0
        while stack:
            cur = stack.pop()
            total += piece_value(board[cur])
            for nb in NEIGHBORS[cur]:
                if seen[nb] or owner_of_cell(board[nb]) != player:
                    continue
                seen[nb] = True
                stack.append(nb)
        if total > best:
            best = total
    return best


def swarm_winner(board: list[int]) -> int:
    red_swarm = largest_component_value(board, RED)
    blue_swarm = largest_component_value(board, BLUE)
    if red_swarm > blue_swarm:
        return RED
    if blue_swarm > red_swarm:
        return BLUE
    return 0


def round_end_connection_outcome(state: ExternalState) -> int:
    if state.player_to_move != RED or state.turn <= 0:
        return ROUND_CONNECT_NONE

    red_connected = is_connected(state.board, RED)
    blue_connected = is_connected(state.board, BLUE)
    if red_connected and blue_connected:
        return ROUND_CONNECT_BOTH
    if red_connected:
        return RED
    if blue_connected:
        return BLUE
    return ROUND_CONNECT_NONE


def apply_move(state: ExternalState, from_sq: int, to_sq: int) -> None:
    piece = state.board[from_sq]
    state.board[from_sq] = EMPTY
    state.board[to_sq] = piece
    state.player_to_move = opponent(state.player_to_move)
    state.turn += 1


@dataclass
class GameResult:
    winner: int
    plies: int
    candidate_score: float


def play_game(
    *,
    base_engine: RustEngineProcess,
    cand_engine: RustEngineProcess,
    seed: int,
    candidate_as_red: bool,
    budget_ms: int,
    max_plies: int,
) -> GameResult:
    state = ExternalState(board=setup_initial_board(seed), player_to_move=RED, turn=0)
    winner = 0

    for _ in range(max_plies):
        stm = state.player_to_move
        use_cand = (candidate_as_red and stm == RED) or (not candidate_as_red and stm == BLUE)
        engine = cand_engine if use_cand else base_engine

        result = engine.search(state, budget_ms * 1_000_000)
        if not result.has_move:
            winner = opponent(stm)
            break

        apply_move(state, result.from_sq, result.to_sq)

        outcome = round_end_connection_outcome(state)
        if outcome in (RED, BLUE):
            winner = outcome
            break
        if outcome == ROUND_CONNECT_BOTH:
            winner = swarm_winner(state.board)
            break
        if state.turn >= 60:
            winner = swarm_winner(state.board)
            break

    cand_color = RED if candidate_as_red else BLUE
    if winner == 0:
        score = 0.5
    elif winner == cand_color:
        score = 1.0
    else:
        score = 0.0
    return GameResult(winner=winner, plies=state.turn, candidate_score=score)


def elo_to_score(elo: float) -> float:
    return 1.0 / (1.0 + 10.0 ** (-elo / 400.0))


def score_to_elo(score: float) -> float:
    p = min(max(score, 1e-6), 1.0 - 1e-6)
    return -400.0 * math.log10((1.0 / p) - 1.0)


def llr_increment(score: float, p0: float, p1: float) -> float:
    q0 = 1.0 - p0
    q1 = 1.0 - p1
    score = min(max(score, 0.0), 1.0)
    return score * math.log(p1 / p0) + (1.0 - score) * math.log(q1 / q0)


def resolve_binary_path(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded
    return (ROOT / expanded).resolve()


def resolve_optional_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded
    return (ROOT / expanded).resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description="Selfplay SPRT gate (candidate vs base).")
    parser.add_argument("--base-binary", type=Path, default=Path("artifacts/piranhas-base"))
    parser.add_argument("--cand-binary", type=Path, default=Path("artifacts/piranhas-cand"))
    parser.add_argument("--move-budget-ms", type=int, default=250)
    parser.add_argument("--seed", type=int, default=23)
    parser.add_argument("--max-games", type=int, default=400)
    parser.add_argument("--max-plies", type=int, default=60)
    parser.add_argument("--elo0", type=float, default=0.0)
    parser.add_argument("--elo1", type=float, default=35.0)
    parser.add_argument("--alpha", type=float, default=0.05)
    parser.add_argument("--beta", type=float, default=0.05)
    parser.add_argument("--tt-mb", type=int, default=2048)
    parser.add_argument("--base-eval-profile", type=str, default="")
    parser.add_argument("--cand-eval-profile", type=str, default="")
    parser.add_argument("--base-eval-weights", type=Path, default=None)
    parser.add_argument("--cand-eval-weights", type=Path, default=None)
    args = parser.parse_args()

    if args.tt_mb > 0:
        import os

        os.environ["PIRANHAS_TT_MB"] = str(args.tt_mb)

    base_binary = resolve_binary_path(args.base_binary)
    cand_binary = resolve_binary_path(args.cand_binary)
    base_weights = resolve_optional_path(args.base_eval_weights)
    cand_weights = resolve_optional_path(args.cand_eval_weights)

    missing: list[tuple[str, Path]] = []
    if not base_binary.exists():
        missing.append(("base", base_binary))
    if not cand_binary.exists():
        missing.append(("cand", cand_binary))
    if base_weights is not None and not base_weights.exists():
        missing.append(("base_eval_weights", base_weights))
    if cand_weights is not None and not cand_weights.exists():
        missing.append(("cand_eval_weights", cand_weights))
    if missing:
        print("Missing binary path(s):", file=sys.stderr)
        for label, path in missing:
            print(f"  - {label}: {path}", file=sys.stderr)
        print(
            "\nPrepare relative binaries, e.g.:",
            "\n  cargo build --release"
            "\n  mkdir -p artifacts"
            "\n  cp target/release/piranhas-rs-engine artifacts/piranhas-base"
            "\n  cp target/release/piranhas-rs-engine artifacts/piranhas-cand",
            file=sys.stderr,
        )
        return 2

    base_env: dict[str, str] = {}
    cand_env: dict[str, str] = {}
    if args.base_eval_profile.strip():
        base_env["PIRANHAS_EVAL_PROFILE"] = args.base_eval_profile.strip()
    if args.cand_eval_profile.strip():
        cand_env["PIRANHAS_EVAL_PROFILE"] = args.cand_eval_profile.strip()
    if base_weights is not None:
        base_env["PIRANHAS_EVAL_WEIGHTS_FILE"] = str(base_weights)
    if cand_weights is not None:
        cand_env["PIRANHAS_EVAL_WEIGHTS_FILE"] = str(cand_weights)

    base_engine = RustEngineProcess(binary=base_binary, env_overrides=base_env)
    cand_engine = RustEngineProcess(binary=cand_binary, env_overrides=cand_env)

    p0 = elo_to_score(args.elo0)
    p1 = elo_to_score(args.elo1)
    upper = math.log((1.0 - args.beta) / args.alpha)
    lower = math.log(args.beta / (1.0 - args.alpha))

    wins = 0
    losses = 0
    draws = 0
    llr = 0.0

    try:
        games = 0
        pair_idx = 0
        while games < args.max_games:
            game_seed = args.seed + pair_idx
            pair_idx += 1
            for candidate_as_red in (True, False):
                if games >= args.max_games:
                    break
                result = play_game(
                    base_engine=base_engine,
                    cand_engine=cand_engine,
                    seed=game_seed,
                    candidate_as_red=candidate_as_red,
                    budget_ms=args.move_budget_ms,
                    max_plies=args.max_plies,
                )

                if result.candidate_score >= 1.0:
                    wins += 1
                elif result.candidate_score <= 0.0:
                    losses += 1
                else:
                    draws += 1

                llr += llr_increment(result.candidate_score, p0, p1)
                games = wins + losses + draws
                mean_score = (wins + 0.5 * draws) / max(1, games)
                elo_hat = score_to_elo(mean_score)

                print(
                    f"g={games:>4} W/L/D={wins}/{losses}/{draws} "
                    f"score={mean_score:.3f} elo_hat={elo_hat:+.1f} llr={llr:+.3f} "
                    f"bounds=[{lower:+.3f},{upper:+.3f}]"
                )

                if llr >= upper:
                    print("SPRT: ACCEPT H1")
                    return 0
                if llr <= lower:
                    print("SPRT: ACCEPT H0")
                    return 1
    finally:
        base_engine.close()
        cand_engine.close()

    print("SPRT: INCONCLUSIVE (max games reached)")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
