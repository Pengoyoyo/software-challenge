#!/usr/bin/env python3
"""
Lightweight micro benchmark for cython_v2 search quality/speed metrics.

Focus metrics per position:
- reached depth
- nodes
- tt hits
- nps
- elapsed seconds
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import random
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean

from socha import Board, FieldType, GameState

from cython_core.search import clear_tt, init_search, iterative_deepening


# One valid start layout captured from server logs.
START_ROWS = [
    ["Leer", "blue1", "blue3", "blue1", "blue2", "blue2", "blue1", "blue1", "blue3", "Leer"],
    ["red3", "Leer", "Leer", "Leer", "Leer", "Leer", "Leer", "Leer", "Leer", "red1"],
    ["red3", "Leer", "Leer", "Leer", "Krake", "Leer", "Leer", "Leer", "Leer", "red3"],
    ["red1", "Leer", "Leer", "Leer", "Leer", "Leer", "Leer", "Leer", "Leer", "red3"],
    ["red2", "Leer", "Leer", "Leer", "Leer", "Leer", "Leer", "Leer", "Leer", "red1"],
    ["red2", "Leer", "Leer", "Leer", "Leer", "Leer", "Leer", "Leer", "Leer", "red2"],
    ["red1", "Leer", "Leer", "Krake", "Leer", "Leer", "Leer", "Leer", "Leer", "red1"],
    ["red1", "Leer", "Leer", "Leer", "Leer", "Leer", "Leer", "Leer", "Leer", "red1"],
    ["red3", "Leer", "Leer", "Leer", "Leer", "Leer", "Leer", "Leer", "Leer", "red2"],
    ["Leer", "blue1", "blue3", "blue3", "blue1", "blue2", "blue1", "blue1", "blue2", "Leer"],
]

FIELD_MAP = {
    "Leer": FieldType.Empty,
    "Krake": FieldType.Squid,
    "red1": FieldType.OneS,
    "red2": FieldType.OneM,
    "red3": FieldType.OneL,
    "blue1": FieldType.TwoS,
    "blue2": FieldType.TwoM,
    "blue3": FieldType.TwoL,
}

LINE_RE = re.compile(
    r"^d(?P<depth>\d+):\s+(?P<score>-?\d+(?:\.\d+)?)\s+\|\s+"
    r"(?P<nodes>\d+)n\s+(?P<hits>\d+)h\s+(?P<nps>\d+)nps\s+(?P<secs>\d+(?:\.\d+)?)s$"
)


@dataclass
class BenchResult:
    label: str
    seed: int
    plies: int
    repeat: int
    team: int
    depth: int
    score: float
    nodes: int
    tt_hits: int
    nps: int
    elapsed_s: float


def build_start_state() -> GameState:
    board_map = [[FIELD_MAP[cell] for cell in row] for row in START_ROWS]
    return GameState(Board(board_map), 0)


def make_position(seed: int, plies: int) -> GameState:
    rnd = random.Random(seed)
    state = build_start_state()
    for _ in range(plies):
        moves = list(state.possible_moves())
        if not moves:
            break
        state.perform_move_mut(rnd.choice(moves))
    return state


def parse_last_iterative_line(output: str) -> dict[str, float | int]:
    last_match: re.Match[str] | None = None
    for line in output.splitlines():
        match = LINE_RE.match(line.strip())
        if match:
            last_match = match
    if last_match is None:
        raise RuntimeError("No iterative deepening stats line found in output.")
    groups = last_match.groupdict()
    return {
        "depth": int(groups["depth"]),
        "score": float(groups["score"]),
        "nodes": int(groups["nodes"]),
        "tt_hits": int(groups["hits"]),
        "nps": int(groups["nps"]),
        "elapsed_s": float(groups["secs"]),
    }


def run_one(label: str, seed: int, plies: int, repeat: int, time_limit: float, preserve_tt: bool) -> BenchResult:
    if not preserve_tt:
        clear_tt()

    state = make_position(seed=seed, plies=plies)
    our_team = 1 if (state.turn % 2 == 0) else 2

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        _ = iterative_deepening(state, our_team, time_limit)

    stats = parse_last_iterative_line(buffer.getvalue())
    return BenchResult(
        label=label,
        seed=seed,
        plies=plies,
        repeat=repeat,
        team=our_team,
        depth=int(stats["depth"]),
        score=float(stats["score"]),
        nodes=int(stats["nodes"]),
        tt_hits=int(stats["tt_hits"]),
        nps=int(stats["nps"]),
        elapsed_s=float(stats["elapsed_s"]),
    )


def parse_plies_arg(raw: str) -> list[int]:
    out = []
    for chunk in raw.split(","):
        text = chunk.strip()
        if not text:
            continue
        out.append(int(text))
    if not out:
        raise ValueError("Need at least one plies value.")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="cython_v2 micro benchmark")
    parser.add_argument("--plies", default="0,6,12,20,30", help="Comma-separated plies for generated positions")
    parser.add_argument("--seed-base", type=int, default=11, help="Seed base for deterministic positions")
    parser.add_argument("--repeats", type=int, default=1, help="Repeats per position")
    parser.add_argument("--time-limit", type=float, default=0.25, help="Search time per run in seconds")
    parser.add_argument("--preserve-tt", action="store_true", help="Do not clear TT between runs")
    parser.add_argument("--json-out", type=Path, default=Path("bench") / "micro_benchmark_latest.json")
    args = parser.parse_args()

    plies_list = parse_plies_arg(args.plies)
    args.json_out.parent.mkdir(parents=True, exist_ok=True)

    init_search()

    results: list[BenchResult] = []
    for idx, plies in enumerate(plies_list):
        for rep in range(args.repeats):
            seed = args.seed_base + idx * 101 + rep * 997
            label = f"p{plies}_r{rep+1}"
            res = run_one(
                label=label,
                seed=seed,
                plies=plies,
                repeat=rep + 1,
                time_limit=args.time_limit,
                preserve_tt=args.preserve_tt,
            )
            results.append(res)
            print(
                f"{res.label:>8}  team={res.team}  depth={res.depth:>2}  nodes={res.nodes:>8}  "
                f"hits={res.tt_hits:>6}  nps={res.nps:>8}  t={res.elapsed_s:>4.2f}s"
            )

    depth_avg = mean(r.depth for r in results)
    nodes_avg = mean(r.nodes for r in results)
    nps_avg = mean(r.nps for r in results)
    tt_hits_avg = mean(r.tt_hits for r in results)

    summary = {
        "runs": len(results),
        "depth_avg": depth_avg,
        "depth_max": max(r.depth for r in results),
        "nodes_avg": nodes_avg,
        "nps_avg": nps_avg,
        "tt_hits_avg": tt_hits_avg,
        "time_limit": args.time_limit,
        "preserve_tt": args.preserve_tt,
        "plies": plies_list,
    }

    payload = {
        "summary": summary,
        "results": [asdict(r) for r in results],
    }
    args.json_out.write_text(json.dumps(payload, indent=2))

    print("\nSummary")
    print(
        f"runs={summary['runs']} depth_avg={summary['depth_avg']:.2f} depth_max={summary['depth_max']} "
        f"nodes_avg={summary['nodes_avg']:.0f} nps_avg={summary['nps_avg']:.0f} tt_hits_avg={summary['tt_hits_avg']:.0f}"
    )
    print(f"json={args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
