#!/usr/bin/env python3
"""
Genetic optimization for cython_v2 evaluation weights.

This script optimizes the runtime-settable weights in
`cython_v2/cython_core/evaluate.pyx` by playing head-to-head matches.
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
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).parent.parent.resolve()
SERVER_JAR = ROOT / "server" / "server.jar"
PYTHON_PATH = ROOT / ".venv" / "bin" / "python"
NEW_BOT = ROOT / "bots" / "cython_v2" / "client_cython.py"
DEFAULT_OPPONENTS = (
    "bots/cython_v1/client_cython.py",
    "bots/python/client_optimized.py",
)

WEIGHT_NAMES = (
    "best_swarm",
    "swarm_count",
    "material",
    "isolated",
    "distance",
)
BASE_WEIGHTS = (17.74, 3.0, 2.0, 4.0, 0.63)
WEIGHT_BOUNDS = (
    (5.0, 45.0),   # best_swarm
    (0.1, 12.0),   # swarm_count
    (0.1, 10.0),   # material
    (0.1, 12.0),   # isolated
    (0.05, 3.0),   # distance
)


@dataclass
class Genome:
    weights: tuple[float, float, float, float, float]
    fitness: float | None = None
    wins: int = 0
    losses: int = 0
    draws: int = 0
    errors: int = 0


def clamp_weight(idx: int, value: float) -> float:
    lo, hi = WEIGHT_BOUNDS[idx]
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def format_weights(weights: tuple[float, float, float, float, float]) -> str:
    return ",".join(f"{w:.10g}" for w in weights)


def parse_winner_from_server_log(content: str) -> str:
    if "LOST_CONNECTION" in content:
        if "ONE hat das Spiel verlassen" in content:
            return "TWO"
        if "TWO hat das Spiel verlassen" in content:
            return "ONE"

    score_match = re.search(r"scores=\[\[Siegpunkte=(\d+).*?\], \[Siegpunkte=(\d+)", content)
    if score_match:
        s1, s2 = int(score_match.group(1)), int(score_match.group(2))
        if s1 > s2:
            return "ONE"
        if s2 > s1:
            return "TWO"
        return "DRAW"

    if "winner=ONE" in content or "winner=Team One" in content:
        return "ONE"
    if "winner=TWO" in content or "winner=Team Two" in content:
        return "TWO"
    if "DRAW" in content or "draw" in content.lower():
        return "DRAW"

    return "UNKNOWN"


def find_free_port(start: int) -> int:
    for port in range(start, start + 400):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("localhost", port)) != 0:
                return port
    return start


def kill_process_group(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        pass
    try:
        proc.wait(timeout=3)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass


def run_game(
    bot_one: Path,
    bot_two: Path,
    game_id: int,
    env_one: dict[str, str] | None = None,
    env_two: dict[str, str] | None = None,
    timeout_s: int = 300,
    base_port: int = 16000,
) -> dict:
    """
    Run one game and return:
      {"winner": "ONE"/"TWO"/"DRAW"/"UNKNOWN"/"ERROR", "bot1_crash": bool, "bot2_crash": bool}
    """
    port_seed = base_port + ((game_id * 17) % 10000)
    port = find_free_port(port_seed)

    result = {"winner": "UNKNOWN", "bot1_crash": False, "bot2_crash": False}
    server_log = Path(f"/tmp/ga_server_{game_id}.log")
    bot1_log = Path(f"/tmp/ga_bot1_{game_id}.log")
    bot2_log = Path(f"/tmp/ga_bot2_{game_id}.log")

    server_proc = None
    bot1_proc = None
    bot2_proc = None

    env1 = os.environ.copy()
    env2 = os.environ.copy()
    env1["PYTHONUNBUFFERED"] = "1"
    env2["PYTHONUNBUFFERED"] = "1"
    if env_one:
        env1.update(env_one)
    if env_two:
        env2.update(env_two)

    try:
        with open(server_log, "w") as srv:
            server_proc = subprocess.Popen(
                ["java", "-jar", str(SERVER_JAR), "--port", str(port)],
                cwd=str(ROOT),
                stdout=srv,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid,
            )
        time.sleep(2.3)

        with open(bot1_log, "w") as b1:
            bot1_proc = subprocess.Popen(
                [str(PYTHON_PATH), "-u", str(bot_one), "--port", str(port)],
                cwd=str(ROOT),
                stdout=b1,
                stderr=subprocess.STDOUT,
                env=env1,
                preexec_fn=os.setsid,
            )
        time.sleep(0.4)

        with open(bot2_log, "w") as b2:
            bot2_proc = subprocess.Popen(
                [str(PYTHON_PATH), "-u", str(bot_two), "--port", str(port)],
                cwd=str(ROOT),
                stdout=b2,
                stderr=subprocess.STDOUT,
                env=env2,
                preexec_fn=os.setsid,
            )

        start = time.time()
        while time.time() - start < timeout_s:
            if bot1_proc.poll() is not None and bot2_proc.poll() is not None:
                break
            time.sleep(1)
        time.sleep(0.8)

        if bot1_proc.poll() is not None and bot1_proc.returncode != 0:
            result["bot1_crash"] = True
        if bot2_proc.poll() is not None and bot2_proc.returncode != 0:
            result["bot2_crash"] = True

        if server_log.exists():
            result["winner"] = parse_winner_from_server_log(server_log.read_text(errors="ignore"))
        else:
            result["winner"] = "UNKNOWN"

    except Exception:
        result["winner"] = "ERROR"
    finally:
        kill_process_group(bot1_proc)
        kill_process_group(bot2_proc)
        kill_process_group(server_proc)
        server_log.unlink(missing_ok=True)
        bot1_log.unlink(missing_ok=True)
        bot2_log.unlink(missing_ok=True)

    return result


def evaluate_genome(
    genome: Genome,
    opponents: list[Path],
    games_per_opponent: int,
    game_id_base: int,
    timeout_s: int,
) -> Genome:
    """Evaluate one genome in-place and return it."""
    env_new = {"CYTHON_V2_EVAL_PARAMS": format_weights(genome.weights)}

    wins = losses = draws = errors = 0
    game_no = 0
    for opp in opponents:
        for i in range(games_per_opponent):
            # Side switch for fairness.
            if i % 2 == 0:
                bot_one, bot_two = NEW_BOT, opp
                new_side = "ONE"
            else:
                bot_one, bot_two = opp, NEW_BOT
                new_side = "TWO"

            env_one = env_new if bot_one == NEW_BOT else None
            env_two = env_new if bot_two == NEW_BOT else None
            res = run_game(
                bot_one=bot_one,
                bot_two=bot_two,
                game_id=game_id_base + game_no,
                env_one=env_one,
                env_two=env_two,
                timeout_s=timeout_s,
            )
            game_no += 1

            winner = res["winner"]
            if winner == new_side:
                wins += 1
            elif winner in ("ONE", "TWO"):
                losses += 1
            elif winner == "DRAW":
                draws += 1
            else:
                errors += 1

    total = wins + losses + draws + errors
    points = wins + 0.5 * draws - 1.0 * errors
    fitness = points / max(1, total)

    genome.fitness = fitness
    genome.wins = wins
    genome.losses = losses
    genome.draws = draws
    genome.errors = errors
    return genome


def tournament_select(pop: list[Genome], rng: random.Random, k: int = 3) -> Genome:
    sample = rng.sample(pop, min(k, len(pop)))
    sample.sort(key=lambda g: g.fitness if g.fitness is not None else -999.0, reverse=True)
    return sample[0]


def crossover(
    a: tuple[float, float, float, float, float],
    b: tuple[float, float, float, float, float],
    rng: random.Random,
) -> tuple[float, float, float, float, float]:
    child = []
    for i, (av, bv) in enumerate(zip(a, b)):
        t = rng.random()
        val = av * t + bv * (1.0 - t)
        child.append(clamp_weight(i, val))
    return tuple(child)  # type: ignore[return-value]


def mutate(
    weights: tuple[float, float, float, float, float],
    sigma: float,
    rng: random.Random,
) -> tuple[float, float, float, float, float]:
    out = []
    for i, w in enumerate(weights):
        lo, hi = WEIGHT_BOUNDS[i]
        span = hi - lo
        # Mutate each gene with 70% probability.
        if rng.random() < 0.7:
            w = w + rng.gauss(0.0, sigma * span)
        out.append(clamp_weight(i, w))
    return tuple(out)  # type: ignore[return-value]


def save_checkpoint(
    checkpoint_path: Path,
    generation: int,
    sigma: float,
    population: list[Genome],
    history: list[dict],
    rng_state: object,
) -> None:
    payload = {
        "generation": generation,
        "sigma": sigma,
        "population": [asdict(g) for g in population],
        "history": history,
        "rng_state": repr(rng_state),
    }
    checkpoint_path.write_text(json.dumps(payload, indent=2))


def load_checkpoint(path: Path) -> dict:
    return json.loads(path.read_text())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GA optimizer for cython_v2 eval weights")
    parser.add_argument("--population-size", type=int, default=16)
    parser.add_argument("--generations", type=int, default=50)
    parser.add_argument("--elite-count", type=int, default=4)
    parser.add_argument("--games-per-opponent", type=int, default=4)
    parser.add_argument("--timeout-s", type=int, default=300)
    parser.add_argument("--mutation-sigma", type=float, default=0.08)
    parser.add_argument("--mutation-decay", type=float, default=0.985)
    parser.add_argument("--mutation-floor", type=float, default=0.015)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--checkpoint", type=Path, default=ROOT / "ga_v2_checkpoint.json")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--opponent",
        action="append",
        default=None,
        help=(
            "Relative path to opponent bot script; can be passed multiple times. "
            "If omitted, built-in default opponents are used."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not SERVER_JAR.exists():
        print(f"Fehler: Server nicht gefunden: {SERVER_JAR}")
        return 1
    if not PYTHON_PATH.exists():
        print(f"Fehler: Python nicht gefunden: {PYTHON_PATH}")
        return 1

    opponent_args = list(args.opponent) if args.opponent else list(DEFAULT_OPPONENTS)
    opponents = [Path(op).resolve() if Path(op).is_absolute() else (ROOT / op).resolve() for op in opponent_args]
    for opp in opponents:
        if not opp.exists():
            print(f"Fehler: Opponent nicht gefunden: {opp}")
            return 1

    rng = random.Random(args.seed)
    history: list[dict] = []
    sigma = args.mutation_sigma
    start_gen = 0
    population: list[Genome]

    if args.resume and args.checkpoint.exists():
        cp = load_checkpoint(args.checkpoint)
        start_gen = int(cp["generation"]) + 1
        sigma = float(cp["sigma"])
        population = [
            Genome(
                weights=tuple(item["weights"]),
                fitness=item.get("fitness"),
                wins=item.get("wins", 0),
                losses=item.get("losses", 0),
                draws=item.get("draws", 0),
                errors=item.get("errors", 0),
            )
            for item in cp["population"]
        ]
        try:
            rng_state = ast.literal_eval(cp.get("rng_state", ""))
            if isinstance(rng_state, tuple) and len(rng_state) == 3:
                rng.setstate(rng_state)
        except Exception:
            pass
        history = list(cp.get("history", []))
        print(
            f"Checkpoint geladen: generation={start_gen}, population={len(population)}, sigma={sigma:.4f}",
            flush=True,
        )

        # Chunked runs store the evaluated generation in the checkpoint.
        # On resume we need to derive the next generation state first.
        if population and all(g.fitness is not None for g in population):
            population.sort(key=lambda g: g.fitness if g.fitness is not None else -999.0, reverse=True)
            next_pop: list[Genome] = []
            for elite in population[: args.elite_count]:
                next_pop.append(Genome(weights=elite.weights))

            while len(next_pop) < args.population_size:
                p1 = tournament_select(population, rng)
                p2 = tournament_select(population, rng)
                child = crossover(p1.weights, p2.weights, rng)
                child = mutate(child, sigma, rng)
                next_pop.append(Genome(weights=child))

            population = next_pop
            sigma = max(args.mutation_floor, sigma * args.mutation_decay)
            print(
                "Resume: next generation population created from checkpoint parents.",
                flush=True,
            )
    else:
        population = [Genome(weights=BASE_WEIGHTS)]
        while len(population) < args.population_size:
            mutated = mutate(BASE_WEIGHTS, sigma, rng)
            population.append(Genome(weights=mutated))

    try:
        for gen in range(start_gen, args.generations):
            print(f"\n=== Generation {gen} ===", flush=True)
            print(f"Sigma={sigma:.4f}", flush=True)

            # Evaluate all genomes.
            for i, genome in enumerate(population):
                if genome.fitness is not None:
                    continue
                game_id_base = gen * 100000 + i * 1000
                evaluate_genome(
                    genome=genome,
                    opponents=opponents,
                    games_per_opponent=args.games_per_opponent,
                    game_id_base=game_id_base,
                    timeout_s=args.timeout_s,
                )
                print(
                    f"  [{i+1:>2}/{len(population)}] "
                    f"fit={genome.fitness:+.4f} "
                    f"W/L/D/E={genome.wins}/{genome.losses}/{genome.draws}/{genome.errors} "
                    f"weights={format_weights(genome.weights)}"
                    ,
                    flush=True
                )

            population.sort(key=lambda g: g.fitness if g.fitness is not None else -999.0, reverse=True)
            best = population[0]
            mean_fit = sum(g.fitness or 0.0 for g in population) / max(1, len(population))
            history_item = {
                "generation": gen,
                "best_fitness": best.fitness,
                "best_weights": best.weights,
                "mean_fitness": mean_fit,
            }
            history.append(history_item)

            print(
                f"Best gen {gen}: fit={best.fitness:+.4f} "
                f"W/L/D/E={best.wins}/{best.losses}/{best.draws}/{best.errors} "
                f"weights={format_weights(best.weights)}"
                ,
                flush=True
            )

            save_checkpoint(
                checkpoint_path=args.checkpoint,
                generation=gen,
                sigma=sigma,
                population=population,
                history=history,
                rng_state=rng.getstate(),
            )
            print(f"Checkpoint gespeichert: {args.checkpoint}", flush=True)

            if gen == args.generations - 1:
                break

            # Create next generation.
            next_pop: list[Genome] = []
            for elite in population[: args.elite_count]:
                next_pop.append(Genome(weights=elite.weights))

            while len(next_pop) < args.population_size:
                p1 = tournament_select(population, rng)
                p2 = tournament_select(population, rng)
                child = crossover(p1.weights, p2.weights, rng)
                child = mutate(child, sigma, rng)
                next_pop.append(Genome(weights=child))

            population = next_pop
            sigma = max(args.mutation_floor, sigma * args.mutation_decay)

    except KeyboardInterrupt:
        print("\nAbbruch durch Benutzer. Letzter Zustand ist im Checkpoint gespeichert.", flush=True)
        return 130

    population.sort(key=lambda g: g.fitness if g.fitness is not None else -999.0, reverse=True)
    print("\n=== Fertig ===", flush=True)
    print(f"Best weights: {format_weights(population[0].weights)}", flush=True)
    print(f"Best fitness: {population[0].fitness:+.4f}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
