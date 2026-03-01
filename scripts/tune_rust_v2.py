#!/usr/bin/env python3
"""
High-throughput eval-weight tuner for rust_v2 against discovered bots.

Highlights:
- discovers runnable python bot entrypoints via benchmark.py discovery
- optional preflight to filter broken opponents automatically
- GA optimization with checkpoint/resume
- parallel candidate evaluation (many games concurrently)
- side-switched matches; env weights applied only to target bot
- writes checkpoint, live progress and final markdown analysis
"""

from __future__ import annotations

import argparse
import ast
import concurrent.futures
import json
import os
import random
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parent.parent.resolve()
SERVER_JAR = ROOT / "server" / "server.jar"

# ── rust_v2 eval weights ──────────────────────────────────────────────────────
# Order must match PIRANHAS_RSV2_EVAL_WEIGHTS (13 comma-separated values)
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
BASE_WEIGHTS = (380.0, 260.0, 50.0, 130.0, 15.0, 4.0, 7.0, 180.0, 130.0, 90.0, 20.0, 12.0, 85000.0)
WEIGHT_BOUNDS = (
    (100.0, 700.0),    # w_largest
    (50.0,  500.0),    # w_components
    (0.0,   200.0),    # w_spread
    (30.0,  300.0),    # w_material
    (0.0,   60.0),     # w_links
    (0.0,   20.0),     # w_center
    (0.0,   30.0),     # w_mobility
    (50.0,  400.0),    # w_late_largest
    (30.0,  300.0),    # w_late_components
    (10.0,  250.0),    # w_late_spread
    (0.0,   80.0),     # w_late_links
    (0.0,   50.0),     # w_late_mobility
    (20000.0, 200000.0),  # connect_bonus
)

N_WEIGHTS = len(WEIGHT_NAMES)  # 13

DEFAULT_TARGET = ROOT / "bots" / "rust_v2" / "pur_rust_client.py"
DEFAULT_TARGET_ENV = "PIRANHAS_RSV2_EVAL_WEIGHTS"
DEFAULT_EXCLUDES = [r"/bots/cpp/", r"/bots/my_player/", r"/submissions/", r"/bots/python/client\.py$"]
_BENCHMARK_MOD: Any | None = None
_PORT_LOCK = threading.Lock()
_RESERVED_PORTS: set[int] = set()


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
    weights: tuple  # N_WEIGHTS floats
    eval_result: EvalResult | None = None


@dataclass
class GameSpec:
    """One scheduled game.  tag is an opaque routing key set by the caller."""
    game_id: int
    bot_one: BotEntry
    bot_two: BotEntry
    env_one: dict[str, str] | None
    env_two: dict[str, str] | None
    target_side: str  # "ONE" or "TWO"
    tag: Any = None


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Tune rust_v2 eval weights with high-throughput parallel evaluation."
    )
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--target-env", default=DEFAULT_TARGET_ENV)
    parser.add_argument(
        "--initial-weights",
        default=",".join(str(v) for v in BASE_WEIGHTS),
        help=f"Comma-separated {N_WEIGHTS} floats (default: rust_v2 baseline).",
    )
    parser.add_argument(
        "--opponent",
        action="append",
        default=None,
        help="Optional explicit opponent path. Can be passed multiple times.",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=None,
        help="Regex pattern to exclude opponents by path/name. Can be repeated.",
    )
    parser.add_argument("--include-starter", action="store_true")
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--preflight-games", type=int, default=2)
    parser.add_argument("--games-per-opponent", type=int, default=2)
    parser.add_argument("--timeout-s", type=int, default=180)
    parser.add_argument("--base-port", type=int, default=16000)
    parser.add_argument("--error-penalty", type=float, default=1.0)
    parser.add_argument(
        "--parallel-games",
        type=int,
        default=0,
        help="Concurrent games during candidate evaluation (0 = auto).",
    )
    parser.add_argument(
        "--cpu-cores",
        type=int,
        default=0,
        help="CPU cores to assume for auto parallelism (0 = os.cpu_count()).",
    )
    parser.add_argument(
        "--cores-per-game",
        type=float,
        default=2.0,
        help="Estimated cores per running game for auto parallelism.",
    )
    parser.add_argument(
        "--reserve-cores",
        type=int,
        default=2,
        help="Reserved cores for OS/background workload when auto-sizing.",
    )
    parser.add_argument(
        "--max-parallel-games",
        type=int,
        default=16,
        help="Upper bound for auto-sized parallel games.",
    )
    parser.add_argument(
        "--max-cores",
        action="store_true",
        help="Aggressive auto mode: one game per core (up to --max-parallel-games).",
    )
    parser.add_argument(
        "--core-budget",
        type=int,
        default=0,
        help="Hard cap on total CPU cores the tuner may consume across all concurrent "
             "games (0 = no extra cap). Divides by --cores-per-game to derive the "
             "maximum number of parallel games, then takes the minimum with the other "
             "limits. Example for a 30-core HPC server: --core-budget 30",
    )

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

    parser.add_argument("--final-validation-games", type=int, default=10)

    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--checkpoint", type=Path, default=None)
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
    if args.parallel_games < 0:
        raise SystemExit("--parallel-games must be >= 0")
    if args.cpu_cores < 0:
        raise SystemExit("--cpu-cores must be >= 0")
    if args.cores_per_game <= 0:
        raise SystemExit("--cores-per-game must be > 0")
    if args.reserve_cores < 0:
        raise SystemExit("--reserve-cores must be >= 0")
    if args.max_parallel_games <= 0:
        raise SystemExit("--max-parallel-games must be > 0")
    if args.core_budget < 0:
        raise SystemExit("--core-budget must be >= 0")

    return args


def resolve_parallel_games(args: argparse.Namespace) -> tuple[int, int]:
    cpu_cores = args.cpu_cores if args.cpu_cores > 0 else (os.cpu_count() or 1)

    if args.parallel_games > 0:
        # Explicit override — still honour core-budget if set.
        result = args.parallel_games
        if args.core_budget > 0:
            result = min(result, max(1, int(args.core_budget / args.cores_per_game)))
        return result, cpu_cores

    if args.max_cores:
        auto = max(1, min(cpu_cores, args.max_parallel_games))
    else:
        usable = max(1, cpu_cores - args.reserve_cores)
        auto = max(1, int(usable / args.cores_per_game))
        auto = min(auto, args.max_parallel_games)

    # Hard cap from core budget (e.g. --core-budget 30 on a 30-core HPC node).
    if args.core_budget > 0:
        budget_cap = max(1, int(args.core_budget / args.cores_per_game))
        auto = min(auto, budget_cap)

    return auto, cpu_cores


def parse_weights(raw: str) -> tuple:
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != N_WEIGHTS:
        raise ValueError(f"exactly {N_WEIGHTS} weights are required")
    vals = tuple(float(p) for p in parts)
    if any((not (v == v) or v in (float("inf"), float("-inf"))) for v in vals):
        raise ValueError("weights must be finite")
    return vals


def format_weights(weights: tuple) -> str:
    return ",".join(f"{w:.10g}" for w in weights)


def clamp_weight(idx: int, value: float) -> float:
    lo, hi = WEIGHT_BOUNDS[idx]
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def mutate(weights: tuple, sigma: float, rng: random.Random) -> tuple:
    out: list[float] = []
    for idx, w in enumerate(weights):
        lo, hi = WEIGHT_BOUNDS[idx]
        span = hi - lo
        if rng.random() < 0.75:
            w = w + rng.gauss(0.0, sigma * span)
        out.append(clamp_weight(idx, w))
    return tuple(out)


def crossover(a: tuple, b: tuple, rng: random.Random) -> tuple:
    child: list[float] = []
    for idx, (av, bv) in enumerate(zip(a, b)):
        t = rng.random()
        v = av * t + bv * (1.0 - t)
        child.append(clamp_weight(idx, v))
    return tuple(child)


def tournament_select(population: list[Candidate], rng: random.Random, k: int = 3) -> Candidate:
    sample = rng.sample(population, min(k, len(population)))
    sample.sort(key=lambda c: eval_sort_key(c.eval_result), reverse=True)
    return sample[0]


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
        out.append(BotEntry(path=str(path), name=item.name, python_exec=item.python_exec))

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


def filter_opponents(
    all_bots: list[BotEntry],
    target: Path,
    include: list[Path] | None,
    exclude_patterns: list[str],
) -> list[BotEntry]:
    target_resolved = str(normalize_path(target))

    all_map: dict[str, BotEntry] = {b.path: b for b in all_bots}
    if include:
        selected: dict[str, BotEntry] = {}
        for p in include:
            add_explicit_bot(p, all_map)
            key = str(normalize_path(p))
            if key in all_map:
                selected[key] = all_map[key]
        bot_map = selected
    else:
        bot_map = all_map

    regexes = [re.compile(p) for p in exclude_patterns]

    out: list[BotEntry] = []
    for bot in bot_map.values():
        if bot.path == target_resolved:
            continue
        hay = f"{bot.name} {bot.path}"
        if any(rx.search(hay) for rx in regexes):
            continue
        out.append(bot)

    out.sort(key=lambda b: b.path)
    return out


def find_free_port(start: int) -> int:
    for port in range(start, start + 5000):
        with _PORT_LOCK:
            if port in _RESERVED_PORTS:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                if sock.connect_ex(("127.0.0.1", port)) != 0:
                    _RESERVED_PORTS.add(port)
                    return port
    raise RuntimeError("No free port found")


def release_reserved_port(port: int | None) -> None:
    if port is None:
        return
    with _PORT_LOCK:
        _RESERVED_PORTS.discard(port)


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
    """Return 'ONE', 'TWO', 'DRAW', or 'UNKNOWN' from any game log format."""
    s = re.sub(r"\x1b\[[0-9;]*m", "", log_content)  # strip ANSI

    # ── explicit winner= patterns ────────────────────────────────────────────
    # "winner=ONE" / "winner=TWO"  (legacy server log)
    if re.search(r"winner\s*=\s*ONE\b", s, re.IGNORECASE):
        return "ONE"
    if re.search(r"winner\s*=\s*TWO\b", s, re.IGNORECASE):
        return "TWO"
    # "Winner: ONE"
    if re.search(r"Winner:\s*ONE\b", s, re.IGNORECASE):
        return "ONE"
    if re.search(r"Winner:\s*TWO\b", s, re.IGNORECASE):
        return "TWO"
    # "winner=Team One" / "winner=Team Two"
    if re.search(r"winner\s*=\s*Team One\b", s, re.IGNORECASE):
        return "ONE"
    if re.search(r"winner\s*=\s*Team Two\b", s, re.IGNORECASE):
        return "TWO"

    # ── Python-bot log: Winner(team='ONE', ...)  ─────────────────────────────
    if re.search(r"Winner\s*\(\s*team\s*=\s*['\"]?ONE['\"]?", s, re.IGNORECASE):
        return "ONE"
    if re.search(r"Winner\s*\(\s*team\s*=\s*['\"]?TWO['\"]?", s, re.IGNORECASE):
        return "TWO"

    # ── Rust-bot log: Winner { team: One, ... }  ─────────────────────────────
    if re.search(r"Winner\s*\{[^}]*team\s*:\s*One\b", s, re.IGNORECASE):
        return "ONE"
    if re.search(r"Winner\s*\{[^}]*team\s*:\s*Two\b", s, re.IGNORECASE):
        return "TWO"

    # ── draw / no winner  ────────────────────────────────────────────────────
    if re.search(
        r"\b(draw|tie|unentschieden|gleichstand)\b|winner\s*=\s*(NONE|NULL|KEINER?)\b",
        s, re.IGNORECASE,
    ):
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
    env_one: dict[str, str] | None = None,
    env_two: dict[str, str] | None = None,
) -> dict[str, Any]:
    logs_dir = run_dir / "games"
    logs_dir.mkdir(parents=True, exist_ok=True)

    server_log = logs_dir / f"game_{game_id:06d}_server.log"
    bot1_log = logs_dir / f"game_{game_id:06d}_bot_one.log"
    bot2_log = logs_dir / f"game_{game_id:06d}_bot_two.log"

    port_seed = base_port + ((game_id * 19) % 12000)
    port: int | None = find_free_port(port_seed)

    result: dict[str, Any] = {
        "winner": "UNKNOWN",
        "bot1_crash": False,
        "bot2_crash": False,
        "timeout": False,
        "server_log": str(server_log),
        "bot1_log": str(bot1_log),
        "bot2_log": str(bot2_log),
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
                cwd=str(SERVER_JAR.parent),  # must run from server/ so lib/ is found
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
            content = server_log.read_text(encoding="utf-8", errors="ignore")
            result["winner"] = parse_winner(content)
        else:
            result["winner"] = "UNKNOWN"
        # Fallback: try bot logs if server log didn't yield a clear winner
        if result["winner"] == "UNKNOWN":
            for log_path in (bot1_log, bot2_log):
                if log_path.exists():
                    w = parse_winner(log_path.read_text(encoding="utf-8", errors="ignore"))
                    if w != "UNKNOWN":
                        result["winner"] = w
                        break

    except Exception:
        result["winner"] = "ERROR"
    finally:
        kill_process_group(bot1_proc)
        kill_process_group(bot2_proc)
        kill_process_group(server_proc)
        release_reserved_port(port)

        if not keep_logs:
            if result["winner"] in {"ONE", "TWO", "DRAW"} and not result["bot1_crash"] and not result["bot2_crash"]:
                server_log.unlink(missing_ok=True)
                bot1_log.unlink(missing_ok=True)
                bot2_log.unlink(missing_ok=True)

    return result


def run_game_specs_parallel(
    specs: list[GameSpec],
    parallel_games: int,
    timeout_s: int,
    base_port: int,
    run_dir: Path,
    keep_game_logs: bool,
) -> list[tuple[GameSpec, str]]:
    """Run specs concurrently, keeping at most parallel_games games in flight.

    Returns (spec, winner) pairs in the same order as *specs*.
    The ThreadPoolExecutor naturally saturates parallel_games workers so cores
    are never idle as long as there are pending jobs.
    """
    if not specs:
        return []

    max_workers = max(1, min(parallel_games, len(specs)))
    out: list[tuple[GameSpec, str]] = [None] * len(specs)  # type: ignore[list-item]

    def _run(idx_spec: tuple[int, GameSpec]) -> tuple[int, str]:
        i, spec = idx_spec
        game = run_game(
            bot_one=spec.bot_one,
            bot_two=spec.bot_two,
            game_id=spec.game_id,
            timeout_s=timeout_s,
            base_port=base_port,
            run_dir=run_dir,
            keep_logs=keep_game_logs,
            env_one=spec.env_one,
            env_two=spec.env_two,
        )
        return i, str(game.get("winner", "UNKNOWN"))

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {pool.submit(_run, (i, s)): i for i, s in enumerate(specs)}
        for fut in concurrent.futures.as_completed(future_map):
            i = future_map[fut]
            try:
                _, winner = fut.result()
            except Exception as exc:
                winner = "ERROR"
                print(f"  [game {specs[i].game_id}] error: {exc}", flush=True)
            out[i] = (specs[i], winner)

    return out


def _tally(winner: str, target_side: str, stats: OpponentStats) -> None:
    stats.games += 1
    if winner == target_side:
        stats.wins += 1
    elif winner in ("ONE", "TWO"):
        stats.losses += 1
    elif winner == "DRAW":
        stats.draws += 1
    else:
        stats.errors += 1


def _make_specs(
    env_target: dict[str, str],
    target: BotEntry,
    opp: BotEntry,
    games_per_opponent: int,
    start_gid: int,
    tag: Any,
) -> list[GameSpec]:
    """Build GameSpec objects for one (candidate, opponent) pair."""
    specs = []
    for rep in range(games_per_opponent):
        if rep % 2 == 0:
            bot_one, bot_two = target, opp
            env_one, env_two = env_target, None
            target_side = "ONE"
        else:
            bot_one, bot_two = opp, target
            env_one, env_two = None, env_target
            target_side = "TWO"
        specs.append(GameSpec(
            game_id=start_gid + rep,
            bot_one=bot_one,
            bot_two=bot_two,
            env_one=env_one,
            env_two=env_two,
            target_side=target_side,
            tag=tag,
        ))
    return specs


def _make_eval_result(per_opp: dict[str, OpponentStats], error_penalty: float) -> EvalResult:
    scores = [score_from_stats(s, error_penalty) for s in per_opp.values()]
    return EvalResult(
        fitness=sum(scores) / max(1, len(scores)),
        worst_fitness=min(scores) if scores else -1.0,
        wins=sum(s.wins for s in per_opp.values()),
        losses=sum(s.losses for s in per_opp.values()),
        draws=sum(s.draws for s in per_opp.values()),
        errors=sum(s.errors for s in per_opp.values()),
        games=sum(s.games for s in per_opp.values()),
        per_opponent=per_opp,
    )


def score_from_stats(stats: OpponentStats, error_penalty: float) -> float:
    if stats.games <= 0:
        return -1.0
    points = stats.wins + 0.5 * stats.draws - error_penalty * stats.errors
    return points / stats.games


def eval_sort_key(eval_result: EvalResult | None) -> tuple[float, float, float, float]:
    if eval_result is None:
        return (-999.0, -999.0, -999.0, -999.0)
    return (
        eval_result.fitness,
        eval_result.worst_fitness,
        -float(eval_result.errors),
        float(eval_result.wins - eval_result.losses),
    )


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
    total_w = sum(s.wins for s in merged.values())
    total_l = sum(s.losses for s in merged.values())
    total_d = sum(s.draws for s in merged.values())
    total_e = sum(s.errors for s in merged.values())
    total_g = sum(s.games for s in merged.values())

    return EvalResult(
        fitness=sum(scores) / max(1, len(scores)),
        worst_fitness=min(scores) if scores else -1.0,
        wins=total_w,
        losses=total_l,
        draws=total_d,
        errors=total_e,
        games=total_g,
        per_opponent=merged,
    )


def evaluate_weights(
    weights: tuple,
    target: BotEntry,
    opponents: list[BotEntry],
    games_per_opponent: int,
    timeout_s: int,
    base_port: int,
    run_dir: Path,
    keep_game_logs: bool,
    target_env_name: str,
    error_penalty: float,
    next_game_id: int,
    parallel_games: int = 1,
) -> tuple[EvalResult, int]:
    env_target = {target_env_name: format_weights(weights)}
    gid = next_game_id
    specs: list[GameSpec] = []
    for opp in opponents:
        specs.extend(_make_specs(env_target, target, opp, games_per_opponent, gid, opp.path))
        gid += games_per_opponent

    per_opponent: dict[str, OpponentStats] = {opp.path: OpponentStats() for opp in opponents}
    for spec, winner in run_game_specs_parallel(specs, parallel_games, timeout_s, base_port, run_dir, keep_game_logs):
        _tally(winner, spec.target_side, per_opponent[spec.tag])

    return _make_eval_result(per_opponent, error_penalty), gid



def evaluate_pending_candidates_parallel(
    population: list[Candidate],
    target: BotEntry,
    opponents: list[BotEntry],
    args: argparse.Namespace,
    run_dir: Path,
    start_game_id: int,
    parallel_games: int,
) -> int:
    pending = [(idx, c) for idx, c in enumerate(population, 1) if c.eval_result is None]
    if not pending:
        return start_game_id

    games_per_eval = len(opponents) * args.games_per_opponent
    gid = start_game_id

    # One GameSpec per game across ALL pending candidates — the shared pool
    # keeps exactly parallel_games games in flight at all times.
    specs: list[GameSpec] = []
    for idx, cand in pending:
        env_target = {args.target_env: format_weights(cand.weights)}
        for opp in opponents:
            specs.extend(_make_specs(env_target, target, opp, args.games_per_opponent, gid, (idx, opp.path)))
            gid += args.games_per_opponent

    total_jobs = len(specs)
    print(
        f"[eval] pending={len(pending)} jobs={total_jobs} "
        f"parallel_games={min(parallel_games, total_jobs)} games_per_candidate={games_per_eval}",
        flush=True,
    )

    per_candidate: dict[int, dict[str, OpponentStats]] = {
        idx: {opp.path: OpponentStats() for opp in opponents} for idx, _ in pending
    }

    for spec, winner in run_game_specs_parallel(
        specs, parallel_games, args.timeout_s, args.base_port, run_dir, args.keep_game_logs
    ):
        idx, opp_path = spec.tag
        _tally(winner, spec.target_side, per_candidate[idx][opp_path])

    for idx, cand in pending:
        ev = _make_eval_result(per_candidate[idx], args.error_penalty)
        cand.eval_result = ev
        print(
            f"  [{idx:>2}/{len(population)}] fit={ev.fitness:+.4f} "
            f"worst={ev.worst_fitness:+.4f} "
            f"W/L/D/E={ev.wins}/{ev.losses}/{ev.draws}/{ev.errors} "
            f"weights={format_weights(cand.weights)}",
            flush=True,
        )

    return gid


def candidate_to_json(candidate: Candidate) -> dict[str, Any]:
    payload: dict[str, Any] = {"weights": list(candidate.weights)}
    if candidate.eval_result is not None:
        per_opp = {
            path: asdict(stats)
            for path, stats in candidate.eval_result.per_opponent.items()
        }
        payload["eval_result"] = {
            "fitness": candidate.eval_result.fitness,
            "worst_fitness": candidate.eval_result.worst_fitness,
            "wins": candidate.eval_result.wins,
            "losses": candidate.eval_result.losses,
            "draws": candidate.eval_result.draws,
            "errors": candidate.eval_result.errors,
            "games": candidate.eval_result.games,
            "per_opponent": per_opp,
        }
    else:
        payload["eval_result"] = None
    return payload


def candidate_from_json(payload: dict[str, Any]) -> Candidate:
    weights_raw = payload.get("weights")
    if not isinstance(weights_raw, list) or len(weights_raw) != N_WEIGHTS:
        raise ValueError(f"invalid candidate weights in checkpoint (expected {N_WEIGHTS})")
    weights = tuple(float(x) for x in weights_raw)

    ev_raw = payload.get("eval_result")
    if not isinstance(ev_raw, dict):
        return Candidate(weights=weights)

    per_raw = ev_raw.get("per_opponent")
    if not isinstance(per_raw, dict):
        per_raw = {}

    per_opp: dict[str, OpponentStats] = {}
    for key, value in per_raw.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        per_opp[key] = OpponentStats(
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
        per_opponent=per_opp,
    )
    return Candidate(weights=weights, eval_result=ev)


def save_checkpoint(
    checkpoint_path: Path,
    generation: int,
    sigma: float,
    population: list[Candidate],
    history: list[dict[str, Any]],
    global_best: Candidate | None,
    rng_state: object,
    next_game_id: int,
    args: argparse.Namespace,
    target: BotEntry,
    opponents: list[BotEntry],
    skipped_opponents: list[dict[str, Any]],
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
        "next_game_id": next_game_id,
        "target": asdict(target),
        "opponents": [asdict(o) for o in opponents],
        "skipped_opponents": skipped_opponents,
        "args": {
            "target_env": args.target_env,
            "games_per_opponent": args.games_per_opponent,
            "timeout_s": args.timeout_s,
            "base_port": args.base_port,
            "parallel_games": args.parallel_games,
            "cpu_cores": args.cpu_cores,
            "cores_per_game": args.cores_per_game,
            "reserve_cores": args.reserve_cores,
            "max_parallel_games": args.max_parallel_games,
            "max_cores": args.max_cores,
            "population_size": args.population_size,
            "elite_count": args.elite_count,
            "mutation_sigma": args.mutation_sigma,
            "mutation_decay": args.mutation_decay,
            "mutation_floor": args.mutation_floor,
            "seed": args.seed,
            "error_penalty": args.error_penalty,
        },
    }
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text(json.dumps(payload, indent=2))


def load_checkpoint(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def rank_population(population: list[Candidate]) -> None:
    population.sort(key=lambda c: eval_sort_key(c.eval_result), reverse=True)


def build_next_population(
    population: list[Candidate],
    pop_size: int,
    elite_count: int,
    immigrants: int,
    sigma: float,
    rng: random.Random,
) -> list[Candidate]:
    rank_population(population)
    next_pop: list[Candidate] = []

    for elite in population[:elite_count]:
        next_pop.append(Candidate(weights=elite.weights))

    immigrants = min(max(0, immigrants), max(0, pop_size - len(next_pop)))
    for _ in range(immigrants):
        rand_w = []
        for idx, (lo, hi) in enumerate(WEIGHT_BOUNDS):
            v = rng.uniform(lo, hi)
            rand_w.append(clamp_weight(idx, v))
        next_pop.append(Candidate(weights=tuple(rand_w)))

    while len(next_pop) < pop_size:
        p1 = tournament_select(population, rng)
        p2 = tournament_select(population, rng)
        child = crossover(p1.weights, p2.weights, rng)
        child = mutate(child, sigma, rng)
        next_pop.append(Candidate(weights=child))

    return next_pop


def preflight_opponents(
    target: BotEntry,
    opponents: list[BotEntry],
    initial_weights: tuple,
    args: argparse.Namespace,
    run_dir: Path,
    start_game_id: int,
    parallel_games: int,
) -> tuple[list[BotEntry], list[dict[str, Any]], int]:
    if args.skip_preflight:
        return opponents, [], start_game_id

    print(f"[preflight] opponents to test: {len(opponents)}", flush=True)

    env_target = {args.target_env: format_weights(initial_weights)}
    gid = start_game_id

    # Decompose all preflight games into individual specs so the shared pool
    # keeps parallel_games games running at all times regardless of opponent order.
    specs: list[GameSpec] = []
    for idx, opp in enumerate(opponents, start=1):
        specs.extend(_make_specs(env_target, target, opp, args.preflight_games, gid, (idx, opp)))
        gid += args.preflight_games

    per_opp_stats: dict[str, OpponentStats] = {opp.path: OpponentStats() for opp in opponents}
    for spec, winner in run_game_specs_parallel(
        specs, parallel_games, args.timeout_s, args.base_port, run_dir, args.keep_game_logs
    ):
        _idx, opp = spec.tag
        _tally(winner, spec.target_side, per_opp_stats[opp.path])

    usable: list[BotEntry] = []
    skipped: list[dict[str, Any]] = []
    for idx, opp in enumerate(opponents, start=1):
        res = per_opp_stats[opp.path]
        ok = (res.wins + res.losses + res.draws) > 0
        status = "OK" if ok else "SKIP"
        print(
            f"[preflight] {idx:>2}/{len(opponents)} {status} "
            f"{safe_rel(Path(opp.path))} W/L/D/E={res.wins}/{res.losses}/{res.draws}/{res.errors}",
            flush=True,
        )
        if ok:
            usable.append(opp)
        else:
            skipped.append({
                "path": opp.path,
                "name": opp.name,
                "reason": "no_decisive_or_draw_result",
                "wins": res.wins,
                "losses": res.losses,
                "draws": res.draws,
                "errors": res.errors,
            })

    return usable, skipped, gid


def write_progress(
    path: Path,
    state: str,
    generation: int,
    sigma: float,
    best: Candidate | None,
    next_game_id: int,
) -> None:
    payload: dict[str, Any] = {
        "time": now_iso(),
        "state": state,
        "generation": generation,
        "sigma": sigma,
        "next_game_id": next_game_id,
    }
    if best is not None and best.eval_result is not None:
        payload["best"] = {
            "fitness": best.eval_result.fitness,
            "worst_fitness": best.eval_result.worst_fitness,
            "wins": best.eval_result.wins,
            "losses": best.eval_result.losses,
            "draws": best.eval_result.draws,
            "errors": best.eval_result.errors,
            "games": best.eval_result.games,
            "weights": list(best.weights),
            "named": dict(zip(WEIGHT_NAMES, best.weights)),
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def write_final_analysis(
    analysis_path: Path,
    args: argparse.Namespace,
    start_ts: float,
    end_ts: float,
    target: BotEntry,
    discovered_count: int,
    opponents: list[BotEntry],
    skipped_opponents: list[dict[str, Any]],
    history: list[dict[str, Any]],
    global_best: Candidate | None,
    final_validation: EvalResult | None,
) -> None:
    lines: list[str] = []
    lines.append("# rust_v2 Tuning Analysis")
    lines.append("")
    lines.append(f"- Generated: {now_iso()}")
    lines.append(f"- Runtime: {int(end_ts - start_ts)}s")
    lines.append(f"- Start: {datetime.fromtimestamp(start_ts).isoformat(timespec='seconds')}")
    lines.append(f"- End: {datetime.fromtimestamp(end_ts).isoformat(timespec='seconds')}")
    lines.append("")
    lines.append("## Configuration")
    lines.append("")
    lines.append(f"- Target: {target.path}")
    lines.append(f"- Target env var: {args.target_env}")
    lines.append(f"- Weight names: {', '.join(WEIGHT_NAMES)}")
    lines.append(f"- Discovered bots: {discovered_count}")
    lines.append(f"- Active opponents: {len(opponents)}")
    lines.append(f"- Games per opponent: {args.games_per_opponent}")
    lines.append(f"- Timeout per game: {args.timeout_s}s")
    lines.append(f"- Parallel games: {args.parallel_games}")
    lines.append(
        f"- Auto sizing: cpu_cores={args.cpu_cores}, cores_per_game={args.cores_per_game}, "
        f"reserve_cores={args.reserve_cores}, max_parallel_games={args.max_parallel_games}, "
        f"max_cores={args.max_cores}"
    )
    lines.append(f"- Population/Elites: {args.population_size}/{args.elite_count}")
    lines.append(f"- Mutation sigma/decay/floor: {args.mutation_sigma}/{args.mutation_decay}/{args.mutation_floor}")
    lines.append(f"- Seed: {args.seed}")
    lines.append("")
    lines.append("## Opponents")
    lines.append("")
    for opp in opponents:
        lines.append(f"- {opp.path}")
    if not opponents:
        lines.append("- none")

    lines.append("")
    lines.append("## Skipped In Preflight")
    lines.append("")
    if skipped_opponents:
        for item in skipped_opponents:
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
        lines.append(f"- Best weights (raw): {list(global_best.weights)}")
        lines.append(f"- Best weights (named):")
        for name, val in zip(WEIGHT_NAMES, global_best.weights):
            lines.append(f"  - {name}: {val:.10g}")
        lines.append(f"- Env line:  {args.target_env}={format_weights(global_best.weights)}")
        lines.append(f"- Aggregate W/L/D/E: {ev.wins}/{ev.losses}/{ev.draws}/{ev.errors}")
        lines.append(f"- Aggregate games: {ev.games}")
    else:
        lines.append("- No best result available.")

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


def main() -> int:
    args = parse_args()

    target_path = normalize_path(args.target)
    if not target_path.exists():
        print(f"Error: target not found: {target_path}", file=sys.stderr)
        return 2

    if args.log_dir is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_dir = ROOT / "log" / "tune_rust_v2" / stamp
    else:
        log_dir = normalize_path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    checkpoint_path = normalize_path(args.checkpoint) if args.checkpoint else (log_dir / "checkpoint.json")
    progress_path = log_dir / "progress_live.json"
    analysis_path = log_dir / "final_analysis.md"

    try:
        initial_weights = parse_weights(args.initial_weights)
    except Exception as exc:
        print(f"Error: invalid --initial-weights: {exc}", file=sys.stderr)
        return 2

    parallel_games, detected_cpu = resolve_parallel_games(args)
    args.parallel_games = parallel_games
    args.cpu_cores = detected_cpu

    print(f"[setup] root: {ROOT}", flush=True)
    print(f"[setup] target: {target_path}", flush=True)
    print(f"[setup] log_dir: {log_dir}", flush=True)
    print(f"[setup] weight names: {', '.join(WEIGHT_NAMES)}", flush=True)
    print(f"[setup] initial weights: {format_weights(initial_weights)}", flush=True)
    budget_str = f", core_budget={args.core_budget}" if args.core_budget > 0 else ""
    print(
        f"[setup] parallel_games={parallel_games} (cpu_cores={detected_cpu}, "
        f"cores_per_game={args.cores_per_game}, reserve_cores={args.reserve_cores}, "
        f"max_cores={args.max_cores}{budget_str})",
        flush=True,
    )

    discovered = load_discovered_bots(include_starter=args.include_starter)
    discovered_count = len(discovered)

    bot_map = {b.path: b for b in discovered}
    target_key = str(target_path)
    if target_key not in bot_map:
        add_explicit_bot(target_path, bot_map)
    target = bot_map[target_key]

    include_paths = [Path(p) for p in args.opponent] if args.opponent else None
    opponents = filter_opponents(
        list(bot_map.values()),
        target=target_path,
        include=include_paths,
        exclude_patterns=args.exclude,
    )

    if args.dry_run:
        print("\n[discovery] target", target.path)
        print(f"[discovery] discovered bots: {discovered_count}")
        print(f"[discovery] opponents ({len(opponents)}):")
        for opp in opponents:
            print(f"  - {opp.path}")
        return 0

    if not SERVER_JAR.exists():
        print(f"Error: server jar not found: {SERVER_JAR}", file=sys.stderr)
        return 2

    rng = random.Random(args.seed)
    history: list[dict[str, Any]] = []
    global_best: Candidate | None = None
    sigma = args.mutation_sigma
    skipped_opponents: list[dict[str, Any]] = []
    game_id = 0

    start_ts = time.time()

    population: list[Candidate]
    start_gen = 0

    if args.resume and checkpoint_path.exists():
        cp = load_checkpoint(checkpoint_path)
        history = list(cp.get("history", []))
        sigma = float(cp.get("sigma", args.mutation_sigma))
        game_id = int(cp.get("next_game_id", 0))

        cp_best = cp.get("global_best")
        if isinstance(cp_best, dict):
            global_best = candidate_from_json(cp_best)

        raw_population = cp.get("population")
        if not isinstance(raw_population, list) or not raw_population:
            print("Error: invalid checkpoint population", file=sys.stderr)
            return 2

        prev_population = [candidate_from_json(item) for item in raw_population if isinstance(item, dict)]
        if not prev_population:
            print("Error: empty checkpoint population", file=sys.stderr)
            return 2

        try:
            rng_state = ast.literal_eval(cp.get("rng_state", ""))
            rng.setstate(rng_state)
        except Exception:
            pass

        cp_gen = int(cp.get("generation", -1))
        start_gen = cp_gen + 1

        cp_target = cp.get("target")
        if isinstance(cp_target, dict):
            print(f"[resume] checkpoint target: {cp_target.get('path')}", flush=True)

        cp_opponents = cp.get("opponents")
        if isinstance(cp_opponents, list) and cp_opponents:
            restored: list[BotEntry] = []
            for item in cp_opponents:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path", "")).strip()
                if not path:
                    continue
                python_exec = str(item.get("python_exec", "python3"))
                name = str(item.get("name", Path(path).stem))
                restored.append(BotEntry(path=path, name=name, python_exec=python_exec))
            if restored:
                opponents = restored

        cp_skipped = cp.get("skipped_opponents")
        if isinstance(cp_skipped, list):
            skipped_opponents = [x for x in cp_skipped if isinstance(x, dict)]

        if start_gen >= args.generations:
            print("[resume] checkpoint already reached requested generations", flush=True)
            population = prev_population
        else:
            population = build_next_population(
                prev_population,
                pop_size=args.population_size,
                elite_count=args.elite_count,
                immigrants=args.immigrants,
                sigma=sigma,
                rng=rng,
            )
            sigma = max(args.mutation_floor, sigma * args.mutation_decay)
            print(f"[resume] continuing from generation {start_gen}", flush=True)
    else:
        population = [Candidate(weights=initial_weights)]
        while len(population) < args.population_size:
            population.append(Candidate(weights=mutate(initial_weights, sigma, rng)))

    if not opponents:
        print("Error: no opponents selected", file=sys.stderr)
        return 2

    opponents, skipped_from_preflight, game_id = preflight_opponents(
        target=target,
        opponents=opponents,
        initial_weights=initial_weights,
        args=args,
        run_dir=log_dir,
        start_game_id=game_id,
        parallel_games=parallel_games,
    )
    skipped_opponents.extend(skipped_from_preflight)

    if not opponents:
        print("Error: no runnable opponents after preflight", file=sys.stderr)
        return 2

    print(f"[setup] active opponents: {len(opponents)}", flush=True)
    for opp in opponents:
        print(f"  - {safe_rel(Path(opp.path))}", flush=True)

    for gen in range(start_gen, args.generations):
        print(f"\n=== Generation {gen} ===", flush=True)
        print(f"Sigma={sigma:.4f}", flush=True)

        game_id = evaluate_pending_candidates_parallel(
            population=population,
            target=target,
            opponents=opponents,
            args=args,
            run_dir=log_dir,
            start_game_id=game_id,
            parallel_games=parallel_games,
        )

        rank_population(population)

        # optional noise reduction for top candidates — all resample games run
        # through the shared pool so cores stay busy across all top candidates.
        if args.resample_top > 0 and args.resample_rounds > 0:
            top_n = min(args.resample_top, len(population))
            resample_specs: list[GameSpec] = []
            for i in range(top_n):
                env_target = {args.target_env: format_weights(population[i].weights)}
                for _round in range(args.resample_rounds):
                    for opp in opponents:
                        resample_specs.extend(
                            _make_specs(env_target, target, opp, args.games_per_opponent, game_id, (i, opp.path))
                        )
                        game_id += args.games_per_opponent

            per_resample: dict[int, dict[str, OpponentStats]] = {
                i: {opp.path: OpponentStats() for opp in opponents} for i in range(top_n)
            }
            for spec, winner in run_game_specs_parallel(
                resample_specs, parallel_games, args.timeout_s, args.base_port, log_dir, args.keep_game_logs
            ):
                i, opp_path = spec.tag
                _tally(winner, spec.target_side, per_resample[i][opp_path])

            for i in range(top_n):
                extra = _make_eval_result(per_resample[i], args.error_penalty)
                base = population[i].eval_result
                assert base is not None
                population[i].eval_result = merge_eval_results(base, extra, args.error_penalty)
            rank_population(population)

        best = population[0]
        assert best.eval_result is not None
        mean_fit = sum(c.eval_result.fitness for c in population if c.eval_result is not None) / max(1, len(population))

        if global_best is None or eval_sort_key(best.eval_result) > eval_sort_key(global_best.eval_result):
            global_best = Candidate(weights=best.weights, eval_result=best.eval_result)

        history_item = {
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
        history.append(history_item)

        print(
            f"Best gen {gen}: fit={best.eval_result.fitness:+.4f} "
            f"worst={best.eval_result.worst_fitness:+.4f} "
            f"W/L/D/E={best.eval_result.wins}/{best.eval_result.losses}/{best.eval_result.draws}/{best.eval_result.errors} "
            f"weights={format_weights(best.weights)}",
            flush=True,
        )

        save_checkpoint(
            checkpoint_path=checkpoint_path,
            generation=gen,
            sigma=sigma,
            population=population,
            history=history,
            global_best=global_best,
            rng_state=rng.getstate(),
            next_game_id=game_id,
            args=args,
            target=target,
            opponents=opponents,
            skipped_opponents=skipped_opponents,
        )
        write_progress(
            path=progress_path,
            state="running",
            generation=gen,
            sigma=sigma,
            best=global_best,
            next_game_id=game_id,
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
    if global_best is not None and global_best.eval_result is not None and args.final_validation_games > 0:
        print("\n[validation] running final validation...", flush=True)
        final_validation, game_id = evaluate_weights(
            weights=global_best.weights,
            target=target,
            opponents=opponents,
            games_per_opponent=args.final_validation_games,
            timeout_s=args.timeout_s,
            base_port=args.base_port,
            run_dir=log_dir,
            keep_game_logs=args.keep_game_logs,
            target_env_name=args.target_env,
            error_penalty=args.error_penalty,
            next_game_id=game_id,
            parallel_games=parallel_games,
        )
        print(
            f"[validation] fit={final_validation.fitness:+.4f} worst={final_validation.worst_fitness:+.4f} "
            f"W/L/D/E={final_validation.wins}/{final_validation.losses}/{final_validation.draws}/{final_validation.errors}",
            flush=True,
        )

    end_ts = time.time()

    write_final_analysis(
        analysis_path=analysis_path,
        args=args,
        start_ts=start_ts,
        end_ts=end_ts,
        target=target,
        discovered_count=discovered_count,
        opponents=opponents,
        skipped_opponents=skipped_opponents,
        history=history,
        global_best=global_best,
        final_validation=final_validation,
    )

    write_progress(
        path=progress_path,
        state="finished",
        generation=args.generations - 1,
        sigma=sigma,
        best=global_best,
        next_game_id=game_id,
    )

    if global_best and global_best.eval_result:
        env_file = log_dir / "best_env.txt"
        env_file.write_text(f"{args.target_env}={format_weights(global_best.weights)}\n")
        print("\n=== Done ===", flush=True)
        print(f"Best fitness: {global_best.eval_result.fitness:+.4f}", flush=True)
        print(f"Best worst-opponent fitness: {global_best.eval_result.worst_fitness:+.4f}", flush=True)
        print(f"Best weights: {format_weights(global_best.weights)}", flush=True)
        print("Named weights:", flush=True)
        for name, val in zip(WEIGHT_NAMES, global_best.weights):
            print(f"  {name}: {val:.10g}", flush=True)
        print(f"Checkpoint: {checkpoint_path}", flush=True)
        print(f"Analysis: {analysis_path}", flush=True)
        print(f"Best env: {env_file}", flush=True)
    else:
        print("\n=== Done (no best result) ===", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
