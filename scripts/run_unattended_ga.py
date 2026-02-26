#!/usr/bin/env python3
"""
Run ga_optimize_v2.py unattended for many hours/days.

Highlights:
- checkpoint resume
- chunked execution with retry
- live progress files (JSON + text) updated continuously
- dynamic opponent list from file (can be changed while running)
- final markdown analysis (optional with extra validation matches)
- lock file to prevent duplicate runs
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent.parent
GA_SCRIPT = ROOT / "scripts" / "ga_optimize_v2.py"
PYTHON_BIN = ROOT / ".venv" / "bin" / "python"

RE_GEN = re.compile(r"^=== Generation (\d+) ===$")
RE_CANDIDATE = re.compile(
    r"^\[\s*(\d+)/(\d+)\]\s+fit=([+\-]?\d+(?:\.\d+)?)\s+W/L/D/E=(\d+)/(\d+)/(\d+)/(\d+)\s+weights=(.+)$"
)
RE_BEST = re.compile(
    r"^Best gen (\d+): fit=([+\-]?\d+(?:\.\d+)?)\s+W/L/D/E=(\d+)/(\d+)/(\d+)/(\d+)\s+weights=(.+)$"
)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def format_duration(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unattended long-run wrapper for ga_optimize_v2.py")
    parser.add_argument("--hours", type=float, default=72.0, help="Run duration in hours. <= 0 means unlimited.")
    parser.add_argument("--chunk-generations", type=int, default=5, help="Generations per chunk.")
    parser.add_argument("--population-size", type=int, default=16)
    parser.add_argument("--elite-count", type=int, default=4)
    parser.add_argument("--games-per-opponent", type=int, default=2)
    parser.add_argument("--timeout-s", type=int, default=180)
    parser.add_argument("--mutation-sigma", type=float, default=0.08)
    parser.add_argument("--mutation-decay", type=float, default=0.985)
    parser.add_argument("--mutation-floor", type=float, default=0.015)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--retry-delay-s", type=int, default=30)
    parser.add_argument("--max-retries", type=int, default=999999)
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=ROOT / "ga_v2_longrun_checkpoint.json",
        help="Checkpoint path for ga_optimize_v2.py",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=ROOT / "log" / "ga_longrun",
        help="Directory for chunk logs and live status files.",
    )
    parser.add_argument(
        "--stop-file",
        type=Path,
        default=None,
        help="If this file exists, runner stops after current chunk.",
    )
    parser.add_argument(
        "--progress-file",
        type=Path,
        default=None,
        help="Live JSON progress file (default: <log-dir>/progress_live.json)",
    )
    parser.add_argument(
        "--status-file",
        type=Path,
        default=None,
        help="Live text status file (default: <log-dir>/progress_live.txt)",
    )
    parser.add_argument(
        "--analysis-file",
        type=Path,
        default=None,
        help="Final analysis markdown file (default: <log-dir>/final_analysis.md)",
    )
    parser.add_argument(
        "--opponents-file",
        type=Path,
        default=None,
        help=(
            "File with opponent paths (one per line). "
            "Can be edited while runner is active; changes are picked up per chunk."
        ),
    )
    parser.add_argument(
        "--opponent",
        action="append",
        default=None,
        help="Additional static opponent path, can be passed multiple times.",
    )
    parser.add_argument(
        "--final-validation-games",
        type=int,
        default=12,
        help="Validation games per opponent at end of run (0 disables).",
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="Skip Cython rebuild before run starts.",
    )
    return parser.parse_args()


def abs_path(path: Path) -> Path:
    if path.is_absolute():
        return path
    return (ROOT / path).resolve()


def ensure_default_opponents_file(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Opponents for run_unattended_ga.py\n"
        "# One path per line, absolute or relative to repo root.\n"
        "bots/cython_v1/client_cython.py\n"
        "bots/cython_v2/client_cython_baseline.py\n"
        "# /absolute/path/to/third_bot.py\n"
    )


def parse_opponents_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    out: list[str] = []
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        out.append(text)
    return out


def resolve_opponents(static_opponents: list[str], opponents_file: Path) -> tuple[list[str], list[str]]:
    raw = list(static_opponents)
    raw.extend(parse_opponents_file(opponents_file))
    if not raw:
        raw = ["bots/cython_v1/client_cython.py"]

    resolved: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for item in raw:
        p = Path(item)
        if not p.is_absolute():
            p = (ROOT / p).resolve()
        if not p.exists():
            warnings.append(f"Opponent not found, skipped: {item} -> {p}")
            continue
        sp = str(p)
        if sp in seen:
            continue
        seen.add(sp)
        resolved.append(sp)

    return resolved, warnings


def load_checkpoint(checkpoint: Path) -> dict[str, Any] | None:
    if not checkpoint.exists():
        return None
    try:
        return json.loads(checkpoint.read_text())
    except Exception:
        return None


def read_generation(checkpoint: Path) -> int:
    payload = load_checkpoint(checkpoint)
    if not payload:
        return -1
    gen = payload.get("generation")
    if isinstance(gen, int):
        return gen
    try:
        return int(gen)
    except Exception:
        return -1


def extract_best_from_checkpoint(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not payload:
        return None

    history = payload.get("history")
    best_hist = None
    if isinstance(history, list) and history:
        for entry in history:
            if not isinstance(entry, dict):
                continue
            fit = entry.get("best_fitness")
            if fit is None:
                continue
            if best_hist is None or fit > best_hist["best_fitness"]:
                best_hist = entry

    population = payload.get("population")
    best_pop = None
    if isinstance(population, list) and population:
        for item in population:
            if not isinstance(item, dict):
                continue
            fit = item.get("fitness")
            if fit is None:
                continue
            if best_pop is None or fit > best_pop["fitness"]:
                best_pop = item

    if best_pop:
        return {
            "fitness": best_pop.get("fitness"),
            "weights": best_pop.get("weights"),
            "wins": best_pop.get("wins"),
            "losses": best_pop.get("losses"),
            "draws": best_pop.get("draws"),
            "errors": best_pop.get("errors"),
        }
    if best_hist:
        return {
            "fitness": best_hist.get("best_fitness"),
            "weights": best_hist.get("best_weights"),
            "generation": best_hist.get("generation"),
        }
    return None


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=True))
    os.replace(tmp, path)


def render_status_text(progress: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"time: {now_iso()}")
    lines.append(f"state: {progress.get('state')}")
    lines.append(f"pid: {progress.get('pid')}")
    lines.append(f"uptime: {progress.get('uptime_hms')}")
    lines.append(f"checkpoint_generation: {progress.get('checkpoint_generation')}")
    lines.append(f"chunk: {progress.get('chunk_index')} ({progress.get('chunk_range')})")
    lines.append(f"retries: {progress.get('retries')}/{progress.get('max_retries')}")
    lines.append(f"active_opponents: {', '.join(progress.get('active_opponents', []))}")
    lines.append("")

    latest_eval = progress.get("latest_eval")
    if isinstance(latest_eval, dict):
        lines.append(
            "latest_eval: "
            f"[{latest_eval.get('idx')}/{latest_eval.get('total')}] "
            f"fit={latest_eval.get('fitness')} "
            f"W/L/D/E={latest_eval.get('wins')}/{latest_eval.get('losses')}/"
            f"{latest_eval.get('draws')}/{latest_eval.get('errors')}"
        )
        lines.append(f"latest_eval_weights: {latest_eval.get('weights')}")

    latest_best = progress.get("latest_best")
    if isinstance(latest_best, dict):
        lines.append(
            "latest_best: "
            f"gen={latest_best.get('generation')} fit={latest_best.get('fitness')} "
            f"W/L/D/E={latest_best.get('wins')}/{latest_best.get('losses')}/"
            f"{latest_best.get('draws')}/{latest_best.get('errors')}"
        )
        lines.append(f"latest_best_weights: {latest_best.get('weights')}")

    lines.append("")
    lines.append(f"last_line: {progress.get('last_line', '')}")

    val = progress.get("validation")
    if isinstance(val, dict):
        lines.append("")
        lines.append("validation:")
        lines.append(
            f"  state={val.get('state')} "
            f"games_done={val.get('games_done')}/{val.get('games_total')} "
            f"W/L/D/E={val.get('wins')}/{val.get('losses')}/{val.get('draws')}/{val.get('errors')}"
        )

    return "\n".join(lines) + "\n"


def update_progress_files(progress_file: Path, status_file: Path, progress: dict[str, Any]) -> None:
    write_json_atomic(progress_file, progress)
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(render_status_text(progress))


def build_extensions() -> None:
    print("[build] Rebuilding cython_v2 extensions ...", flush=True)
    subprocess.run(
        [str(PYTHON_BIN), "setup.py", "build_ext", "--inplace"],
        cwd=str(ROOT / "bots" / "cython_v2"),
        check=True,
    )


def acquire_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError(f"Lock active: {lock_path} (another runner may be active)")
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def make_ga_command(args: argparse.Namespace, target_generations: int, opponents: list[str]) -> list[str]:
    cmd = [
        str(PYTHON_BIN),
        str(GA_SCRIPT),
        "--resume",
        "--generations",
        str(target_generations),
        "--population-size",
        str(args.population_size),
        "--elite-count",
        str(args.elite_count),
        "--games-per-opponent",
        str(args.games_per_opponent),
        "--timeout-s",
        str(args.timeout_s),
        "--mutation-sigma",
        str(args.mutation_sigma),
        "--mutation-decay",
        str(args.mutation_decay),
        "--mutation-floor",
        str(args.mutation_floor),
        "--seed",
        str(args.seed),
        "--checkpoint",
        str(args.checkpoint),
    ]
    for opp in opponents:
        cmd.extend(["--opponent", opp])
    return cmd


def run_chunk(cmd: list[str], log_file: Path, on_line: Callable[[str], None] | None = None) -> int:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a") as log_handle:
        log_handle.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        log_handle.write(f"CMD: {' '.join(cmd)}\n\n")
        log_handle.flush()

        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )

        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            log_handle.write(line)
            if on_line:
                on_line(line)
        proc.stdout.close()
        return proc.wait()


def parse_best_weights(progress: dict[str, Any], checkpoint: Path) -> list[float] | None:
    latest_best = progress.get("latest_best")
    if isinstance(latest_best, dict) and isinstance(latest_best.get("weights"), str):
        raw = latest_best["weights"]
        try:
            return [float(x.strip()) for x in raw.split(",")]
        except Exception:
            pass

    payload = load_checkpoint(checkpoint)
    best = extract_best_from_checkpoint(payload)
    if isinstance(best, dict):
        weights = best.get("weights")
        if isinstance(weights, list) and len(weights) == 5:
            try:
                return [float(x) for x in weights]
            except Exception:
                pass
        if isinstance(weights, tuple) and len(weights) == 5:
            try:
                return [float(x) for x in weights]
            except Exception:
                pass
    return None


def run_final_validation(
    weights: list[float],
    opponents: list[str],
    games_per_opponent: int,
    timeout_s: int,
    progress: dict[str, Any],
    progress_file: Path,
    status_file: Path,
) -> dict[str, Any]:
    import ga_optimize_v2 as ga

    if games_per_opponent <= 0:
        return {"enabled": False}
    if games_per_opponent % 2 != 0:
        games_per_opponent += 1

    progress["validation"] = {
        "state": "running",
        "games_total": games_per_opponent * len(opponents),
        "games_done": 0,
        "wins": 0,
        "losses": 0,
        "draws": 0,
        "errors": 0,
    }
    update_progress_files(progress_file, status_file, progress)

    env_new = {"CYTHON_V2_EVAL_PARAMS": ga.format_weights(tuple(weights))}
    game_id_base = int(time.time()) % 1_000_000 + 2_000_000

    total_w = total_l = total_d = total_e = 0
    per_opp: list[dict[str, Any]] = []
    game_no = 0
    for opp_idx, opp in enumerate(opponents):
        opp_path = Path(opp)
        w = l = d = e = 0
        for i in range(games_per_opponent):
            if i % 2 == 0:
                bot_one, bot_two = ga.NEW_BOT, opp_path
                new_side = "ONE"
            else:
                bot_one, bot_two = opp_path, ga.NEW_BOT
                new_side = "TWO"

            env_one = env_new if bot_one == ga.NEW_BOT else None
            env_two = env_new if bot_two == ga.NEW_BOT else None

            res = ga.run_game(
                bot_one=bot_one,
                bot_two=bot_two,
                game_id=game_id_base + opp_idx * 10000 + game_no,
                env_one=env_one,
                env_two=env_two,
                timeout_s=timeout_s,
                base_port=20000,
            )
            game_no += 1

            winner = res.get("winner")
            if winner == new_side:
                w += 1
                total_w += 1
            elif winner in ("ONE", "TWO"):
                l += 1
                total_l += 1
            elif winner == "DRAW":
                d += 1
                total_d += 1
            else:
                e += 1
                total_e += 1

            val = progress.get("validation", {})
            if isinstance(val, dict):
                val["games_done"] = int(val.get("games_done", 0)) + 1
                val["wins"] = total_w
                val["losses"] = total_l
                val["draws"] = total_d
                val["errors"] = total_e
            update_progress_files(progress_file, status_file, progress)

        n = w + l + d + e
        points = w + 0.5 * d - e
        fit = points / max(1, n)
        per_opp.append(
            {
                "opponent": str(opp_path),
                "games": n,
                "wins": w,
                "losses": l,
                "draws": d,
                "errors": e,
                "fitness": fit,
            }
        )

    n_total = total_w + total_l + total_d + total_e
    points_total = total_w + 0.5 * total_d - total_e
    fit_total = points_total / max(1, n_total)

    summary = {
        "enabled": True,
        "games_per_opponent": games_per_opponent,
        "total_games": n_total,
        "wins": total_w,
        "losses": total_l,
        "draws": total_d,
        "errors": total_e,
        "fitness": fit_total,
        "win_rate": total_w / max(1, n_total),
        "per_opponent": per_opp,
    }

    val = progress.get("validation")
    if isinstance(val, dict):
        val["state"] = "done"
        val["games_done"] = n_total
        val["wins"] = total_w
        val["losses"] = total_l
        val["draws"] = total_d
        val["errors"] = total_e
    update_progress_files(progress_file, status_file, progress)
    return summary


def write_final_analysis(
    analysis_file: Path,
    args: argparse.Namespace,
    start_ts: float,
    end_ts: float,
    progress: dict[str, Any],
    final_validation: dict[str, Any] | None,
) -> None:
    payload = load_checkpoint(args.checkpoint)
    generation = read_generation(args.checkpoint)
    best = extract_best_from_checkpoint(payload)

    history = []
    if isinstance(payload, dict):
        h = payload.get("history")
        if isinstance(h, list):
            history = [x for x in h if isinstance(x, dict)]

    lines: list[str] = []
    lines.append("# GA Long-Run Analysis")
    lines.append("")
    lines.append(f"- Generated: {now_iso()}")
    lines.append(f"- Runtime: {format_duration(end_ts - start_ts)}")
    lines.append(f"- Start: {datetime.fromtimestamp(start_ts).isoformat(timespec='seconds')}")
    lines.append(f"- End: {datetime.fromtimestamp(end_ts).isoformat(timespec='seconds')}")
    lines.append("")
    lines.append("## Configuration")
    lines.append("")
    lines.append(f"- Chunk generations: {args.chunk_generations}")
    lines.append(f"- Population: {args.population_size}")
    lines.append(f"- Elites: {args.elite_count}")
    lines.append(f"- Games per opponent: {args.games_per_opponent}")
    lines.append(f"- Timeout per game: {args.timeout_s}s")
    lines.append(f"- Mutation sigma/decay/floor: {args.mutation_sigma}/{args.mutation_decay}/{args.mutation_floor}")
    lines.append(f"- Seed: {args.seed}")
    lines.append(f"- Opponents file: {args.opponents_file}")
    lines.append(f"- Active opponents (last chunk): {', '.join(progress.get('active_opponents', []))}")
    lines.append("")
    lines.append("## Training Result")
    lines.append("")
    lines.append(f"- Last checkpoint generation: {generation}")
    lines.append(f"- Chunks completed: {progress.get('completed_chunks', 0)}")
    if best:
        lines.append(f"- Best fitness: {best.get('fitness')}")
        lines.append(f"- Best weights: {best.get('weights')}")
        lines.append(
            "- Best W/L/D/E: "
            f"{best.get('wins', 'n/a')}/{best.get('losses', 'n/a')}/"
            f"{best.get('draws', 'n/a')}/{best.get('errors', 'n/a')}"
        )
    else:
        lines.append("- Best fitness: n/a")

    if history:
        first_fit = history[0].get("best_fitness")
        last_fit = history[-1].get("best_fitness")
        lines.append(f"- First recorded best fitness: {first_fit}")
        lines.append(f"- Last recorded best fitness: {last_fit}")

    lines.append("")
    lines.append("## History (Top 10 by best_fitness)")
    lines.append("")
    lines.append("| generation | best_fitness | mean_fitness | best_weights |")
    lines.append("|---:|---:|---:|---|")
    top = sorted(
        history,
        key=lambda x: x.get("best_fitness", -999.0) if x.get("best_fitness") is not None else -999.0,
        reverse=True,
    )[:10]
    for item in top:
        lines.append(
            f"| {item.get('generation')} | {item.get('best_fitness')} | "
            f"{item.get('mean_fitness')} | {item.get('best_weights')} |"
        )
    if not top:
        lines.append("| n/a | n/a | n/a | n/a |")

    lines.append("")
    lines.append("## Final Validation")
    lines.append("")
    if final_validation and final_validation.get("enabled"):
        lines.append(f"- Games per opponent: {final_validation.get('games_per_opponent')}")
        lines.append(
            f"- Total W/L/D/E: {final_validation.get('wins')}/{final_validation.get('losses')}/"
            f"{final_validation.get('draws')}/{final_validation.get('errors')}"
        )
        lines.append(f"- Total fitness: {final_validation.get('fitness')}")
        lines.append(f"- Total win rate: {final_validation.get('win_rate')}")
        lines.append("")
        lines.append("| opponent | games | wins | losses | draws | errors | fitness |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        per_opp = final_validation.get("per_opponent", [])
        for item in per_opp:
            lines.append(
                f"| {item.get('opponent')} | {item.get('games')} | {item.get('wins')} | "
                f"{item.get('losses')} | {item.get('draws')} | {item.get('errors')} | {item.get('fitness')} |"
            )
    else:
        lines.append("- Validation disabled.")

    analysis_file.parent.mkdir(parents=True, exist_ok=True)
    analysis_file.write_text("\n".join(lines) + "\n")


def main() -> int:
    args = parse_args()

    if not PYTHON_BIN.exists():
        print(f"Fehler: Python nicht gefunden: {PYTHON_BIN}", file=sys.stderr)
        return 1
    if not GA_SCRIPT.exists():
        print(f"Fehler: GA-Skript nicht gefunden: {GA_SCRIPT}", file=sys.stderr)
        return 1
    if args.chunk_generations <= 0:
        print("Fehler: --chunk-generations muss > 0 sein", file=sys.stderr)
        return 1
    if args.population_size <= 0:
        print("Fehler: --population-size muss > 0 sein", file=sys.stderr)
        return 1

    args.checkpoint = abs_path(args.checkpoint)
    args.log_dir = abs_path(args.log_dir)
    args.log_dir.mkdir(parents=True, exist_ok=True)

    args.progress_file = abs_path(args.progress_file) if args.progress_file else (args.log_dir / "progress_live.json")
    args.status_file = abs_path(args.status_file) if args.status_file else (args.log_dir / "progress_live.txt")
    args.analysis_file = abs_path(args.analysis_file) if args.analysis_file else (args.log_dir / "final_analysis.md")
    args.opponents_file = abs_path(args.opponents_file) if args.opponents_file else (args.log_dir / "opponents.txt")

    static_opponents = list(args.opponent or [])
    ensure_default_opponents_file(args.opponents_file)

    stop_file = abs_path(args.stop_file) if args.stop_file else (args.log_dir / "STOP")

    lock_path = args.checkpoint.with_suffix(args.checkpoint.suffix + ".lock")
    try:
        lock_handle = acquire_lock(lock_path)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    start_ts = time.time()
    end_ts = None if args.hours <= 0 else start_ts + args.hours * 3600.0

    progress: dict[str, Any] = {
        "state": "starting",
        "pid": os.getpid(),
        "started_at": now_iso(),
        "uptime_hms": "00:00:00",
        "checkpoint": str(args.checkpoint),
        "checkpoint_generation": read_generation(args.checkpoint),
        "chunk_index": 0,
        "chunk_range": "-",
        "retries": 0,
        "max_retries": args.max_retries,
        "active_opponents": [],
        "last_line": "",
        "latest_eval": None,
        "latest_best": None,
        "completed_chunks": 0,
        "progress_file": str(args.progress_file),
        "status_file": str(args.status_file),
        "analysis_file": str(args.analysis_file),
        "opponents_file": str(args.opponents_file),
    }
    update_progress_files(args.progress_file, args.status_file, progress)

    final_validation: dict[str, Any] | None = None

    try:
        if not args.no_build:
            build_extensions()

        retries = 0
        chunk_no = 0

        print(f"[runner] Start: {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
        if end_ts is None:
            print("[runner] Laufzeit: unbegrenzt", flush=True)
        else:
            print(f"[runner] Laufzeit bis: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(end_ts))}", flush=True)
        print(f"[runner] Checkpoint: {args.checkpoint}", flush=True)
        print(f"[runner] Stop-Datei: {stop_file}", flush=True)
        print(f"[runner] Opponents-Datei: {args.opponents_file}", flush=True)

        while True:
            now = time.time()
            progress["uptime_hms"] = format_duration(now - start_ts)
            progress["checkpoint_generation"] = read_generation(args.checkpoint)
            update_progress_files(args.progress_file, args.status_file, progress)

            if end_ts is not None and now >= end_ts:
                print("[runner] Ziel-Laufzeit erreicht.", flush=True)
                break
            if stop_file.exists():
                print(f"[runner] Stop-Datei gefunden: {stop_file}", flush=True)
                break

            opponents, warnings = resolve_opponents(static_opponents, args.opponents_file)
            if warnings:
                for w in warnings:
                    print(f"[runner] WARN: {w}", flush=True)
            if not opponents:
                print("[runner] Keine validen Gegner vorhanden. Warte 30s ...", flush=True)
                time.sleep(30)
                continue

            current_gen = read_generation(args.checkpoint)
            target_generations = current_gen + 1 + args.chunk_generations
            if target_generations < args.chunk_generations:
                target_generations = args.chunk_generations

            chunk_no += 1
            log_file = args.log_dir / (
                f"chunk_{chunk_no:04d}_from_{current_gen + 1}_to_{target_generations - 1}_"
                f"{time.strftime('%Y%m%d_%H%M%S')}.log"
            )

            cmd = make_ga_command(args, target_generations, opponents)
            print(
                f"[runner] Chunk {chunk_no}: Generationen {current_gen + 1}..{target_generations - 1}",
                flush=True,
            )

            progress["state"] = "running_chunk"
            progress["chunk_index"] = chunk_no
            progress["chunk_range"] = f"{current_gen + 1}..{target_generations - 1}"
            progress["chunk_log_file"] = str(log_file)
            progress["active_opponents"] = opponents
            progress["retries"] = retries
            progress["uptime_hms"] = format_duration(time.time() - start_ts)
            update_progress_files(args.progress_file, args.status_file, progress)

            last_flush = 0.0

            def on_line(line: str) -> None:
                nonlocal last_flush
                text = line.strip()
                if not text:
                    return
                progress["last_line"] = text

                m = RE_GEN.match(text)
                if m:
                    progress["current_generation"] = int(m.group(1))

                m = RE_CANDIDATE.match(text)
                if m:
                    progress["latest_eval"] = {
                        "idx": int(m.group(1)),
                        "total": int(m.group(2)),
                        "fitness": float(m.group(3)),
                        "wins": int(m.group(4)),
                        "losses": int(m.group(5)),
                        "draws": int(m.group(6)),
                        "errors": int(m.group(7)),
                        "weights": m.group(8),
                    }

                m = RE_BEST.match(text)
                if m:
                    progress["latest_best"] = {
                        "generation": int(m.group(1)),
                        "fitness": float(m.group(2)),
                        "wins": int(m.group(3)),
                        "losses": int(m.group(4)),
                        "draws": int(m.group(5)),
                        "errors": int(m.group(6)),
                        "weights": m.group(7),
                    }

                now_inner = time.time()
                if now_inner - last_flush >= 1.0:
                    progress["uptime_hms"] = format_duration(now_inner - start_ts)
                    progress["checkpoint_generation"] = read_generation(args.checkpoint)
                    update_progress_files(args.progress_file, args.status_file, progress)
                    last_flush = now_inner

            rc = run_chunk(cmd, log_file, on_line=on_line)

            progress["last_chunk_rc"] = rc
            progress["checkpoint_generation"] = read_generation(args.checkpoint)
            progress["uptime_hms"] = format_duration(time.time() - start_ts)
            update_progress_files(args.progress_file, args.status_file, progress)

            if rc == 0:
                retries = 0
                progress["completed_chunks"] = int(progress.get("completed_chunks", 0)) + 1
                print(
                    f"[runner] Chunk {chunk_no} fertig. Checkpoint-Generation: {progress['checkpoint_generation']}",
                    flush=True,
                )
                continue

            if rc in (130, 143):
                print(f"[runner] Abbruchsignal erhalten (rc={rc}).", flush=True)
                break

            retries += 1
            progress["retries"] = retries
            print(f"[runner] Chunk fehlgeschlagen (rc={rc}), Retry {retries}/{args.max_retries}", flush=True)
            if retries > args.max_retries:
                print("[runner] Max retries erreicht.", flush=True)
                break

            sleep_s = max(1, args.retry_delay_s)
            progress["state"] = "waiting_retry"
            progress["uptime_hms"] = format_duration(time.time() - start_ts)
            update_progress_files(args.progress_file, args.status_file, progress)
            print(f"[runner] Warte {sleep_s}s vor erneutem Versuch ...", flush=True)
            time.sleep(sleep_s)

        progress["state"] = "finishing"
        progress["uptime_hms"] = format_duration(time.time() - start_ts)
        progress["checkpoint_generation"] = read_generation(args.checkpoint)
        update_progress_files(args.progress_file, args.status_file, progress)

        best_weights = parse_best_weights(progress, args.checkpoint)
        if best_weights and args.final_validation_games > 0:
            print("[runner] Starte finale Validierung ...", flush=True)
            opponents, warnings = resolve_opponents(static_opponents, args.opponents_file)
            for w in warnings:
                print(f"[runner] WARN: {w}", flush=True)
            if opponents:
                progress["state"] = "validating"
                progress["active_opponents"] = opponents
                update_progress_files(args.progress_file, args.status_file, progress)
                final_validation = run_final_validation(
                    weights=best_weights,
                    opponents=opponents,
                    games_per_opponent=args.final_validation_games,
                    timeout_s=args.timeout_s,
                    progress=progress,
                    progress_file=args.progress_file,
                    status_file=args.status_file,
                )

        end_real = time.time()
        write_final_analysis(
            analysis_file=args.analysis_file,
            args=args,
            start_ts=start_ts,
            end_ts=end_real,
            progress=progress,
            final_validation=final_validation,
        )

        progress["state"] = "finished"
        progress["finished_at"] = now_iso()
        progress["uptime_hms"] = format_duration(end_real - start_ts)
        progress["checkpoint_generation"] = read_generation(args.checkpoint)
        progress["analysis_file"] = str(args.analysis_file)
        update_progress_files(args.progress_file, args.status_file, progress)

        print(f"[runner] Ende. Letzte Checkpoint-Generation: {progress['checkpoint_generation']}", flush=True)
        print(f"[runner] Analyse geschrieben: {args.analysis_file}", flush=True)
        return 0

    finally:
        try:
            lock_handle.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
