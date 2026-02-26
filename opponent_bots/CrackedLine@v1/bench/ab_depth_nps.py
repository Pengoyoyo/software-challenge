#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rust_bridge import RustEngineProcess
from state_adapter import BLUE, EMPTY, ExternalState, RED, make_piece

BOARD_SIZE = 10
NUM_SQUARES = BOARD_SIZE * BOARD_SIZE


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


@dataclass
class BenchRow:
    idx: int
    turn: int
    depth: int
    nodes: int
    qnodes: int
    nps: int
    tt_hit_rate: float
    elapsed_ms: float


@dataclass
class BenchSummary:
    avg_depth: float
    avg_nodes: float
    avg_nps: float
    avg_tt_hit_rate: float
    avg_elapsed_ms: float


def apply_move(state: ExternalState, from_sq: int, to_sq: int) -> None:
    piece = state.board[from_sq]
    state.board[from_sq] = EMPTY
    state.board[to_sq] = piece
    state.player_to_move = RED if state.player_to_move == BLUE else BLUE
    state.turn += 1


def generate_snapshots(
    engine: RustEngineProcess,
    seed: int,
    snapshot_plies: Iterable[int],
    gen_budget_ms: int,
) -> list[ExternalState]:
    targets = sorted(set(int(v) for v in snapshot_plies if int(v) >= 0))
    if not targets:
        return []

    max_target = targets[-1]
    state = ExternalState(board=setup_initial_board(seed), player_to_move=RED, turn=0)
    snapshots: dict[int, ExternalState] = {}

    if 0 in targets:
        snapshots[0] = ExternalState(board=list(state.board), player_to_move=state.player_to_move, turn=state.turn)

    while state.turn < max_target:
        result = engine.search(state, gen_budget_ms * 1_000_000)
        if not result.has_move:
            break
        apply_move(state, result.from_sq, result.to_sq)
        if state.turn in targets and state.turn not in snapshots:
            snapshots[state.turn] = ExternalState(
                board=list(state.board),
                player_to_move=state.player_to_move,
                turn=state.turn,
            )

    return [snapshots[ply] for ply in targets if ply in snapshots]


def run_bench(engine: RustEngineProcess, states: list[ExternalState], budget_ms: int) -> tuple[list[BenchRow], BenchSummary]:
    rows: list[BenchRow] = []
    for idx, state in enumerate(states):
        result = engine.search(
            ExternalState(board=list(state.board), player_to_move=state.player_to_move, turn=state.turn),
            budget_ms * 1_000_000,
        )
        elapsed_s = max(1e-9, result.elapsed_ns / 1_000_000_000)
        nps = int(result.nodes / elapsed_s)
        tt_rate = (result.tt_hits / result.tt_probes) if result.tt_probes > 0 else 0.0
        rows.append(
            BenchRow(
                idx=idx,
                turn=state.turn,
                depth=result.depth,
                nodes=result.nodes,
                qnodes=result.qnodes,
                nps=nps,
                tt_hit_rate=tt_rate,
                elapsed_ms=result.elapsed_ns / 1_000_000,
            )
        )

    if not rows:
        return rows, BenchSummary(0.0, 0.0, 0.0, 0.0, 0.0)

    n = len(rows)
    summary = BenchSummary(
        avg_depth=sum(r.depth for r in rows) / n,
        avg_nodes=sum(r.nodes for r in rows) / n,
        avg_nps=sum(r.nps for r in rows) / n,
        avg_tt_hit_rate=sum(r.tt_hit_rate for r in rows) / n,
        avg_elapsed_ms=sum(r.elapsed_ms for r in rows) / n,
    )
    return rows, summary


def print_table(title: str, rows: list[BenchRow], summary: BenchSummary) -> None:
    print(f"\n=== {title} ===")
    print("idx turn depth nodes qnodes nps tt_hit_rate elapsed_ms")
    for r in rows:
        print(
            f"{r.idx:>3} {r.turn:>4} {r.depth:>5} {r.nodes:>7} {r.qnodes:>6} "
            f"{r.nps:>8} {r.tt_hit_rate:>10.2%} {r.elapsed_ms:>9.2f}"
        )
    print(
        "avg",
        f"depth={summary.avg_depth:.2f}",
        f"nodes={summary.avg_nodes:.0f}",
        f"nps={summary.avg_nps:.0f}",
        f"tt_hit={summary.avg_tt_hit_rate:.2%}",
        f"elapsed_ms={summary.avg_elapsed_ms:.2f}",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="A/B benchmark focused on depth + NPS.")
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--snapshot-plies", type=str, default="0,8,16,24")
    parser.add_argument("--gen-budget-ms", type=int, default=30)
    parser.add_argument("--bench-budget-ms", type=int, default=200)
    parser.add_argument("--base-binary", type=Path, default=Path("target/release/piranhas-rs-engine"))
    parser.add_argument("--cand-binary", type=Path, default=None)
    args = parser.parse_args()

    snapshot_plies = [int(part.strip()) for part in args.snapshot_plies.split(",") if part.strip()]

    base_engine = RustEngineProcess(binary=args.base_binary)
    snapshots = generate_snapshots(base_engine, args.seed, snapshot_plies, args.gen_budget_ms)
    if not snapshots:
        print("No snapshots generated.")
        return 1

    base_rows, base_summary = run_bench(base_engine, snapshots, args.bench_budget_ms)
    print_table("BASE", base_rows, base_summary)
    base_engine.close()

    if args.cand_binary is not None:
        cand_engine = RustEngineProcess(binary=args.cand_binary)
        cand_rows, cand_summary = run_bench(cand_engine, snapshots, args.bench_budget_ms)
        print_table("CAND", cand_rows, cand_summary)
        cand_engine.close()

        depth_delta = cand_summary.avg_depth - base_summary.avg_depth
        nps_delta = cand_summary.avg_nps - base_summary.avg_nps
        nps_delta_pct = (nps_delta / base_summary.avg_nps * 100.0) if base_summary.avg_nps > 0 else 0.0
        print("\n=== DELTA (cand - base) ===")
        print(f"avg_depth_delta={depth_delta:+.2f}")
        print(f"avg_nps_delta={nps_delta:+.0f} ({nps_delta_pct:+.2f}%)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
