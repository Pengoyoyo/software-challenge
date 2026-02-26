#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass
import math
import os
from pathlib import Path
import struct
import sys
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rust_bridge import RustEngineProcess
from state_adapter import BLUE, EMPTY, ExternalState, RED, make_piece

BOARD_SIZE = 10
NUM_SQUARES = BOARD_SIZE * BOARD_SIZE
RECORD_STRUCT = struct.Struct("<QHHHhBHB")


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


def apply_move(state: ExternalState, from_sq: int, to_sq: int) -> None:
    piece = state.board[from_sq]
    state.board[from_sq] = EMPTY
    state.board[to_sq] = piece
    state.player_to_move = RED if state.player_to_move == BLUE else BLUE
    state.turn += 1


@dataclass
class Agg:
    move_counts: Counter[int]
    score_sum: int = 0
    depth_sum: int = 0
    samples: int = 0


def merge_samples(aggs: dict[int, Agg], samples: Iterable[tuple[int, int, int, int]]) -> None:
    for position_hash, best_encoded, score, depth in samples:
        agg = aggs[position_hash]
        agg.move_counts[int(best_encoded)] += 1
        agg.score_sum += int(score)
        agg.depth_sum += int(depth)
        agg.samples += 1


def seed_for_game(game_idx: int, seed_start: int, seed_count: int) -> int:
    if seed_count <= 0:
        return seed_start + game_idx
    return seed_start + (game_idx % seed_count)


def analyze_single_game(
    game_idx: int,
    seed: int,
    turn_max: int,
    analysis_budget_ms: int,
    playout_budget_ms: int,
) -> tuple[int, list[tuple[int, int, int, int]]]:
    samples: list[tuple[int, int, int, int]] = []
    engine = RustEngineProcess()
    try:
        state = ExternalState(board=setup_initial_board(seed), player_to_move=RED, turn=0)
        while state.turn <= turn_max:
            position_hash = engine.position_hash(state)

            analysis = engine.search(
                ExternalState(
                    board=list(state.board),
                    player_to_move=state.player_to_move,
                    turn=state.turn,
                ),
                analysis_budget_ms * 1_000_000,
            )
            if not analysis.has_move:
                break

            best_encoded = ((analysis.from_sq << 7) | analysis.to_sq)
            samples.append((position_hash, best_encoded, int(analysis.score), int(analysis.depth)))

            playout = engine.search(state, playout_budget_ms * 1_000_000)
            if not playout.has_move:
                break
            apply_move(state, playout.from_sq, playout.to_sq)
    finally:
        engine.close()

    return game_idx, samples


def default_workers() -> int:
    nproc = os.cpu_count() or 1
    return max(1, int(math.floor(nproc * 0.8)))


def build_cache(
    games: int,
    seed_start: int,
    seed_count: int,
    turn_max: int,
    analysis_budget_ms: int,
    playout_budget_ms: int,
    workers: int,
    progress_every: int,
) -> dict[int, tuple[int, int, int, int, int, int, int]]:
    aggs: dict[int, Agg] = defaultdict(lambda: Agg(move_counts=Counter()))
    total_games = max(1, games)
    every = max(1, progress_every)

    def iter_tasks() -> list[tuple[int, int]]:
        return [
            (game_idx, seed_for_game(game_idx, seed_start, seed_count))
            for game_idx in range(total_games)
        ]

    tasks = iter_tasks()
    done = 0

    if workers <= 1:
        for game_idx, seed in tasks:
            _, samples = analyze_single_game(
                game_idx=game_idx,
                seed=seed,
                turn_max=turn_max,
                analysis_budget_ms=analysis_budget_ms,
                playout_budget_ms=playout_budget_ms,
            )
            merge_samples(aggs, samples)
            done += 1
            if done % every == 0 or done == total_games:
                print(f"progress={done}/{total_games}")
    else:
        pending_by_idx: dict[int, list[tuple[int, int, int, int]]] = {}
        next_to_merge = 0

        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        analyze_single_game,
                        game_idx,
                        seed,
                        turn_max,
                        analysis_budget_ms,
                        playout_budget_ms,
                    ): game_idx
                    for game_idx, seed in tasks
                }

                while futures:
                    done_set, _ = wait(futures.keys(), return_when=FIRST_COMPLETED)
                    for fut in done_set:
                        idx = futures.pop(fut)
                        try:
                            game_idx, samples = fut.result()
                        except Exception:
                            game_idx, samples = idx, []
                        pending_by_idx[game_idx] = samples

                        while next_to_merge in pending_by_idx:
                            merge_samples(aggs, pending_by_idx.pop(next_to_merge))
                            next_to_merge += 1
                            done += 1
                            if done % every == 0 or done == total_games:
                                print(f"progress={done}/{total_games}")
        except (PermissionError, OSError):
            print("parallel_disabled=semaphore_permission_fallback")
            for game_idx, seed in tasks:
                _, samples = analyze_single_game(
                    game_idx=game_idx,
                    seed=seed,
                    turn_max=turn_max,
                    analysis_budget_ms=analysis_budget_ms,
                    playout_budget_ms=playout_budget_ms,
                )
                merge_samples(aggs, samples)
                done += 1
                if done % every == 0 or done == total_games:
                    print(f"progress={done}/{total_games}")

    out: dict[int, tuple[int, int, int, int, int, int, int]] = {}
    for key, agg in aggs.items():
        if agg.samples <= 0:
            continue

        ranked = agg.move_counts.most_common(3)
        best = ranked[0][0]
        best_count = ranked[0][1]
        alt1 = ranked[1][0] if len(ranked) > 1 else 0
        alt2 = ranked[2][0] if len(ranked) > 2 else 0

        confidence = int(round((best_count * 100.0) / agg.samples))
        score_cp = int(round(agg.score_sum / max(1, agg.samples)))
        depth = int(round(agg.depth_sum / max(1, agg.samples)))

        out[key] = (
            best,
            alt1,
            alt2,
            max(-32768, min(32767, score_cp)),
            max(0, min(255, depth)),
            max(0, min(65535, agg.samples)),
            max(0, min(100, confidence)),
        )

    return out


def write_cache(path: Path, entries: dict[int, tuple[int, int, int, int, int, int, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        f.write(b"OPC1")
        for key in sorted(entries.keys()):
            best, alt1, alt2, score_cp, depth, samples, confidence = entries[key]
            f.write(
                RECORD_STRUCT.pack(
                    key,
                    best,
                    alt1,
                    alt2,
                    score_cp,
                    depth,
                    samples,
                    confidence,
                )
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build OPC1 opening policy cache.")
    parser.add_argument("--output", type=Path, default=Path("artifacts/opening_policy_cache.bin"))
    parser.add_argument("--games", type=int, default=64)
    parser.add_argument("--seed-start", type=int, default=200)
    parser.add_argument("--seed-count", type=int, default=0)
    parser.add_argument("--turn-max", type=int, default=14)
    parser.add_argument("--analysis-budget-ms", type=int, default=900)
    parser.add_argument("--playout-budget-ms", type=int, default=80)
    parser.add_argument("--min-samples", type=int, default=6)
    parser.add_argument("--min-confidence", type=int, default=65)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--progress-every", type=int, default=25)
    args = parser.parse_args()

    workers = args.workers if args.workers > 0 else default_workers()

    entries = build_cache(
        games=max(1, args.games),
        seed_start=args.seed_start,
        seed_count=max(0, args.seed_count),
        turn_max=max(0, args.turn_max),
        analysis_budget_ms=max(10, args.analysis_budget_ms),
        playout_budget_ms=max(5, args.playout_budget_ms),
        workers=max(1, workers),
        progress_every=max(1, args.progress_every),
    )

    filtered = {
        key: value
        for key, value in entries.items()
        if value[5] >= args.min_samples and value[6] >= args.min_confidence
    }

    write_cache(args.output, filtered)

    print(f"workers={max(1, workers)}")
    print(f"entries_raw={len(entries)}")
    print(f"entries_written={len(filtered)}")
    print(f"output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
