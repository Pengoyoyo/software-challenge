#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

from rust_bridge import RustEngineProcess
from selfplay_sprt import play_game, resolve_binary_path

WEIGHT_KEYS = [
    "w_largest",
    "w_components",
    "w_spread",
    "w_count",
    "w_links",
    "w_center",
    "w_mobility",
    "w_mobility_targets",
    "w_late_largest",
    "w_late_components",
    "w_late_spread",
    "w_late_links",
    "w_late_mobility",
    "w_bridge_risk",
    "w_bridge_redundancy",
    "w_threat_in1",
    "w_threat_in2",
    "w_safe_capture",
    "w_no_move_pressure",
    "w_late_swarm_cohesion",
    "w_late_fragment_pressure",
    "w_late_disconnect_pressure",
    "w_race_connect1",
    "w_race_connect2",
    "w_race_disconnect1",
    "w_race_disconnect2",
    "w_race_side_to_move",
    "w_cut_pressure",
    "w_collapse_risk",
    "w_articulation_pressure",
    "w_round_end_tempo",
    "connect_bonus",
]

DEFAULT_WEIGHTS: dict[str, int] = {
    "w_largest": 340,
    "w_components": 230,
    "w_spread": 65,
    "w_count": 115,
    "w_links": 12,
    "w_center": 6,
    "w_mobility": 5,
    "w_mobility_targets": 4,
    "w_late_largest": 150,
    "w_late_components": 110,
    "w_late_spread": 80,
    "w_late_links": 16,
    "w_late_mobility": 10,
    "w_bridge_risk": 32,
    "w_bridge_redundancy": 20,
    "w_threat_in1": 18_000,
    "w_threat_in2": 9_500,
    "w_safe_capture": 22,
    "w_no_move_pressure": 2500,
    "w_late_swarm_cohesion": 120,
    "w_late_fragment_pressure": 48,
    "w_late_disconnect_pressure": 7000,
    "w_race_connect1": 12_000,
    "w_race_connect2": 5200,
    "w_race_disconnect1": 8600,
    "w_race_disconnect2": 3600,
    "w_race_side_to_move": 4000,
    "w_cut_pressure": 24,
    "w_collapse_risk": 180,
    "w_articulation_pressure": 28,
    "w_round_end_tempo": 2600,
    "connect_bonus": 70_000,
}

NON_NEGATIVE_KEYS = set(WEIGHT_KEYS)


def load_weights_file(path: Path) -> dict[str, int]:
    weights = dict(DEFAULT_WEIGHTS)
    for line in path.read_text(encoding="utf-8").splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#") or "=" not in trimmed:
            continue
        key, value = trimmed.split("=", 1)
        key = key.strip()
        if key not in weights:
            continue
        try:
            weights[key] = int(value.strip())
        except ValueError:
            continue
    return weights


def write_weights_file(path: Path, weights: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}={int(weights[key])}" for key in WEIGHT_KEYS]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def clamp_weight(key: str, value: float, base: int) -> int:
    upper = max(2_000, abs(base) * 6 + 2_000)
    clamped = int(round(max(-upper, min(upper, value))))
    if key in NON_NEGATIVE_KEYS:
        clamped = max(0, clamped)
    return clamped


@dataclass
class MatchResult:
    plus_mean: float
    wins: int
    losses: int
    draws: int


def run_plus_minus_match(
    *,
    binary: Path,
    plus_weights_path: Path,
    minus_weights_path: Path,
    games: int,
    move_budget_ms: int,
    max_plies: int,
    seed_start: int,
    tt_mb: int,
) -> MatchResult:
    if tt_mb > 0:
        import os

        os.environ["PIRANHAS_TT_MB"] = str(tt_mb)

    plus_engine = RustEngineProcess(
        binary=binary,
        env_overrides={"PIRANHAS_EVAL_WEIGHTS_FILE": str(plus_weights_path)},
    )
    minus_engine = RustEngineProcess(
        binary=binary,
        env_overrides={"PIRANHAS_EVAL_WEIGHTS_FILE": str(minus_weights_path)},
    )

    wins = 0
    losses = 0
    draws = 0
    try:
        total_games = max(1, games)
        pair_count = total_games // 2

        for pair_idx in range(pair_count):
            seed = seed_start + pair_idx
            for plus_as_red in (True, False):
                # plus is candidate, minus is base
                result = play_game(
                    base_engine=minus_engine,
                    cand_engine=plus_engine,
                    seed=seed,
                    candidate_as_red=plus_as_red,
                    budget_ms=move_budget_ms,
                    max_plies=max_plies,
                )
                if result.candidate_score >= 1.0:
                    wins += 1
                elif result.candidate_score <= 0.0:
                    losses += 1
                else:
                    draws += 1

        if total_games % 2 == 1:
            seed = seed_start + pair_count
            result = play_game(
                base_engine=minus_engine,
                cand_engine=plus_engine,
                seed=seed,
                candidate_as_red=(pair_count % 2) == 0,
                budget_ms=move_budget_ms,
                max_plies=max_plies,
            )
            if result.candidate_score >= 1.0:
                wins += 1
            elif result.candidate_score <= 0.0:
                losses += 1
            else:
                draws += 1
    finally:
        plus_engine.close()
        minus_engine.close()

    total = max(1, wins + losses + draws)
    plus_mean = (wins + 0.5 * draws) / total
    return MatchResult(plus_mean=plus_mean, wins=wins, losses=losses, draws=draws)


def random_delta(rng: random.Random) -> int:
    return 1 if rng.random() < 0.5 else -1


def stage_spsa(
    *,
    stage_name: str,
    theta: dict[str, int],
    base_weights: dict[str, int],
    binary: Path,
    out_dir: Path,
    iterations: int,
    games_per_eval: int,
    move_budget_ms: int,
    max_plies: int,
    seed_start: int,
    tt_mb: int,
    a: float,
    c: float,
    A: float,
    alpha: float,
    gamma: float,
    rng: random.Random,
    dry_run: bool,
) -> tuple[dict[str, int], dict[str, Any]]:
    stage_dir = out_dir / stage_name
    stage_dir.mkdir(parents=True, exist_ok=True)
    stage_log_path = stage_dir / "iterations.jsonl"

    best_theta = dict(theta)
    best_score = -1.0
    baseline_path = stage_dir / "baseline.weights"
    write_weights_file(baseline_path, base_weights)

    with stage_log_path.open("w", encoding="utf-8") as log_file:
        for k in range(max(1, iterations)):
            kk = float(k + 1)
            ak = a / ((A + kk) ** alpha)
            ck = c / (kk**gamma)

            delta = {key: random_delta(rng) for key in WEIGHT_KEYS}
            plus = {
                key: clamp_weight(key, theta[key] + ck * delta[key], base_weights[key])
                for key in WEIGHT_KEYS
            }
            minus = {
                key: clamp_weight(key, theta[key] - ck * delta[key], base_weights[key])
                for key in WEIGHT_KEYS
            }

            plus_path = stage_dir / f"iter_{k:04d}.plus.weights"
            minus_path = stage_dir / f"iter_{k:04d}.minus.weights"
            write_weights_file(plus_path, plus)
            write_weights_file(minus_path, minus)

            if dry_run:
                y = rng.uniform(-0.15, 0.15)
                match_result = MatchResult(plus_mean=0.5 + y / 2.0, wins=0, losses=0, draws=0)
            else:
                match_result = run_plus_minus_match(
                    binary=binary,
                    plus_weights_path=plus_path,
                    minus_weights_path=minus_path,
                    games=games_per_eval,
                    move_budget_ms=move_budget_ms,
                    max_plies=max_plies,
                    seed_start=seed_start + k * max(1, games_per_eval + 3),
                    tt_mb=tt_mb,
                )
                y = 2.0 * (match_result.plus_mean - 0.5)

            grad_scale = y / max(1e-9, 2.0 * ck)
            next_theta = dict(theta)
            for key in WEIGHT_KEYS:
                ghat = grad_scale * float(delta[key])
                updated = next_theta[key] + ak * ghat
                next_theta[key] = clamp_weight(key, updated, base_weights[key])
            theta = next_theta

            # Quick check against baseline; only periodic to keep runtime bounded.
            baseline_score = None
            if dry_run:
                baseline_score = 0.5 + rng.uniform(-0.08, 0.08)
            elif k % 5 == 0:
                cand_path = stage_dir / f"iter_{k:04d}.theta.weights"
                write_weights_file(cand_path, theta)
                baseline_match = run_plus_minus_match(
                    binary=binary,
                    plus_weights_path=cand_path,
                    minus_weights_path=baseline_path,
                    games=max(8, games_per_eval // 2),
                    move_budget_ms=move_budget_ms,
                    max_plies=max_plies,
                    seed_start=seed_start + 100_000 + k * 97,
                    tt_mb=tt_mb,
                )
                baseline_score = baseline_match.plus_mean

            if baseline_score is not None and baseline_score > best_score:
                best_score = baseline_score
                best_theta = dict(theta)

            record = {
                "stage": stage_name,
                "iter": k,
                "ak": ak,
                "ck": ck,
                "y": y,
                "plus_mean": match_result.plus_mean,
                "wins": match_result.wins,
                "losses": match_result.losses,
                "draws": match_result.draws,
                "baseline_score": baseline_score,
            }
            log_file.write(json.dumps(record, ensure_ascii=True) + "\n")
            log_file.flush()
            print(
                f"[{stage_name}] iter={k+1}/{iterations} plus_mean={match_result.plus_mean:.3f} "
                f"y={y:+.3f} baseline={baseline_score if baseline_score is not None else 'skip'}"
            )

    return theta, {"best_theta": best_theta, "best_score": best_score}


def main() -> int:
    parser = argparse.ArgumentParser(description="SPSA eval tuner (STC selfplay).")
    parser.add_argument("--binary", type=Path, default=Path("target/release/piranhas-rs-engine"))
    parser.add_argument("--outdir", type=Path, default=Path("artifacts/eval_tuning"))
    parser.add_argument("--seed", type=int, default=41)
    parser.add_argument("--tt-mb", type=int, default=1024)
    parser.add_argument("--move-budget-ms", type=int, default=90)
    parser.add_argument("--max-plies", type=int, default=60)
    parser.add_argument("--iterations-a", type=int, default=24)
    parser.add_argument("--iterations-b", type=int, default=12)
    parser.add_argument("--games-per-eval-a", type=int, default=24)
    parser.add_argument("--games-per-eval-b", type=int, default=36)
    parser.add_argument("--weights-file", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    binary = resolve_binary_path(args.binary)
    if not binary.exists():
        print(f"binary not found: {binary}", file=sys.stderr)
        return 2

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = (args.outdir if args.outdir.is_absolute() else ROOT / args.outdir) / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    base_weights = dict(DEFAULT_WEIGHTS)
    if args.weights_file is not None:
        weights_path = (
            args.weights_file
            if args.weights_file.is_absolute()
            else (ROOT / args.weights_file).resolve()
        )
        if not weights_path.exists():
            print(f"weights file not found: {weights_path}", file=sys.stderr)
            return 2
        base_weights = load_weights_file(weights_path)

    theta = dict(base_weights)
    baseline_path = out_dir / "baseline.weights"
    write_weights_file(baseline_path, base_weights)

    rng = random.Random(args.seed)
    theta, stage_a_info = stage_spsa(
        stage_name="stage_a",
        theta=theta,
        base_weights=base_weights,
        binary=binary,
        out_dir=out_dir,
        iterations=max(1, args.iterations_a),
        games_per_eval=max(2, args.games_per_eval_a),
        move_budget_ms=max(20, args.move_budget_ms),
        max_plies=max(20, args.max_plies),
        seed_start=args.seed,
        tt_mb=max(16, args.tt_mb),
        a=280.0,
        c=70.0,
        A=24.0,
        alpha=0.602,
        gamma=0.101,
        rng=rng,
        dry_run=args.dry_run,
    )

    theta, stage_b_info = stage_spsa(
        stage_name="stage_b",
        theta=theta,
        base_weights=base_weights,
        binary=binary,
        out_dir=out_dir,
        iterations=max(1, args.iterations_b),
        games_per_eval=max(2, args.games_per_eval_b),
        move_budget_ms=max(20, args.move_budget_ms),
        max_plies=max(20, args.max_plies),
        seed_start=args.seed + 500_000,
        tt_mb=max(16, args.tt_mb),
        a=140.0,
        c=35.0,
        A=12.0,
        alpha=0.602,
        gamma=0.101,
        rng=rng,
        dry_run=args.dry_run,
    )

    final_path = out_dir / "final.weights"
    best_path = out_dir / "best.weights"
    write_weights_file(final_path, theta)
    best_stage_a = stage_a_info.get("best_theta") or theta
    best_stage_b = stage_b_info.get("best_theta") or theta
    best_theta = best_stage_b if stage_b_info.get("best_score", -1.0) >= 0 else best_stage_a
    write_weights_file(best_path, best_theta)

    meta = {
        "binary": str(binary),
        "seed": args.seed,
        "tt_mb": args.tt_mb,
        "move_budget_ms": args.move_budget_ms,
        "max_plies": args.max_plies,
        "iterations_a": args.iterations_a,
        "iterations_b": args.iterations_b,
        "games_per_eval_a": args.games_per_eval_a,
        "games_per_eval_b": args.games_per_eval_b,
        "dry_run": bool(args.dry_run),
        "baseline_weights": str(baseline_path),
        "final_weights": str(final_path),
        "best_weights": str(best_path),
        "stage_a_best_score": stage_a_info.get("best_score"),
        "stage_b_best_score": stage_b_info.get("best_score"),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print(f"out_dir={out_dir}")
    print(f"best_weights={best_path}")
    print(f"final_weights={final_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
