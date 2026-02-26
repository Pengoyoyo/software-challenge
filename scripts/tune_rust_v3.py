#!/usr/bin/env python3
"""
Tune runtime-configurable eval weights for:
- bots/rust/pur_rust_client.py
- bots/cython_v3/client_cython.py

This script expects the engines to support env-based weight overrides:
- rust: PIRANHAS_RS_EVAL_WEIGHTS (fallback: RUST_EVAL_WEIGHTS)
- cython_v3 rust_core: CYTHON_V3_EVAL_WEIGHTS (fallbacks above)
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import random
import re
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent.resolve()
SERVER_JAR = ROOT / "server" / "server.jar"

TARGET_CONFIG = {
    "rust": {
        "path": ROOT / "bots" / "rust" / "pur_rust_client.py",
        "env": "PIRANHAS_RS_EVAL_WEIGHTS",
    },
    "v3": {
        "path": ROOT / "bots" / "cython_v3" / "client_cython.py",
        "env": "CYTHON_V3_EVAL_WEIGHTS",
    },
}

WEIGHT_NAMES = (
    "w_largest",
    "w_components",
    "w_spread",
    "w_material",
    "w_links",
    "w_center",
    "w_mobility",
    "w_late_largest",
    "w_late_components",
    "w_late_spread",
    "w_late_links",
    "w_late_mobility",
    "connect_bonus",
)

BASE_WEIGHTS = (
    380.0,
    260.0,
    50.0,
    130.0,
    15.0,
    4.0,
    7.0,
    180.0,
    130.0,
    90.0,
    20.0,
    12.0,
    85_000.0,
)

WEIGHT_BOUNDS = (
    (100.0, 900.0),
    (50.0, 700.0),
    (0.0, 300.0),
    (20.0, 450.0),
    (0.0, 100.0),
    (0.0, 30.0),
    (0.0, 50.0),
    (50.0, 600.0),
    (50.0, 500.0),
    (0.0, 300.0),
    (0.0, 120.0),
    (0.0, 80.0),
    (10_000.0, 200_000.0),
)

DEFAULT_EXCLUDES = [r"/bots/cpp/"]
_BENCHMARK_MOD: Any | None = None


@dataclass
class BotEntry:
    path: str
    name: str
    python_exec: str


@dataclass
class OpponentStats:
    wins: int = 0
    losses: int = 0
    draws: int = 0
    errors: int = 0
    games: int = 0


@dataclass
class EvalResult:
    fitness: float
    worst_fitness: float
    wins: int
    losses: int
    draws: int
    errors: int
    games: int
    per_opponent: dict[str, OpponentStats]


@dataclass
class Candidate:
    weights: tuple[float, ...]
    eval_result: EvalResult | None = None


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def get_benchmark_module() -> Any:
    global _BENCHMARK_MOD
    if _BENCHMARK_MOD is None:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import benchmark  # type: ignore
        _BENCHMARK_MOD = benchmark
    return _BENCHMARK_MOD


def normalize_path(path: Path | str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = ROOT / p
    return p.resolve()


def safe_rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except Exception:
        return str(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tune eval weights for rust/v3 bots.")
    parser.add_argument("--target", choices=["rust", "v3", "both"], default="both")
    parser.add_argument(
        "--initial-weights",
        default=",".join(f"{x:.10g}" for x in BASE_WEIGHTS),
        help="Comma-separated 13 floats.",
    )
    parser.add_argument(
        "--opponent",
        action="append",
        default=None,
        help="If set, use only these opponents (can repeat).",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=None,
        help="Regex exclude for opponent path/name (repeatable).",
    )
    parser.add_argument("--include-starter", action="store_true")
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--preflight-games", type=int, default=2)
    parser.add_argument("--games-per-opponent", type=int, default=2)
    parser.add_argument("--timeout-s", type=int, default=180)
    parser.add_argument("--base-port", type=int, default=18000)
    parser.add_argument("--error-penalty", type=float, default=1.0)

    parser.add_argument("--population-size", type=int, default=16)
    parser.add_argument("--generations", type=int, default=40)
    parser.add_argument("--elite-count", type=int, default=4)
    parser.add_argument("--immigrants", type=int, default=2)
    parser.add_argument("--mutation-sigma", type=float, default=0.08)
    parser.add_argument("--mutation-decay", type=float, default=0.985)
    parser.add_argument("--mutation-floor", type=float, default=0.015)
    parser.add_argument("--seed", type=int, default=42)

    parser.add_argument("--resample-top", type=int, default=0)
    parser.add_argument("--resample-rounds", type=int, default=0)
    parser.add_argument("--final-validation-games", type=int, default=12)

    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--log-dir", type=Path, default=None)
    parser.add_argument("--keep-game-logs", action="store_true")
    parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.exclude is None:
        args.exclude = list(DEFAULT_EXCLUDES)
    else:
        args.exclude = list(DEFAULT_EXCLUDES) + list(args.exclude)

    if args.population_size <= 0:
        raise SystemExit("--population-size must be > 0")
    if args.elite_count <= 0 or args.elite_count > args.population_size:
        raise SystemExit("--elite-count must be in [1, population-size]")
    if args.immigrants < 0 or args.immigrants >= args.population_size:
        raise SystemExit("--immigrants must be >=0 and < population-size")
    if args.games_per_opponent <= 0:
        raise SystemExit("--games-per-opponent must be > 0")
    if args.preflight_games <= 0:
        raise SystemExit("--preflight-games must be > 0")
    if args.timeout_s <= 0:
        raise SystemExit("--timeout-s must be > 0")
    if args.generations <= 0:
        raise SystemExit("--generations must be > 0")

    return args


def parse_weights(raw: str) -> tuple[float, ...]:
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != len(WEIGHT_NAMES):
        raise ValueError(f"exactly {len(WEIGHT_NAMES)} weights required")
    vals = tuple(float(p) for p in parts)
    if any((not (v == v) or v in (float("inf"), float("-inf"))) for v in vals):
        raise ValueError("weights must be finite")
    return vals


def format_weights(weights: tuple[float, ...]) -> str:
    return ",".join(f"{w:.10g}" for w in weights)


def clamp_weight(idx: int, value: float) -> float:
    lo, hi = WEIGHT_BOUNDS[idx]
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def mutate(weights: tuple[float, ...], sigma: float, rng: random.Random) -> tuple[float, ...]:
    out: list[float] = []
    for idx, w in enumerate(weights):
        lo, hi = WEIGHT_BOUNDS[idx]
        span = hi - lo
        if rng.random() < 0.75:
            w = w + rng.gauss(0.0, sigma * span)
        out.append(clamp_weight(idx, w))
    return tuple(out)


def crossover(a: tuple[float, ...], b: tuple[float, ...], rng: random.Random) -> tuple[float, ...]:
    out: list[float] = []
    for idx, (av, bv) in enumerate(zip(a, b)):
        t = rng.random()
        out.append(clamp_weight(idx, av * t + bv * (1.0 - t)))
    return tuple(out)


def eval_sort_key(ev: EvalResult | None) -> tuple[float, float, float, float]:
    if ev is None:
        return (-999.0, -999.0, -999.0, -999.0)
    return (ev.fitness, ev.worst_fitness, -float(ev.errors), float(ev.wins - ev.losses))


def tournament_select(pop: list[Candidate], rng: random.Random, k: int = 3) -> Candidate:
    sample = rng.sample(pop, min(k, len(pop)))
    sample.sort(key=lambda c: eval_sort_key(c.eval_result), reverse=True)
    return sample[0]


def rank_population(pop: list[Candidate]) -> None:
    pop.sort(key=lambda c: eval_sort_key(c.eval_result), reverse=True)


def build_next_population(
    population: list[Candidate],
    pop_size: int,
    elite_count: int,
    immigrants: int,
    sigma: float,
    rng: random.Random,
) -> list[Candidate]:
    rank_population(population)
    out: list[Candidate] = []

    for elite in population[:elite_count]:
        out.append(Candidate(weights=elite.weights))

    immigrants = min(max(0, immigrants), max(0, pop_size - len(out)))
    for _ in range(immigrants):
        values = [rng.uniform(lo, hi) for lo, hi in WEIGHT_BOUNDS]
        out.append(Candidate(weights=tuple(values)))

    while len(out) < pop_size:
        p1 = tournament_select(population, rng)
        p2 = tournament_select(population, rng)
        child = crossover(p1.weights, p2.weights, rng)
        child = mutate(child, sigma, rng)
        out.append(Candidate(weights=child))

    return out


def load_discovered_bots(include_starter: bool) -> list[BotEntry]:
    benchmark = get_benchmark_module()
    discovered = benchmark.discover_bots()
    benchmark.load_saved_custom_candidates(discovered)

    out: list[BotEntry] = []
    seen: set[str] = set()
    for item in discovered:
        path = normalize_path(item.path)
        if not include_starter and path.name == "starter.py":
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(BotEntry(path=key, name=item.name, python_exec=item.python_exec))

    out.sort(key=lambda b: b.path)
    return out


def add_explicit_bot(path: Path, bots: dict[str, BotEntry]) -> None:
    p = normalize_path(path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"bot path not found: {p}")

    benchmark = get_benchmark_module()
    key = str(p)
    if key in bots:
        return
    bots[key] = BotEntry(path=key, name=p.stem, python_exec=benchmark.get_python(p))


def find_free_port(start: int) -> int:
    for port in range(start, start + 5000):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("No free port found")


def kill_process_group(proc: subprocess.Popen[Any] | None) -> None:
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        return
    try:
        proc.wait(timeout=2.5)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass


def parse_winner(log_content: str) -> str:
    benchmark = get_benchmark_module()
    result, _reason = benchmark.parse_game_result(log_content)
    if result == benchmark.RESULT_WIN_ONE:
        return "ONE"
    if result == benchmark.RESULT_WIN_TWO:
        return "TWO"
    if result == benchmark.RESULT_DRAW:
        return "DRAW"
    return "UNKNOWN"


def run_game(
    bot_one: BotEntry,
    bot_two: BotEntry,
    game_id: int,
    timeout_s: int,
    base_port: int,
    run_dir: Path,
    keep_logs: bool,
    env_one: dict[str, str] | None,
    env_two: dict[str, str] | None,
) -> dict[str, Any]:
    logs_dir = run_dir / "games"
    logs_dir.mkdir(parents=True, exist_ok=True)

    server_log = logs_dir / f"game_{game_id:06d}_server.log"
    bot1_log = logs_dir / f"game_{game_id:06d}_bot_one.log"
    bot2_log = logs_dir / f"game_{game_id:06d}_bot_two.log"

    port_seed = base_port + ((game_id * 19) % 12000)
    port = find_free_port(port_seed)

    result: dict[str, Any] = {
        "winner": "UNKNOWN",
        "bot1_crash": False,
        "bot2_crash": False,
        "timeout": False,
    }

    env1 = os.environ.copy()
    env2 = os.environ.copy()
    env1["PYTHONUNBUFFERED"] = "1"
    env2["PYTHONUNBUFFERED"] = "1"
    env1["PYTHONPATH"] = f"{ROOT}:{env1.get('PYTHONPATH', '')}".rstrip(":")
    env2["PYTHONPATH"] = f"{ROOT}:{env2.get('PYTHONPATH', '')}".rstrip(":")
    if env_one:
        env1.update(env_one)
    if env_two:
        env2.update(env_two)

    cmd_server = ["java", "-jar", str(SERVER_JAR), "--port", str(port)]
    cmd_one = [bot_one.python_exec, "-u", bot_one.path, "--port", str(port)]
    cmd_two = [bot_two.python_exec, "-u", bot_two.path, "--port", str(port)]

    server_proc: subprocess.Popen[Any] | None = None
    bot1_proc: subprocess.Popen[Any] | None = None
    bot2_proc: subprocess.Popen[Any] | None = None

    try:
        with server_log.open("w", encoding="utf-8") as srv:
            server_proc = subprocess.Popen(
                cmd_server,
                cwd=str(ROOT),
                stdout=srv,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid,
            )
        time.sleep(2.2)

        with bot1_log.open("w", encoding="utf-8") as b1:
            bot1_proc = subprocess.Popen(
                cmd_one,
                cwd=str(Path(bot_one.path).parent),
                stdout=b1,
                stderr=subprocess.STDOUT,
                env=env1,
                preexec_fn=os.setsid,
            )
        time.sleep(0.4)

        with bot2_log.open("w", encoding="utf-8") as b2:
            bot2_proc = subprocess.Popen(
                cmd_two,
                cwd=str(Path(bot_two.path).parent),
                stdout=b2,
                stderr=subprocess.STDOUT,
                env=env2,
                preexec_fn=os.setsid,
            )

        started = time.monotonic()
        while time.monotonic() - started < timeout_s:
            if bot1_proc.poll() is not None and bot2_proc.poll() is not None:
                break
            time.sleep(0.2)
        else:
            result["timeout"] = True

        time.sleep(0.8)

        if bot1_proc.poll() is not None and bot1_proc.returncode != 0:
            result["bot1_crash"] = True
        if bot2_proc.poll() is not None and bot2_proc.returncode != 0:
            result["bot2_crash"] = True

        if server_log.exists():
            result["winner"] = parse_winner(server_log.read_text(encoding="utf-8", errors="ignore"))
        else:
            result["winner"] = "UNKNOWN"

    except Exception:
        result["winner"] = "ERROR"
    finally:
        kill_process_group(bot1_proc)
        kill_process_group(bot2_proc)
        kill_process_group(server_proc)

        if not keep_logs:
            if result["winner"] in {"ONE", "TWO", "DRAW"} and not result["bot1_crash"] and not result["bot2_crash"]:
                server_log.unlink(missing_ok=True)
                bot1_log.unlink(missing_ok=True)
                bot2_log.unlink(missing_ok=True)

    return result


def score_from_stats(stats: OpponentStats, error_penalty: float) -> float:
    if stats.games <= 0:
        return -1.0
    points = stats.wins + 0.5 * stats.draws - error_penalty * stats.errors
    return points / stats.games


def merge_eval_results(a: EvalResult, b: EvalResult, error_penalty: float) -> EvalResult:
    merged: dict[str, OpponentStats] = {}
    for key, stats in a.per_opponent.items():
        merged[key] = OpponentStats(
            wins=stats.wins,
            losses=stats.losses,
            draws=stats.draws,
            errors=stats.errors,
            games=stats.games,
        )
    for key, stats in b.per_opponent.items():
        cur = merged.get(key, OpponentStats())
        cur.wins += stats.wins
        cur.losses += stats.losses
        cur.draws += stats.draws
        cur.errors += stats.errors
        cur.games += stats.games
        merged[key] = cur

    scores = [score_from_stats(s, error_penalty) for s in merged.values()]
    return EvalResult(
        fitness=sum(scores) / max(1, len(scores)),
        worst_fitness=min(scores) if scores else -1.0,
        wins=sum(s.wins for s in merged.values()),
        losses=sum(s.losses for s in merged.values()),
        draws=sum(s.draws for s in merged.values()),
        errors=sum(s.errors for s in merged.values()),
        games=sum(s.games for s in merged.values()),
        per_opponent=merged,
    )


def evaluate_weights(
    weights: tuple[float, ...],
    target: BotEntry,
    target_env_name: str,
    opponents: list[BotEntry],
    games_per_opponent: int,
    timeout_s: int,
    base_port: int,
    run_dir: Path,
    keep_game_logs: bool,
    error_penalty: float,
    next_game_id: int,
) -> tuple[EvalResult, int]:
    env_target = {target_env_name: format_weights(weights)}
    gid = next_game_id
    per_opp: dict[str, OpponentStats] = {}

    for opp in opponents:
        stats = OpponentStats()
        for i in range(games_per_opponent):
            if i % 2 == 0:
                bot_one, bot_two = target, opp
                env_one, env_two = env_target, None
                target_side = "ONE"
            else:
                bot_one, bot_two = opp, target
                env_one, env_two = None, env_target
                target_side = "TWO"

            game = run_game(
                bot_one=bot_one,
                bot_two=bot_two,
                game_id=gid,
                timeout_s=timeout_s,
                base_port=base_port,
                run_dir=run_dir,
                keep_logs=keep_game_logs,
                env_one=env_one,
                env_two=env_two,
            )
            gid += 1

            winner = game.get("winner")
            if winner == target_side:
                stats.wins += 1
            elif winner in ("ONE", "TWO"):
                stats.losses += 1
            elif winner == "DRAW":
                stats.draws += 1
            else:
                stats.errors += 1
            stats.games += 1

        per_opp[opp.path] = stats

    scores = [score_from_stats(s, error_penalty) for s in per_opp.values()]
    result = EvalResult(
        fitness=sum(scores) / max(1, len(scores)),
        worst_fitness=min(scores) if scores else -1.0,
        wins=sum(s.wins for s in per_opp.values()),
        losses=sum(s.losses for s in per_opp.values()),
        draws=sum(s.draws for s in per_opp.values()),
        errors=sum(s.errors for s in per_opp.values()),
        games=sum(s.games for s in per_opp.values()),
        per_opponent=per_opp,
    )
    return result, gid


def candidate_to_json(candidate: Candidate) -> dict[str, Any]:
    payload: dict[str, Any] = {"weights": list(candidate.weights)}
    ev = candidate.eval_result
    if ev is None:
        payload["eval_result"] = None
        return payload

    payload["eval_result"] = {
        "fitness": ev.fitness,
        "worst_fitness": ev.worst_fitness,
        "wins": ev.wins,
        "losses": ev.losses,
        "draws": ev.draws,
        "errors": ev.errors,
        "games": ev.games,
        "per_opponent": {k: asdict(v) for k, v in ev.per_opponent.items()},
    }
    return payload


def candidate_from_json(payload: dict[str, Any]) -> Candidate:
    weights_raw = payload.get("weights")
    if not isinstance(weights_raw, list) or len(weights_raw) != len(WEIGHT_NAMES):
        raise ValueError("invalid candidate weights in checkpoint")
    weights = tuple(float(x) for x in weights_raw)

    ev_raw = payload.get("eval_result")
    if not isinstance(ev_raw, dict):
        return Candidate(weights=weights)

    per_raw = ev_raw.get("per_opponent")
    if not isinstance(per_raw, dict):
        per_raw = {}

    per: dict[str, OpponentStats] = {}
    for key, value in per_raw.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        per[key] = OpponentStats(
            wins=int(value.get("wins", 0)),
            losses=int(value.get("losses", 0)),
            draws=int(value.get("draws", 0)),
            errors=int(value.get("errors", 0)),
            games=int(value.get("games", 0)),
        )

    ev = EvalResult(
        fitness=float(ev_raw.get("fitness", -999.0)),
        worst_fitness=float(ev_raw.get("worst_fitness", -999.0)),
        wins=int(ev_raw.get("wins", 0)),
        losses=int(ev_raw.get("losses", 0)),
        draws=int(ev_raw.get("draws", 0)),
        errors=int(ev_raw.get("errors", 0)),
        games=int(ev_raw.get("games", 0)),
        per_opponent=per,
    )
    return Candidate(weights=weights, eval_result=ev)


def write_progress(path: Path, state: str, generation: int, sigma: float, best: Candidate | None, next_gid: int) -> None:
    payload: dict[str, Any] = {
        "time": now_iso(),
        "state": state,
        "generation": generation,
        "sigma": sigma,
        "next_game_id": next_gid,
    }
    if best and best.eval_result:
        payload["best"] = {
            "weights": list(best.weights),
            "fitness": best.eval_result.fitness,
            "worst_fitness": best.eval_result.worst_fitness,
            "wins": best.eval_result.wins,
            "losses": best.eval_result.losses,
            "draws": best.eval_result.draws,
            "errors": best.eval_result.errors,
            "games": best.eval_result.games,
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def save_checkpoint(
    path: Path,
    generation: int,
    sigma: float,
    population: list[Candidate],
    history: list[dict[str, Any]],
    global_best: Candidate | None,
    rng_state: object,
    next_gid: int,
    target: BotEntry,
    target_env: str,
    opponents: list[BotEntry],
    skipped: list[dict[str, Any]],
    args: argparse.Namespace,
) -> None:
    payload = {
        "version": 1,
        "updated_at": now_iso(),
        "generation": generation,
        "sigma": sigma,
        "population": [candidate_to_json(c) for c in population],
        "history": history,
        "global_best": candidate_to_json(global_best) if global_best else None,
        "rng_state": repr(rng_state),
        "next_game_id": next_gid,
        "target": asdict(target),
        "target_env": target_env,
        "opponents": [asdict(o) for o in opponents],
        "skipped_opponents": skipped,
        "args": {
            "games_per_opponent": args.games_per_opponent,
            "timeout_s": args.timeout_s,
            "base_port": args.base_port,
            "population_size": args.population_size,
            "elite_count": args.elite_count,
            "mutation_sigma": args.mutation_sigma,
            "mutation_decay": args.mutation_decay,
            "mutation_floor": args.mutation_floor,
            "seed": args.seed,
            "error_penalty": args.error_penalty,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def write_analysis(
    analysis_path: Path,
    start_ts: float,
    end_ts: float,
    target_key: str,
    target: BotEntry,
    target_env: str,
    discovered_count: int,
    opponents: list[BotEntry],
    skipped: list[dict[str, Any]],
    history: list[dict[str, Any]],
    global_best: Candidate | None,
    final_validation: EvalResult | None,
    args: argparse.Namespace,
) -> None:
    lines: list[str] = []
    lines.append("# Rust/V3 Tuning Analysis")
    lines.append("")
    lines.append(f"- Generated: {now_iso()}")
    lines.append(f"- Target key: {target_key}")
    lines.append(f"- Runtime: {int(end_ts - start_ts)}s")
    lines.append(f"- Start: {datetime.fromtimestamp(start_ts).isoformat(timespec='seconds')}")
    lines.append(f"- End: {datetime.fromtimestamp(end_ts).isoformat(timespec='seconds')}")
    lines.append("")
    lines.append("## Configuration")
    lines.append("")
    lines.append(f"- Target: {target.path}")
    lines.append(f"- Target env: {target_env}")
    lines.append(f"- Discovered bots: {discovered_count}")
    lines.append(f"- Active opponents: {len(opponents)}")
    lines.append(f"- Games per opponent: {args.games_per_opponent}")
    lines.append(f"- Timeout per game: {args.timeout_s}s")
    lines.append(f"- Population/Elites: {args.population_size}/{args.elite_count}")
    lines.append(f"- Mutation sigma/decay/floor: {args.mutation_sigma}/{args.mutation_decay}/{args.mutation_floor}")
    lines.append(f"- Seed: {args.seed}")
    lines.append("")
    lines.append("## Weight Names")
    lines.append("")
    lines.append(f"- {', '.join(WEIGHT_NAMES)}")

    lines.append("")
    lines.append("## Opponents")
    lines.append("")
    if opponents:
        for opp in opponents:
            lines.append(f"- {opp.path}")
    else:
        lines.append("- none")

    lines.append("")
    lines.append("## Skipped In Preflight")
    lines.append("")
    if skipped:
        for item in skipped:
            lines.append(
                f"- {item.get('path')} | reason={item.get('reason')} "
                f"W/L/D/E={item.get('wins')}/{item.get('losses')}/{item.get('draws')}/{item.get('errors')}"
            )
    else:
        lines.append("- none")

    lines.append("")
    lines.append("## Best Result")
    lines.append("")
    if global_best and global_best.eval_result:
        ev = global_best.eval_result
        lines.append(f"- Best fitness: {ev.fitness}")
        lines.append(f"- Best worst-opponent fitness: {ev.worst_fitness}")
        lines.append(f"- Best weights: {list(global_best.weights)}")
        lines.append(f"- Aggregate W/L/D/E: {ev.wins}/{ev.losses}/{ev.draws}/{ev.errors}")
        lines.append(f"- Aggregate games: {ev.games}")
    else:
        lines.append("- no best result")

    lines.append("")
    lines.append("## History")
    lines.append("")
    lines.append("| generation | sigma | best_fitness | best_worst_fitness | mean_fitness | best_weights |")
    lines.append("|---:|---:|---:|---:|---:|---|")
    if history:
        for h in history:
            lines.append(
                f"| {h.get('generation')} | {h.get('sigma')} | {h.get('best_fitness')} | "
                f"{h.get('best_worst_fitness')} | {h.get('mean_fitness')} | {h.get('best_weights')} |"
            )
    else:
        lines.append("| n/a | n/a | n/a | n/a | n/a | n/a |")

    lines.append("")
    lines.append("## Final Validation")
    lines.append("")
    if final_validation is None:
        lines.append("- Validation disabled.")
    else:
        lines.append(f"- Fitness: {final_validation.fitness}")
        lines.append(f"- Worst-opponent fitness: {final_validation.worst_fitness}")
        lines.append(
            f"- W/L/D/E: {final_validation.wins}/{final_validation.losses}/{final_validation.draws}/{final_validation.errors}"
        )
        lines.append(f"- Games: {final_validation.games}")
        lines.append("")
        lines.append("| opponent | games | wins | losses | draws | errors | fitness |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for path, stats in sorted(final_validation.per_opponent.items()):
            fit = score_from_stats(stats, args.error_penalty)
            lines.append(
                f"| {path} | {stats.games} | {stats.wins} | {stats.losses} | {stats.draws} | {stats.errors} | {fit} |"
            )

    analysis_path.parent.mkdir(parents=True, exist_ok=True)
    analysis_path.write_text("\n".join(lines) + "\n")


def preflight(
    target: BotEntry,
    target_env: str,
    opponents: list[BotEntry],
    initial_weights: tuple[float, ...],
    args: argparse.Namespace,
    run_dir: Path,
    game_id: int,
) -> tuple[list[BotEntry], list[dict[str, Any]], int]:
    if args.skip_preflight:
        return opponents, [], game_id

    usable: list[BotEntry] = []
    skipped: list[dict[str, Any]] = []
    gid = game_id

    print(f"[preflight] opponents to test: {len(opponents)}", flush=True)
    for idx, opp in enumerate(opponents, start=1):
        res, gid = evaluate_weights(
            weights=initial_weights,
            target=target,
            target_env_name=target_env,
            opponents=[opp],
            games_per_opponent=args.preflight_games,
            timeout_s=args.timeout_s,
            base_port=args.base_port,
            run_dir=run_dir,
            keep_game_logs=args.keep_game_logs,
            error_penalty=args.error_penalty,
            next_game_id=gid,
        )
        valid = res.wins + res.losses + res.draws
        ok = valid > 0
        print(
            f"[preflight] {idx:>2}/{len(opponents)} {'OK' if ok else 'SKIP'} "
            f"{safe_rel(Path(opp.path))} W/L/D/E={res.wins}/{res.losses}/{res.draws}/{res.errors}",
            flush=True,
        )
        if ok:
            usable.append(opp)
        else:
            skipped.append(
                {
                    "path": opp.path,
                    "name": opp.name,
                    "reason": "no_decisive_or_draw_result",
                    "wins": res.wins,
                    "losses": res.losses,
                    "draws": res.draws,
                    "errors": res.errors,
                }
            )

    return usable, skipped, gid


def run_single_target(
    target_key: str,
    args: argparse.Namespace,
    discovered: list[BotEntry],
    initial_weights: tuple[float, ...],
    base_log_dir: Path,
) -> int:
    cfg = TARGET_CONFIG[target_key]
    target_path = normalize_path(cfg["path"])
    target_env = str(cfg["env"])

    if not target_path.exists():
        print(f"[error] target not found: {target_path}", file=sys.stderr)
        return 2

    run_dir = base_log_dir / target_key
    run_dir.mkdir(parents=True, exist_ok=True)

    checkpoint_path = run_dir / "checkpoint.json"
    analysis_path = run_dir / "final_analysis.md"
    progress_path = run_dir / "progress_live.json"

    bot_map: dict[str, BotEntry] = {b.path: b for b in discovered}
    add_explicit_bot(target_path, bot_map)
    target = bot_map[str(target_path)]

    if args.opponent:
        selected: dict[str, BotEntry] = {}
        for raw in args.opponent:
            p = normalize_path(raw)
            add_explicit_bot(p, bot_map)
            key = str(p)
            if key in bot_map:
                selected[key] = bot_map[key]
        pool = list(selected.values())
    else:
        pool = list(bot_map.values())

    regexes = [re.compile(p) for p in args.exclude]
    opponents: list[BotEntry] = []
    for bot in pool:
        if bot.path == target.path:
            continue
        hay = f"{bot.name} {bot.path}"
        if any(rx.search(hay) for rx in regexes):
            continue
        opponents.append(bot)
    opponents.sort(key=lambda b: b.path)

    print(f"\n=== Target {target_key} ===", flush=True)
    print(f"[setup] target: {target.path}", flush=True)
    print(f"[setup] env: {target_env}", flush=True)
    print(f"[setup] log_dir: {run_dir}", flush=True)

    if args.dry_run:
        print(f"[dry-run] opponents ({len(opponents)}):", flush=True)
        for opp in opponents:
            print(f"  - {opp.path}", flush=True)
        return 0

    if not opponents:
        print("[error] no opponents selected", file=sys.stderr)
        return 2

    rng = random.Random(args.seed)
    sigma = args.mutation_sigma
    history: list[dict[str, Any]] = []
    global_best: Candidate | None = None
    skipped: list[dict[str, Any]] = []
    game_id = 0
    population: list[Candidate]
    start_gen = 0

    start_ts = time.time()

    if args.resume and checkpoint_path.exists():
        cp = json.loads(checkpoint_path.read_text())
        sigma = float(cp.get("sigma", sigma))
        history = list(cp.get("history", []))
        game_id = int(cp.get("next_game_id", 0))

        cp_best = cp.get("global_best")
        if isinstance(cp_best, dict):
            global_best = candidate_from_json(cp_best)

        raw_pop = cp.get("population")
        if not isinstance(raw_pop, list) or not raw_pop:
            print("[error] invalid checkpoint population", file=sys.stderr)
            return 2
        prev_pop = [candidate_from_json(x) for x in raw_pop if isinstance(x, dict)]
        if not prev_pop:
            print("[error] empty checkpoint population", file=sys.stderr)
            return 2

        try:
            rng_state = ast.literal_eval(cp.get("rng_state", ""))
            rng.setstate(rng_state)
        except Exception:
            pass

        cp_opponents = cp.get("opponents")
        if isinstance(cp_opponents, list) and cp_opponents:
            restored: list[BotEntry] = []
            for item in cp_opponents:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path", "")).strip()
                if not path:
                    continue
                restored.append(
                    BotEntry(
                        path=path,
                        name=str(item.get("name", Path(path).stem)),
                        python_exec=str(item.get("python_exec", "python3")),
                    )
                )
            if restored:
                opponents = restored

        cp_skipped = cp.get("skipped_opponents")
        if isinstance(cp_skipped, list):
            skipped = [x for x in cp_skipped if isinstance(x, dict)]

        cp_gen = int(cp.get("generation", -1))
        start_gen = cp_gen + 1

        if start_gen >= args.generations:
            population = prev_pop
        else:
            population = build_next_population(
                population=prev_pop,
                pop_size=args.population_size,
                elite_count=args.elite_count,
                immigrants=args.immigrants,
                sigma=sigma,
                rng=rng,
            )
            sigma = max(args.mutation_floor, sigma * args.mutation_decay)
    else:
        population = [Candidate(weights=initial_weights)]
        while len(population) < args.population_size:
            population.append(Candidate(weights=mutate(initial_weights, sigma, rng)))

    opponents, skipped_pf, game_id = preflight(
        target=target,
        target_env=target_env,
        opponents=opponents,
        initial_weights=initial_weights,
        args=args,
        run_dir=run_dir,
        game_id=game_id,
    )
    skipped.extend(skipped_pf)

    if not opponents:
        print("[error] no runnable opponents after preflight", file=sys.stderr)
        return 2

    print(f"[setup] active opponents: {len(opponents)}", flush=True)
    for opp in opponents:
        print(f"  - {safe_rel(Path(opp.path))}", flush=True)

    for gen in range(start_gen, args.generations):
        print(f"\n=== Generation {gen} ===", flush=True)
        print(f"Sigma={sigma:.4f}", flush=True)

        for idx, cand in enumerate(population, start=1):
            if cand.eval_result is None:
                ev, game_id = evaluate_weights(
                    weights=cand.weights,
                    target=target,
                    target_env_name=target_env,
                    opponents=opponents,
                    games_per_opponent=args.games_per_opponent,
                    timeout_s=args.timeout_s,
                    base_port=args.base_port,
                    run_dir=run_dir,
                    keep_game_logs=args.keep_game_logs,
                    error_penalty=args.error_penalty,
                    next_game_id=game_id,
                )
                cand.eval_result = ev

            ev = cand.eval_result
            assert ev is not None
            print(
                f"  [{idx:>2}/{len(population)}] fit={ev.fitness:+.4f} "
                f"worst={ev.worst_fitness:+.4f} "
                f"W/L/D/E={ev.wins}/{ev.losses}/{ev.draws}/{ev.errors} "
                f"weights={format_weights(cand.weights)}",
                flush=True,
            )

        rank_population(population)

        if args.resample_top > 0 and args.resample_rounds > 0:
            top_n = min(args.resample_top, len(population))
            for i in range(top_n):
                for _ in range(args.resample_rounds):
                    extra, game_id = evaluate_weights(
                        weights=population[i].weights,
                        target=target,
                        target_env_name=target_env,
                        opponents=opponents,
                        games_per_opponent=args.games_per_opponent,
                        timeout_s=args.timeout_s,
                        base_port=args.base_port,
                        run_dir=run_dir,
                        keep_game_logs=args.keep_game_logs,
                        error_penalty=args.error_penalty,
                        next_game_id=game_id,
                    )
                    base = population[i].eval_result
                    assert base is not None
                    population[i].eval_result = merge_eval_results(base, extra, args.error_penalty)
            rank_population(population)

        best = population[0]
        assert best.eval_result is not None
        mean_fit = sum(c.eval_result.fitness for c in population if c.eval_result is not None) / max(1, len(population))

        if global_best is None or eval_sort_key(best.eval_result) > eval_sort_key(global_best.eval_result):
            global_best = Candidate(weights=best.weights, eval_result=best.eval_result)

        history.append(
            {
                "generation": gen,
                "sigma": sigma,
                "best_fitness": best.eval_result.fitness,
                "best_worst_fitness": best.eval_result.worst_fitness,
                "mean_fitness": mean_fit,
                "best_weights": list(best.weights),
                "best_wins": best.eval_result.wins,
                "best_losses": best.eval_result.losses,
                "best_draws": best.eval_result.draws,
                "best_errors": best.eval_result.errors,
            }
        )

        print(
            f"Best gen {gen}: fit={best.eval_result.fitness:+.4f} "
            f"worst={best.eval_result.worst_fitness:+.4f} "
            f"W/L/D/E={best.eval_result.wins}/{best.eval_result.losses}/{best.eval_result.draws}/{best.eval_result.errors} "
            f"weights={format_weights(best.weights)}",
            flush=True,
        )

        save_checkpoint(
            path=checkpoint_path,
            generation=gen,
            sigma=sigma,
            population=population,
            history=history,
            global_best=global_best,
            rng_state=rng.getstate(),
            next_gid=game_id,
            target=target,
            target_env=target_env,
            opponents=opponents,
            skipped=skipped,
            args=args,
        )

        write_progress(
            path=progress_path,
            state="running",
            generation=gen,
            sigma=sigma,
            best=global_best,
            next_gid=game_id,
        )

        if gen == args.generations - 1:
            break

        population = build_next_population(
            population=population,
            pop_size=args.population_size,
            elite_count=args.elite_count,
            immigrants=args.immigrants,
            sigma=sigma,
            rng=rng,
        )
        sigma = max(args.mutation_floor, sigma * args.mutation_decay)

    final_validation: EvalResult | None = None
    if global_best and global_best.eval_result and args.final_validation_games > 0:
        print("\n[validation] running final validation...", flush=True)
        final_validation, game_id = evaluate_weights(
            weights=global_best.weights,
            target=target,
            target_env_name=target_env,
            opponents=opponents,
            games_per_opponent=args.final_validation_games,
            timeout_s=args.timeout_s,
            base_port=args.base_port,
            run_dir=run_dir,
            keep_game_logs=args.keep_game_logs,
            error_penalty=args.error_penalty,
            next_game_id=game_id,
        )
        print(
            f"[validation] fit={final_validation.fitness:+.4f} worst={final_validation.worst_fitness:+.4f} "
            f"W/L/D/E={final_validation.wins}/{final_validation.losses}/{final_validation.draws}/{final_validation.errors}",
            flush=True,
        )

    end_ts = time.time()

    write_analysis(
        analysis_path=analysis_path,
        start_ts=start_ts,
        end_ts=end_ts,
        target_key=target_key,
        target=target,
        target_env=target_env,
        discovered_count=len(discovered),
        opponents=opponents,
        skipped=skipped,
        history=history,
        global_best=global_best,
        final_validation=final_validation,
        args=args,
    )

    write_progress(
        path=progress_path,
        state="finished",
        generation=args.generations - 1,
        sigma=sigma,
        best=global_best,
        next_gid=game_id,
    )

    if global_best and global_best.eval_result:
        env_file = run_dir / "best_env.txt"
        env_file.write_text(f"{target_env}={format_weights(global_best.weights)}\n")
        print("\n=== Done ===", flush=True)
        print(f"Target: {target_key}", flush=True)
        print(f"Best fitness: {global_best.eval_result.fitness:+.4f}", flush=True)
        print(f"Best worst-opponent fitness: {global_best.eval_result.worst_fitness:+.4f}", flush=True)
        print(f"Best weights: {format_weights(global_best.weights)}", flush=True)
        print(f"Checkpoint: {checkpoint_path}", flush=True)
        print(f"Analysis: {analysis_path}", flush=True)
        print(f"Best env: {env_file}", flush=True)

    return 0


def main() -> int:
    args = parse_args()

    if not SERVER_JAR.exists():
        print(f"Error: server jar not found: {SERVER_JAR}", file=sys.stderr)
        return 2

    try:
        initial_weights = parse_weights(args.initial_weights)
    except Exception as exc:
        print(f"Error: invalid --initial-weights: {exc}", file=sys.stderr)
        return 2

    if args.log_dir is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_log_dir = ROOT / "log" / "tune_rust_v3" / stamp
    else:
        base_log_dir = normalize_path(args.log_dir)
    base_log_dir.mkdir(parents=True, exist_ok=True)

    discovered = load_discovered_bots(include_starter=args.include_starter)

    targets = ["rust", "v3"] if args.target == "both" else [args.target]

    print(f"[setup] root: {ROOT}", flush=True)
    print(f"[setup] log_dir: {base_log_dir}", flush=True)
    print(f"[setup] discovered bots: {len(discovered)}", flush=True)
    print(f"[setup] targets: {targets}", flush=True)

    rc = 0
    for target_key in targets:
        rc_target = run_single_target(
            target_key=target_key,
            args=args,
            discovered=discovered,
            initial_weights=initial_weights,
            base_log_dir=base_log_dir,
        )
        if rc_target != 0:
            rc = rc_target

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
