"""
Unified duel/tournament TUI runner.

Start:
    python ./duel.py
"""

from __future__ import annotations

import ast
import curses
import glob
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
try:
    import readline
except Exception:  # pragma: no cover - platform dependent
    readline = None

# ========================= CONFIG =========================

SERVER_JAR = Path("server/server.jar")
SAVE_FILE = Path("duel_state.json")
RESULTS_DIR = Path("results")
RUNS_DIR = RESULTS_DIR / "runs"
LATEST_DUEL_RESULTS_FILE = RESULTS_DIR / "duel_results.json"
LATEST_TOURNAMENT_RESULTS_FILE = RESULTS_DIR / "tournament_results.json"
CUSTOM_BOTS_FILE = Path("custom_bot_paths.json")

STATE_VERSION = 1
BASE_PORT = 16000
PORT_STEP = 10
MAX_GAME_TIME = 300
ELO_K = 32

RESULT_WIN_ONE = 1
RESULT_WIN_TWO = 2
RESULT_DRAW = 0

TURN_TIME_LIMIT = 2.0  # seconds per turn

ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
SCORES_RE = re.compile(
    r"scores=\[\s*Spieler\s*1\[Siegpunkte=(\d+),\s*Schwarmgr(?:öße|oesse)=(\d+)\],\s*"
    r"Spieler\s*2\[Siegpunkte=(\d+),\s*Schwarmgr(?:öße|oesse)=(\d+)\]\s*\]",
    re.IGNORECASE,
)
# Patterns to detect turn time from bot logs
# Matches: "total X.XXXs" or "calc X.XXXs" or "after X.XXX seconds"
TURN_TIME_RE = re.compile(r"(?:total|calc|after)\s+(\d+\.?\d*)\s*s(?:econds?)?", re.IGNORECASE)

PORT_RESERVE_LOCK = threading.Lock()
RESERVED_PORTS: set[int] = set()
SKIP_SCAN_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".idea",
    ".vscode",
    "node_modules",
    "results",
}
MAX_BOT_SCAN_FILE_SIZE = 1_500_000  # bytes


# ========================= DATA =========================


@dataclass
class BotSpec:
    path: str
    name: str
    python_exec: str
    is_custom: bool = False
    is_discovered: bool = True


@dataclass
class BotStats:
    name: str
    path: str
    elo: float = 1000.0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    errors: int = 0
    timeouts: int = 0

    @property
    def games(self) -> int:
        return self.wins + self.losses + self.draws

    @property
    def score(self) -> float:
        if self.games == 0:
            return 0.0
        return (self.wins + 0.5 * self.draws) / self.games

    @property
    def winrate(self) -> str:
        if self.games == 0:
            return "0.0%"
        return f"{100 * self.wins / self.games:.1f}%"


@dataclass
class RunConfig:
    mode: str
    games_per_pair: int
    timeout_sec: int = MAX_GAME_TIME
    base_port: int = BASE_PORT
    parallel_workers: int = 1


@dataclass
class MatchSpec:
    game_idx: int
    bot_one_idx: int
    bot_two_idx: int


@dataclass
class GameRecord:
    game_idx: int
    bot_one: str
    bot_two: str
    result: int | None
    reason: str
    duration_sec: float
    server_log_path: str | None = None
    bot_one_log_path: str | None = None
    bot_two_log_path: str | None = None


@dataclass
class RunState:
    version: int
    mode: str
    run_id: str
    config: RunConfig
    bots: list[BotStats]
    schedule: list[MatchSpec]
    next_game_idx: int
    started_at: str
    updated_at: str
    completed: bool = False
    game_records: list[GameRecord] = field(default_factory=list)


# ========================= TIME / JSON =========================


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_json_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp_path, path)


def make_run_id(mode: str, started_at: str | None = None) -> str:
    raw_ts = started_at or utc_now_iso()
    safe_ts = re.sub(r"[^0-9A-Za-z_-]+", "_", raw_ts).strip("_")
    return f"{safe_ts}_{mode}"


# ========================= STATE SERIALIZATION =========================


def state_to_dict(state: RunState) -> dict[str, Any]:
    return {
        "version": state.version,
        "mode": state.mode,
        "run_id": state.run_id,
        "config": asdict(state.config),
        "bots": [asdict(bot) for bot in state.bots],
        "schedule": [asdict(match) for match in state.schedule],
        "next_game_idx": state.next_game_idx,
        "game_records": [asdict(record) for record in state.game_records],
        "started_at": state.started_at,
        "updated_at": state.updated_at,
        "completed": state.completed,
    }


def save_state(state: RunState) -> None:
    state.updated_at = utc_now_iso()
    save_json_atomic(SAVE_FILE, state_to_dict(state))


def parse_run_state(data: dict[str, Any]) -> RunState:
    required_keys = {
        "version",
        "mode",
        "config",
        "bots",
        "schedule",
        "next_game_idx",
        "started_at",
        "updated_at",
        "completed",
    }
    if not required_keys.issubset(set(data.keys())):
        missing = sorted(required_keys.difference(set(data.keys())))
        raise ValueError(f"missing_keys={','.join(missing)}")

    if int(data["version"]) != STATE_VERSION:
        raise ValueError(
            f"unsupported_state_version={data['version']} expected={STATE_VERSION}"
        )

    config_data = data["config"]
    mode = str(config_data["mode"])
    config = RunConfig(
        mode=mode,
        games_per_pair=int(config_data["games_per_pair"]),
        timeout_sec=int(config_data["timeout_sec"]),
        base_port=int(config_data["base_port"]),
        parallel_workers=resolve_parallel_workers(
            mode,
            int(config_data.get("parallel_workers", 0)) if str(config_data.get("parallel_workers", "")).isdigit() else None,
        ),
    )

    bots = []
    for raw_bot in data["bots"]:
        bots.append(
            BotStats(
                name=str(raw_bot["name"]),
                path=str(raw_bot["path"]),
                elo=float(raw_bot.get("elo", 1000.0)),
                wins=int(raw_bot.get("wins", 0)),
                losses=int(raw_bot.get("losses", 0)),
                draws=int(raw_bot.get("draws", 0)),
                errors=int(raw_bot.get("errors", 0)),
            )
        )

    schedule = []
    for raw_match in data["schedule"]:
        schedule.append(
            MatchSpec(
                game_idx=int(raw_match["game_idx"]),
                bot_one_idx=int(raw_match["bot_one_idx"]),
                bot_two_idx=int(raw_match["bot_two_idx"]),
            )
        )

    game_records = []
    for raw_record in data.get("game_records", []):
        game_records.append(
            GameRecord(
                game_idx=int(raw_record["game_idx"]),
                bot_one=str(raw_record["bot_one"]),
                bot_two=str(raw_record["bot_two"]),
                result=(
                    int(raw_record["result"])
                    if raw_record.get("result") is not None
                    else None
                ),
                reason=str(raw_record.get("reason", "")),
                duration_sec=float(raw_record.get("duration_sec", 0.0)),
                server_log_path=(
                    str(raw_record["server_log_path"])
                    if raw_record.get("server_log_path")
                    else None
                ),
                bot_one_log_path=(
                    str(raw_record["bot_one_log_path"])
                    if raw_record.get("bot_one_log_path")
                    else None
                ),
                bot_two_log_path=(
                    str(raw_record["bot_two_log_path"])
                    if raw_record.get("bot_two_log_path")
                    else None
                ),
            )
        )

    state = RunState(
        version=int(data["version"]),
        mode=str(data["mode"]),
        run_id=str(
            data.get("run_id")
            or make_run_id(str(data["mode"]), str(data["started_at"]))
        ),
        config=config,
        bots=bots,
        schedule=schedule,
        next_game_idx=int(data["next_game_idx"]),
        started_at=str(data["started_at"]),
        updated_at=str(data["updated_at"]),
        completed=bool(data["completed"]),
        game_records=game_records,
    )

    if state.next_game_idx < 0 or state.next_game_idx > len(state.schedule):
        raise ValueError("next_game_idx out of bounds")

    for match in state.schedule:
        if match.bot_one_idx < 0 or match.bot_two_idx < 0:
            raise ValueError("negative bot index in schedule")
        if match.bot_one_idx >= len(state.bots) or match.bot_two_idx >= len(state.bots):
            raise ValueError("schedule bot index out of range")

    return state


def load_state() -> RunState | None:
    if not SAVE_FILE.exists():
        return None
    try:
        with SAVE_FILE.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            raise ValueError("state root is not an object")
        return parse_run_state(raw)
    except Exception as exc:
        print(f"Warnung: Konnte `{SAVE_FILE}` nicht laden ({exc}).")
        return None


def is_resumable(state: RunState | None) -> bool:
    if state is None:
        return False
    if state.completed:
        return False
    return state.next_game_idx < len(state.schedule)


def default_parallel_workers(mode: str) -> int:
    if mode == "duel":
        return 1
    cpu = os.cpu_count() or 2
    return max(1, min(4, cpu))


def resolve_parallel_workers(mode: str, requested: int | None = None) -> int:
    if requested is not None and requested > 0:
        return requested
    env_raw = os.getenv("DUEL_WORKERS", "").strip()
    if env_raw.isdigit() and int(env_raw) > 0:
        return int(env_raw)
    return default_parallel_workers(mode)


# ========================= BOT DISCOVERY =========================


def get_python(bot_path: Path) -> str:
    # Check for venv in bot's directory
    venv_python = bot_path.parent / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    # Check for venv in project root
    project_venv = Path(__file__).parent / ".venv" / "bin" / "python"
    if project_venv.exists():
        return str(project_venv)
    return "python3"


def detect_bot_markers(path: Path) -> bool:
    try:
        if path.stat().st_size > MAX_BOT_SCAN_FILE_SIZE:
            return False
    except Exception:
        return False

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="latin-1", errors="ignore")
    except Exception:
        return False

    if "Starter" not in text and "IClientHandler" not in text and "socha" not in text:
        return False

    regex_has_starter_import = bool(
        re.search(r"\bfrom\s+socha\.starter\s+import\s+Starter\b", text)
    )
    regex_has_iclient = "IClientHandler" in text
    regex_has_starter_call = bool(re.search(r"\bStarter\s*\(", text))

    has_starter_import = regex_has_starter_import
    has_iclient = regex_has_iclient
    has_starter_call = regex_has_starter_call

    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return has_starter_call and (has_starter_import or has_iclient)

    starter_aliases = {"Starter"}
    iclient_aliases = {"IClientHandler"}

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "socha.starter":
                has_starter_import = True
                for alias in node.names:
                    if alias.name == "Starter":
                        starter_aliases.add(alias.asname or alias.name)
            if node.module == "socha.api.networking.game_client":
                for alias in node.names:
                    if alias.name == "IClientHandler":
                        has_iclient = True
                        iclient_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "socha.starter":
                    has_starter_import = True
                if alias.name.startswith("socha.api.networking.game_client"):
                    has_iclient = True
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in starter_aliases:
                has_starter_call = True
            elif isinstance(func, ast.Attribute) and func.attr == "Starter":
                has_starter_call = True
        elif isinstance(node, ast.ClassDef):
            for base in node.bases:
                if isinstance(base, ast.Name) and base.id in iclient_aliases:
                    has_iclient = True
                elif isinstance(base, ast.Attribute) and base.attr == "IClientHandler":
                    has_iclient = True

    return has_starter_call and (has_starter_import or has_iclient)


def make_unique_names(paths: list[Path]) -> dict[Path, str]:
    stems: dict[str, int] = {}
    for path in paths:
        stems[path.stem] = stems.get(path.stem, 0) + 1

    names: dict[Path, str] = {}
    cwd = Path.cwd()
    for path in paths:
        stem = path.stem
        if stems[stem] == 1:
            names[path] = stem
        else:
            try:
                rel_parent = path.parent.relative_to(cwd)
            except ValueError:
                rel_parent = path.parent
            names[path] = f"{stem} [{rel_parent}]"
    return names


def normalize_bot_file_path(raw_path: str) -> Path:
    token = raw_path.strip()
    if not token:
        raise ValueError("Leerer Pfad.")

    path = Path(token).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    resolved = path.resolve()

    if not resolved.exists():
        raise ValueError(f"Pfad existiert nicht: {resolved}")
    if not resolved.is_file():
        raise ValueError(f"Kein Datei-Pfad: {resolved}")
    if resolved.suffix.lower() != ".py":
        raise ValueError(f"Nur .py erlaubt: {resolved.name}")
    return resolved


def load_custom_bot_paths() -> list[str]:
    if not CUSTOM_BOTS_FILE.exists():
        return []
    try:
        with CUSTOM_BOTS_FILE.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return []

    if isinstance(raw, dict):
        raw_paths = raw.get("paths", [])
    elif isinstance(raw, list):
        raw_paths = raw
    else:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for item in raw_paths:
        if not isinstance(item, str):
            continue
        token = item.strip()
        if not token:
            continue
        try:
            normalized = str(normalize_bot_file_path(token))
        except Exception:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def save_custom_bot_paths(paths: list[str]) -> None:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in paths:
        if not isinstance(item, str):
            continue
        token = item.strip()
        if not token:
            continue
        try:
            path = str(normalize_bot_file_path(token))
        except Exception:
            continue
        if path in seen:
            continue
        seen.add(path)
        normalized.append(path)
    save_json_atomic(CUSTOM_BOTS_FILE, {"paths": normalized})


def persist_custom_bot_path(path: Path) -> bool:
    current = load_custom_bot_paths()
    normalized = str(path)
    if normalized in current:
        return False
    current.append(normalized)
    save_custom_bot_paths(current)
    return True


def remove_custom_bot_paths(paths_to_remove: list[str]) -> int:
    remove_set = set(paths_to_remove)
    current = load_custom_bot_paths()
    filtered = [path for path in current if path not in remove_set]
    removed = len(current) - len(filtered)
    if removed > 0:
        save_custom_bot_paths(filtered)
    return removed


def should_skip_scan_dir(name: str) -> bool:
    if name in SKIP_SCAN_DIR_NAMES:
        return True
    if name.startswith("."):
        return True
    return False


def iter_python_files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = [name for name in dirnames if not should_skip_scan_dir(name)]
        dirnames.sort()
        filenames.sort()
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            yield Path(dirpath) / filename


def discover_bots() -> list[BotSpec]:
    candidates: list[Path] = []
    for file_path in iter_python_files(Path.cwd()):
        if not file_path.is_file():
            continue
        if detect_bot_markers(file_path):
            candidates.append(file_path.resolve())

    if not candidates:
        return []

    names = make_unique_names(candidates)
    bots: list[BotSpec] = []
    for path in candidates:
        bots.append(
            BotSpec(
                path=str(path),
                name=names[path],
                python_exec=get_python(path),
                is_custom=False,
                is_discovered=True,
            )
        )
    return bots


def add_custom_bot_candidate(
    candidates: list[BotSpec],
    raw_path: str,
    persist: bool = True,
) -> tuple[int | None, str]:
    try:
        resolved = normalize_bot_file_path(raw_path)
    except Exception as exc:
        return None, str(exc)

    for idx, bot in enumerate(candidates):
        try:
            existing = Path(bot.path).resolve()
        except Exception:
            existing = Path(bot.path)
        if existing == resolved:
            if persist:
                persist_custom_bot_path(resolved)
            bot.is_custom = True
            return idx, f"Bereits vorhanden: {rel_path_str(str(resolved))}"

    used_names = {bot.name for bot in candidates}
    stem = resolved.stem
    try:
        rel_parent = resolved.parent.relative_to(Path.cwd())
    except ValueError:
        rel_parent = resolved.parent
    base_name = stem if stem not in used_names else f"{stem} [{rel_parent}]"
    name = base_name
    suffix = 2
    while name in used_names:
        name = f"{base_name} ({suffix})"
        suffix += 1

    if persist:
        persist_custom_bot_path(resolved)

    bot = BotSpec(
        path=str(resolved),
        name=name,
        python_exec=get_python(resolved),
        is_custom=True,
        is_discovered=False,
    )
    candidates.append(bot)
    has_markers = detect_bot_markers(resolved)
    if has_markers:
        return len(candidates) - 1, f"Hinzugefügt: {name}"
    return len(candidates) - 1, f"Hinzugefügt (ohne Marker): {name}"


def load_saved_custom_candidates(candidates: list[BotSpec]) -> int:
    loaded = 0
    for raw_path in load_custom_bot_paths():
        idx, _ = add_custom_bot_candidate(candidates, raw_path, persist=False)
        if idx is not None:
            loaded += 1
    return loaded


# ========================= SCHEDULE =========================


def build_duel_schedule(games: int) -> list[MatchSpec]:
    schedule: list[MatchSpec] = []
    for game_idx in range(games):
        if game_idx % 2 == 0:
            bot_one_idx, bot_two_idx = 0, 1
        else:
            bot_one_idx, bot_two_idx = 1, 0
        schedule.append(
            MatchSpec(
                game_idx=game_idx,
                bot_one_idx=bot_one_idx,
                bot_two_idx=bot_two_idx,
            )
        )
    return schedule


def build_tournament_schedule(bot_count: int, games_per_pair: int = 2) -> list[MatchSpec]:
    schedule: list[MatchSpec] = []
    game_idx = 0
    games = max(1, games_per_pair)
    for first in range(bot_count):
        for second in range(first + 1, bot_count):
            for game_num in range(games):
                if game_num % 2 == 0:
                    bot_one_idx, bot_two_idx = first, second
                else:
                    bot_one_idx, bot_two_idx = second, first
                schedule.append(
                    MatchSpec(
                        game_idx=game_idx,
                        bot_one_idx=bot_one_idx,
                        bot_two_idx=bot_two_idx,
                    )
                )
                game_idx += 1
    return schedule


# ========================= ELO / STATS =========================


def expected_score(rating_one: float, rating_two: float) -> float:
    return 1 / (1 + 10 ** ((rating_two - rating_one) / 400))


def update_elo(bot_one: BotStats, bot_two: BotStats, result: int) -> None:
    expected_one = expected_score(bot_one.elo, bot_two.elo)
    expected_two = expected_score(bot_two.elo, bot_one.elo)

    if result == RESULT_WIN_ONE:
        score_one, score_two = 1.0, 0.0
    elif result == RESULT_WIN_TWO:
        score_one, score_two = 0.0, 1.0
    elif result == RESULT_DRAW:
        score_one, score_two = 0.5, 0.5
    else:
        return

    bot_one.elo += ELO_K * (score_one - expected_one)
    bot_two.elo += ELO_K * (score_two - expected_two)


def apply_game_result(
    bot_one: BotStats,
    bot_two: BotStats,
    result: int | None,
) -> None:
    if result == RESULT_WIN_ONE:
        bot_one.wins += 1
        bot_two.losses += 1
        update_elo(bot_one, bot_two, RESULT_WIN_ONE)
        return
    if result == RESULT_WIN_TWO:
        bot_two.wins += 1
        bot_one.losses += 1
        update_elo(bot_one, bot_two, RESULT_WIN_TWO)
        return
    if result == RESULT_DRAW:
        bot_one.draws += 1
        bot_two.draws += 1
        update_elo(bot_one, bot_two, RESULT_DRAW)
        return

    bot_one.errors += 1
    bot_two.errors += 1


def rank_bots(bots: list[BotStats]) -> list[BotStats]:
    return sorted(
        bots,
        key=lambda b: (b.score, b.elo, b.wins),
        reverse=True,
    )


# ========================= PORTS / PROCESSES =========================


def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def find_free_port(start_port: int) -> int:
    port = max(1024, start_port)
    while port <= start_port + 5000:
        with PORT_RESERVE_LOCK:
            reserved = port in RESERVED_PORTS
        if not reserved and not is_port_in_use(port):
            return port
        port += 1
    raise RuntimeError("No free port found in search window")


def reserve_free_port(start_port: int) -> int:
    port = max(1024, start_port)
    while port <= start_port + 5000:
        with PORT_RESERVE_LOCK:
            if port not in RESERVED_PORTS and not is_port_in_use(port):
                RESERVED_PORTS.add(port)
                return port
        port += 1
    raise RuntimeError("No free port found in search window")


def release_reserved_port(port: int | None) -> None:
    if port is None:
        return
    with PORT_RESERVE_LOCK:
        RESERVED_PORTS.discard(port)


def terminate_process_group(proc: subprocess.Popen[Any] | None, grace_sec: float = 2.0) -> None:
    if proc is None:
        return
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except Exception:
        return

    deadline = time.monotonic() + grace_sec
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.1)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except Exception:
        pass


def parse_game_result(log_content: str) -> tuple[int | None, str]:
    sanitized = ANSI_ESCAPE_RE.sub("", log_content)

    if re.search(r"winner\s*=\s*ONE\b", sanitized, re.IGNORECASE):
        return RESULT_WIN_ONE, "winner=ONE"
    if re.search(r"winner\s*=\s*TWO\b", sanitized, re.IGNORECASE):
        return RESULT_WIN_TWO, "winner=TWO"
    if re.search(r"Winner:\s*ONE\b", sanitized, re.IGNORECASE):
        return RESULT_WIN_ONE, "Winner: ONE"
    if re.search(r"Winner:\s*TWO\b", sanitized, re.IGNORECASE):
        return RESULT_WIN_TWO, "Winner: TWO"
    if re.search(r"winner\s*=\s*Team One\b", sanitized, re.IGNORECASE):
        return RESULT_WIN_ONE, "winner=Team One"
    if re.search(r"winner\s*=\s*Team Two\b", sanitized, re.IGNORECASE):
        return RESULT_WIN_TWO, "winner=Team Two"
    if re.search(
        r"\b(draw|tie|unentschieden|gleichstand)\b|winner\s*=\s*(NONE|NULL|KEINER?)\b",
        sanitized,
        re.IGNORECASE,
    ):
        return RESULT_DRAW, "draw"

    score_matches = list(SCORES_RE.finditer(sanitized))
    if score_matches:
        last = score_matches[-1]
        p1_points = int(last.group(1))
        p1_swarm = int(last.group(2))
        p2_points = int(last.group(3))
        p2_swarm = int(last.group(4))

        if p1_points > p2_points:
            return RESULT_WIN_ONE, "scores:p1_points"
        if p2_points > p1_points:
            return RESULT_WIN_TWO, "scores:p2_points"
        if p1_swarm > p2_swarm:
            return RESULT_WIN_ONE, "scores:p1_swarm"
        if p2_swarm > p1_swarm:
            return RESULT_WIN_TWO, "scores:p2_swarm"
        return RESULT_DRAW, "scores:equal"

    winner_match = re.search(r"winner\s*=\s*([A-Z_ ]+)", sanitized, re.IGNORECASE)
    if winner_match:
        winner = winner_match.group(1).upper()
        if "ONE" in winner:
            return RESULT_WIN_ONE, f"winner={winner}"
        if "TWO" in winner:
            return RESULT_WIN_TWO, f"winner={winner}"
        if "NONE" in winner or "DRAW" in winner or "TIE" in winner:
            return RESULT_DRAW, f"winner={winner}"

    lower = sanitized.lower()
    if (
        ("spiel beendet" in lower or "game finished" in lower or "game ended" in lower)
        and "winner" not in lower
    ):
        return RESULT_DRAW, "finished_no_winner"
    if "spieler 1" in lower and "spieler 2" in lower and "siegpunkte" in lower:
        return RESULT_DRAW, "scores_unresolved_assume_draw"

    return None, "winner_not_found"


def run_dir_for_state(state: RunState) -> Path:
    return RUNS_DIR / state.run_id


def game_log_paths(run_dir: Path, game_idx: int) -> tuple[Path, Path, Path]:
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stem = f"game_{game_idx:05d}"
    return (
        logs_dir / f"{stem}_server.log",
        logs_dir / f"{stem}_bot_one.log",
        logs_dir / f"{stem}_bot_two.log",
    )


def read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def check_turn_timeouts(log_content: str) -> list[float]:
    """Parse bot log and return list of turn times that exceeded TURN_TIME_LIMIT."""
    violations = []
    for match in TURN_TIME_RE.finditer(log_content):
        try:
            turn_time = float(match.group(1))
            if turn_time > TURN_TIME_LIMIT:
                violations.append(turn_time)
        except ValueError:
            pass
    return violations


def run_game(
    bot_one: BotSpec,
    bot_two: BotSpec,
    game_idx: int,
    timeout_sec: int,
    base_port: int,
    run_dir: Path,
    on_tick: Callable[[float], None] | None = None,
) -> tuple[int | None, str, float, str | None, str | None, str | None]:
    port: int | None = reserve_free_port(base_port + game_idx * PORT_STEP)
    server_log_path, bot_one_log_path, bot_two_log_path = game_log_paths(run_dir, game_idx)
    server_log_path.unlink(missing_ok=True)
    bot_one_log_path.unlink(missing_ok=True)
    bot_two_log_path.unlink(missing_ok=True)

    server_proc: subprocess.Popen[Any] | None = None
    bot_one_proc: subprocess.Popen[Any] | None = None
    bot_two_proc: subprocess.Popen[Any] | None = None
    server_handle: Any | None = None
    bot_one_handle: Any | None = None
    bot_two_handle: Any | None = None

    result: int | None = None
    reason = "unknown"
    timed_out = False
    start_time = time.monotonic()

    try:
        server_handle = server_log_path.open("w", encoding="utf-8")
        bot_one_handle = bot_one_log_path.open("w", encoding="utf-8")
        bot_two_handle = bot_two_log_path.open("w", encoding="utf-8")
        server_proc = subprocess.Popen(
            ["java", "-jar", str(SERVER_JAR), "--port", str(port)],
            stdout=server_handle,
            stderr=subprocess.STDOUT,
            cwd=str(Path(__file__).parent),
            preexec_fn=os.setsid,
        )

        time.sleep(2.0)

        # Prepare environment with PYTHONPATH for package imports
        project_root = str(Path(__file__).parent)
        bot_env = os.environ.copy()
        existing_pythonpath = bot_env.get("PYTHONPATH", "")
        bot_env["PYTHONPATH"] = f"{project_root}:{existing_pythonpath}" if existing_pythonpath else project_root

        bot_one_proc = subprocess.Popen(
            [bot_one.python_exec, str(Path(bot_one.path).resolve()), "--port", str(port)],
            stdout=bot_one_handle,
            stderr=subprocess.STDOUT,
            cwd=str(Path(bot_one.path).resolve().parent),
            env=bot_env,
            preexec_fn=os.setsid,
        )
        time.sleep(0.5)
        bot_two_proc = subprocess.Popen(
            [bot_two.python_exec, str(Path(bot_two.path).resolve()), "--port", str(port)],
            stdout=bot_two_handle,
            stderr=subprocess.STDOUT,
            cwd=str(Path(bot_two.path).resolve().parent),
            env=bot_env,
            preexec_fn=os.setsid,
        )

        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if (
                bot_one_proc.poll() is not None
                and bot_two_proc.poll() is not None
            ):
                break
            if on_tick is not None:
                try:
                    on_tick(time.monotonic() - start_time)
                except Exception:
                    pass
            time.sleep(0.15)
        else:
            timed_out = True

        time.sleep(0.5)

        if server_handle is not None:
            server_handle.flush()
        if bot_one_handle is not None:
            bot_one_handle.flush()
        if bot_two_handle is not None:
            bot_two_handle.flush()

        server_content = read_text_if_exists(server_log_path)
        bot_one_content = read_text_if_exists(bot_one_log_path)
        bot_two_content = read_text_if_exists(bot_two_log_path)
        combined_logs = "\n".join(part for part in [server_content, bot_one_content, bot_two_content] if part)

        if server_content:
            result, reason = parse_game_result(server_content)
        if result is None and combined_logs:
            result, reason = parse_game_result(combined_logs)
        if not combined_logs:
            reason = "log_missing"

        if result is None and timed_out:
            reason = "timeout"
        elif result is None:
            rc_one = bot_one_proc.returncode if bot_one_proc and bot_one_proc.poll() is not None else None
            rc_two = bot_two_proc.returncode if bot_two_proc and bot_two_proc.poll() is not None else None
            rc_server = server_proc.returncode if server_proc and server_proc.poll() is not None else None
            if rc_one == 0 and rc_two == 0 and not timed_out:
                result = RESULT_DRAW
                reason = "bots_exit_zero_assume_draw"
            else:
                reason = f"{reason}|rc_server={rc_server}|rc_one={rc_one}|rc_two={rc_two}"

    except FileNotFoundError as exc:
        reason = f"spawn_failed:{exc.filename}"
        result = None
    except Exception as exc:
        reason = f"runner_error:{exc}"
        result = None
    finally:
        terminate_process_group(bot_one_proc)
        terminate_process_group(bot_two_proc)
        terminate_process_group(server_proc)
        if server_handle is not None:
            server_handle.close()
        if bot_one_handle is not None:
            bot_one_handle.close()
        if bot_two_handle is not None:
            bot_two_handle.close()
        release_reserved_port(port)

    duration_sec = time.monotonic() - start_time
    return (
        result,
        reason,
        duration_sec,
        str(server_log_path),
        str(bot_one_log_path),
        str(bot_two_log_path),
    )


# ========================= RESULTS =========================


def result_path_for_mode(mode: str) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if mode == "duel":
        return LATEST_DUEL_RESULTS_FILE
    return LATEST_TOURNAMENT_RESULTS_FILE


def ensure_run_dirs(state: RunState) -> Path:
    run_dir = run_dir_for_state(state)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    return run_dir


def game_record_to_dict(record: GameRecord) -> dict[str, Any]:
    return asdict(record)


def save_results(state: RunState, update_latest: bool = True) -> Path:
    run_dir = ensure_run_dirs(state)
    bots = rank_bots(state.bots)
    records = state.game_records

    payload: dict[str, Any] = {
        "run_id": state.run_id,
        "run_dir": str(run_dir),
        "mode": state.mode,
        "started_at": state.started_at,
        "completed_at": state.updated_at,
        "completed": state.completed,
        "games_per_pair": state.config.games_per_pair,
        "timeout_sec": state.config.timeout_sec,
        "parallel_workers": effective_worker_count(state),
        "total_games": len(state.schedule),
        "bots": [
            {
                "name": bot.name,
                "path": bot.path,
                "wins": bot.wins,
                "losses": bot.losses,
                "draws": bot.draws,
                "errors": bot.errors,
                "games": bot.games,
                "score": round(bot.score, 3),
                "winrate": bot.winrate,
                "elo": round(bot.elo, 2),
            }
            for bot in bots
        ],
        "records_summary": {
            "bot_one_wins": sum(1 for rec in records if rec.result == RESULT_WIN_ONE),
            "bot_two_wins": sum(1 for rec in records if rec.result == RESULT_WIN_TWO),
            "draws": sum(1 for rec in records if rec.result == RESULT_DRAW),
            "errors": sum(1 for rec in records if rec.result is None),
        },
        "game_records": [game_record_to_dict(rec) for rec in records],
    }

    output_path = run_dir / "summary.json"
    save_json_atomic(output_path, payload)
    if update_latest:
        save_json_atomic(result_path_for_mode(state.mode), payload)
    return output_path


def available_result_files() -> list[Path]:
    if not RUNS_DIR.exists():
        return []
    summaries = [path for path in RUNS_DIR.glob("*/summary.json") if path.is_file()]
    summaries.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return summaries


def load_results_payload(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            return None
        return raw
    except Exception:
        return None


def build_results_summary_lines(path: Path, payload: dict[str, Any]) -> list[str]:
    mode = str(payload.get("mode", "unknown"))
    completed_at = str(payload.get("completed_at", "unknown"))
    started_at = str(payload.get("started_at", "unknown"))
    run_id = str(payload.get("run_id", path.parent.name))
    total_games = payload.get("total_games", "?")
    bots_raw = payload.get("bots", [])
    lines: list[str] = [
        f"Run-ID: {run_id}",
        f"Summary: {path}",
        f"Mode: {mode}",
        f"Started: {started_at}",
        f"Completed: {completed_at}",
        f"Total Games: {total_games}",
        "",
    ]

    header = f"{'#':>2} {'Bot':<26} {'Elo':>7} {'W':>4} {'D':>4} {'L':>4} {'E':>4} {'T':>4} {'Score':>7}"
    lines.append(header)
    lines.append("-" * len(header))

    bot_rows = 0
    if isinstance(bots_raw, list):
        for idx, raw_bot in enumerate(bots_raw, start=1):
            if not isinstance(raw_bot, dict):
                continue
            name = truncate_text(str(raw_bot.get("name", "unknown")), 26)
            elo = float(raw_bot.get("elo", 0.0))
            wins = int(raw_bot.get("wins", 0))
            draws = int(raw_bot.get("draws", 0))
            losses = int(raw_bot.get("losses", 0))
            errors = int(raw_bot.get("errors", 0))
            timeouts = int(raw_bot.get("timeouts", 0))
            score = float(raw_bot.get("score", 0.0))
            lines.append(
                f"{idx:>2} {name:<26} {elo:>7.1f} {wins:>4} {draws:>4} {losses:>4} {errors:>4} {timeouts:>4} {score:>7.3f}"
            )
            bot_rows += 1
    if bot_rows == 0:
        lines.append("(keine Bot-Daten gefunden)")
    return lines


def build_results_records_lines(
    payload: dict[str, Any],
    errors_only: bool = False,
) -> list[str]:
    records_raw = payload.get("game_records", [])
    lines: list[str] = []
    if not isinstance(records_raw, list) or not records_raw:
        return ["Keine Spielverläufe gespeichert."]

    for raw in records_raw:
        if not isinstance(raw, dict):
            continue
        result = raw.get("result")
        if result == RESULT_WIN_ONE:
            outcome = "ONE"
        elif result == RESULT_WIN_TWO:
            outcome = "TWO"
        elif result == RESULT_DRAW:
            outcome = "DRAW"
        else:
            outcome = "ERR"
        if errors_only and outcome != "ERR":
            continue
        idx = int(raw.get("game_idx", -1)) + 1
        one = str(raw.get("bot_one", "?"))
        two = str(raw.get("bot_two", "?"))
        reason = str(raw.get("reason", ""))
        dur = float(raw.get("duration_sec", 0.0))
        server_log = str(raw.get("server_log_path") or "-")
        bot_one_log = str(raw.get("bot_one_log_path") or "-")
        bot_two_log = str(raw.get("bot_two_log_path") or "-")
        lines.append(
            f"#{idx:04d} {outcome:<4} {one} vs {two} | {dur:6.1f}s | {reason} | "
            f"server={server_log} | one={bot_one_log} | two={bot_two_log}"
        )

    if not lines:
        return ["Keine passenden Spielverläufe."]
    return lines


def available_log_files_for_summary(summary_path: Path) -> list[Path]:
    log_dir = summary_path.parent / "logs"
    if not log_dir.exists():
        return []
    logs = [path for path in log_dir.glob("*.log") if path.is_file()]
    logs.sort()
    return logs


def build_log_preview_lines(path: Path, max_lines: int = 400) -> list[str]:
    content = read_text_if_exists(path)
    if not content:
        return [f"{path}", "", "(Log leer)"]
    all_lines = content.splitlines()
    lines = all_lines[-max_lines:]
    return [f"{path}", ""] + lines


def show_results_viewer_plain() -> None:
    files = available_result_files()
    if not files:
        print("Keine Ergebnisdatei gefunden.")
        return
    print("\nErgebnis-Viewer")
    for idx, path in enumerate(files, start=1):
        print(f"  {idx} {path}")
    valid = {str(i) for i in range(1, len(files) + 1)} | {"q", "Q"}
    choice = prompt_choice_plain(f"Datei wählen [1-{len(files)}] oder q: ", valid)
    if choice.lower() == "q":
        return
    selected = files[int(choice) - 1]
    payload = load_results_payload(selected)
    if payload is None:
        print(f"Konnte {selected} nicht lesen.")
        return
    while True:
        print("\nErgebnis-Viewer")
        print("  1 Summary")
        print("  2 Games")
        print("  3 Error-Games")
        print("  4 Logs")
        print("  5 Zurück")
        choice = prompt_choice_plain("Auswahl [1/2/3/4/5]: ", {"1", "2", "3", "4", "5"})
        if choice == "1":
            for line in build_results_summary_lines(selected, payload):
                print(line)
            input("Weiter mit Enter...")
            continue
        if choice == "2":
            for line in build_results_records_lines(payload, errors_only=False):
                print(line)
            input("Weiter mit Enter...")
            continue
        if choice == "3":
            for line in build_results_records_lines(payload, errors_only=True):
                print(line)
            input("Weiter mit Enter...")
            continue
        if choice == "4":
            logs = available_log_files_for_summary(selected)
            if not logs:
                print("Keine Logs gefunden.")
                input("Weiter mit Enter...")
                continue
            for idx, log_path in enumerate(logs, start=1):
                print(f"  {idx} {log_path}")
            valid = {str(i) for i in range(1, len(logs) + 1)} | {"q", "Q"}
            log_choice = prompt_choice_plain(f"Log wählen [1-{len(logs)}] oder q: ", valid)
            if log_choice.lower() == "q":
                continue
            selected_log = logs[int(log_choice) - 1]
            for line in build_log_preview_lines(selected_log):
                print(line)
            input("Weiter mit Enter...")
            continue
        return

# ========================= TUI HELPERS =========================


def rel_path_str(path: str) -> str:
    p = Path(path)
    try:
        return str(p.relative_to(Path.cwd()))
    except ValueError:
        return str(p)


def parse_index_selection(raw: str, max_index: int) -> list[int]:
    selected: list[int] = []
    seen: set[int] = set()

    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            pieces = token.split("-", 1)
            if len(pieces) != 2 or not pieces[0].isdigit() or not pieces[1].isdigit():
                raise ValueError(f"Ungültiger Bereich: {token}")
            start = int(pieces[0])
            end = int(pieces[1])
            if start > end:
                start, end = end, start
            for value in range(start, end + 1):
                if value < 1 or value > max_index:
                    raise ValueError(f"Index außerhalb Bereich: {value}")
                idx = value - 1
                if idx not in seen:
                    seen.add(idx)
                    selected.append(idx)
            continue

        if not token.isdigit():
            raise ValueError(f"Ungültiger Index: {token}")
        value = int(token)
        if value < 1 or value > max_index:
            raise ValueError(f"Index außerhalb Bereich: {value}")
        idx = value - 1
        if idx not in seen:
            seen.add(idx)
            selected.append(idx)

    if not selected:
        raise ValueError("Keine gültige Auswahl erkannt")
    return selected


def format_record_summary(record: GameRecord | None) -> str:
    if record is None:
        return "Noch kein Spiel abgeschlossen."
    if record.result == RESULT_WIN_ONE:
        outcome = f"{record.bot_one} gewann"
    elif record.result == RESULT_WIN_TWO:
        outcome = f"{record.bot_two} gewann"
    elif record.result == RESULT_DRAW:
        outcome = "Unentschieden"
    else:
        outcome = "Fehler"
    return (
        f"Spiel {record.game_idx + 1}: {record.bot_one} vs {record.bot_two} -> "
        f"{outcome} ({record.reason}, {record.duration_sec:.1f}s)"
    )


def parse_iso_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


# ========================= RUN LOOP =========================


def stats_to_spec(bot: BotStats) -> BotSpec:
    path = Path(bot.path)
    return BotSpec(path=str(path), name=bot.name, python_exec=get_python(path))


@dataclass
class MatchOutcome:
    order: int
    match: MatchSpec
    bot_one_name: str
    bot_two_name: str
    result: int | None
    reason: str
    duration_sec: float
    server_log_path: str | None
    bot_one_log_path: str | None
    bot_two_log_path: str | None


def effective_worker_count(state: RunState) -> int:
    requested = int(getattr(state.config, "parallel_workers", 1))
    return max(1, requested)


def run_match_batch(
    state: RunState,
    matches: list[MatchSpec],
    workers: int,
    run_dir: Path,
    poll_hook: Callable[[int, int, int], None] | None = None,
) -> list[MatchOutcome]:
    if not matches:
        return []

    bot_specs = [stats_to_spec(bot) for bot in state.bots]
    jobs: list[tuple[int, MatchSpec, BotSpec, BotSpec]] = []
    for order, match in enumerate(matches):
        jobs.append(
            (
                order,
                match,
                bot_specs[match.bot_one_idx],
                bot_specs[match.bot_two_idx],
            )
        )

    total = len(jobs)
    if workers <= 1:
        outcomes: list[MatchOutcome] = []
        for order, match, bot_one, bot_two in jobs:
            def _tick(_: float) -> None:
                if poll_hook is not None:
                    poll_hook(len(outcomes), total - len(outcomes), total)

            result, reason, duration_sec, server_log_path, bot_one_log_path, bot_two_log_path = run_game(
                bot_one=bot_one,
                bot_two=bot_two,
                game_idx=match.game_idx,
                timeout_sec=state.config.timeout_sec,
                base_port=state.config.base_port,
                run_dir=run_dir,
                on_tick=_tick if poll_hook is not None else None,
            )
            outcomes.append(
                MatchOutcome(
                    order=order,
                    match=match,
                    bot_one_name=bot_one.name,
                    bot_two_name=bot_two.name,
                    result=result,
                    reason=reason,
                    duration_sec=duration_sec,
                    server_log_path=server_log_path,
                    bot_one_log_path=bot_one_log_path,
                    bot_two_log_path=bot_two_log_path,
                )
            )
            if poll_hook is not None:
                poll_hook(len(outcomes), 0, total)
        return outcomes

    outcomes: list[MatchOutcome | None] = [None] * total
    futures: dict[
        Future[tuple[int | None, str, float, str | None, str | None, str | None]],
        tuple[int, MatchSpec, BotSpec, BotSpec],
    ] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for order, match, bot_one, bot_two in jobs:
            future = executor.submit(
                run_game,
                bot_one,
                bot_two,
                match.game_idx,
                state.config.timeout_sec,
                state.config.base_port,
                run_dir,
            )
            futures[future] = (order, match, bot_one, bot_two)

        if poll_hook is not None:
            poll_hook(0, len(futures), total)

        while futures:
            done, _ = wait(
                list(futures.keys()),
                timeout=0.15,
                return_when=FIRST_COMPLETED,
            )
            if not done:
                if poll_hook is not None:
                    poll_hook(total - len(futures), len(futures), total)
                continue

            for future in done:
                order, match, bot_one, bot_two = futures.pop(future)
                try:
                    (
                        result,
                        reason,
                        duration_sec,
                        server_log_path,
                        bot_one_log_path,
                        bot_two_log_path,
                    ) = future.result()
                except Exception as exc:
                    result = None
                    reason = f"worker_error:{exc}"
                    duration_sec = 0.0
                    server_log_path = None
                    bot_one_log_path = None
                    bot_two_log_path = None
                outcomes[order] = MatchOutcome(
                    order=order,
                    match=match,
                    bot_one_name=bot_one.name,
                    bot_two_name=bot_two.name,
                    result=result,
                    reason=reason,
                    duration_sec=duration_sec,
                    server_log_path=server_log_path,
                    bot_one_log_path=bot_one_log_path,
                    bot_two_log_path=bot_two_log_path,
                )

            if poll_hook is not None:
                poll_hook(total - len(futures), len(futures), total)

    return [outcome for outcome in outcomes if outcome is not None]


def apply_match_outcome(state: RunState, outcome: MatchOutcome) -> GameRecord:
    bot_one_stats = state.bots[outcome.match.bot_one_idx]
    bot_two_stats = state.bots[outcome.match.bot_two_idx]

    apply_game_result(bot_one_stats, bot_two_stats, outcome.result)
    record = GameRecord(
        game_idx=outcome.match.game_idx,
        bot_one=outcome.bot_one_name,
        bot_two=outcome.bot_two_name,
        result=outcome.result,
        reason=outcome.reason,
        duration_sec=outcome.duration_sec,
        server_log_path=outcome.server_log_path,
        bot_one_log_path=outcome.bot_one_log_path,
        bot_two_log_path=outcome.bot_two_log_path,
    )
    state.game_records.append(record)
    state.next_game_idx += 1
    save_state(state)

    # Check for turn time limit violations
    if outcome.bot_one_log_path:
        bot_one_log = read_text_if_exists(Path(outcome.bot_one_log_path))
        violations_one = check_turn_timeouts(bot_one_log)
        if violations_one:
            bot_one_stats.timeouts += len(violations_one)
            print(
                f"⚠️  TIMEOUT: {outcome.bot_one_name} exceeded {TURN_TIME_LIMIT}s limit "
                f"({len(violations_one)}x, max={max(violations_one):.3f}s)"
            )
    if outcome.bot_two_log_path:
        bot_two_log = read_text_if_exists(Path(outcome.bot_two_log_path))
        violations_two = check_turn_timeouts(bot_two_log)
        if violations_two:
            bot_two_stats.timeouts += len(violations_two)
            print(
                f"⚠️  TIMEOUT: {outcome.bot_two_name} exceeded {TURN_TIME_LIMIT}s limit "
                f"({len(violations_two)}x, max={max(violations_two):.3f}s)"
            )

    return record


def prompt_choice_plain(prompt_text: str, valid: set[str]) -> str:
    while True:
        value = input(prompt_text).strip()
        if value in valid:
            return value
        print(f"Ungültige Eingabe. Erlaubt: {', '.join(sorted(valid))}")


def path_autocomplete_suggestions(prefix: str) -> list[str]:
    token = prefix.strip()
    suggestions: list[str] = []
    seen: set[str] = set()

    # suggest previously saved custom paths first
    for saved in load_custom_bot_paths():
        display = rel_path_str(saved)
        for option in [display, saved]:
            if token and not option.startswith(token):
                continue
            if option in seen:
                continue
            seen.add(option)
            suggestions.append(option)

    # then filesystem matches
    base_input = token if token else "."
    expanded = os.path.expanduser(base_input)
    if not os.path.isabs(expanded):
        expanded = str(Path.cwd() / expanded)
    for match in sorted(glob.glob(expanded + "*")):
        match_path = Path(match)
        try:
            display = str(match_path.relative_to(Path.cwd()))
        except ValueError:
            display = str(match_path)
        if match_path.is_dir():
            display = display.rstrip("/") + "/"
        if display in seen:
            continue
        seen.add(display)
        suggestions.append(display)

    return suggestions[:200]


def input_with_path_autocomplete(prompt_text: str) -> str:
    if readline is None or not sys.stdin.isatty():
        return input(prompt_text)

    state_cache: dict[str, list[str]] = {"prefix": "", "matches": []}

    def _completer(text: str, state: int) -> str | None:
        if state == 0 or text != state_cache["prefix"]:
            state_cache["prefix"] = text
            state_cache["matches"] = path_autocomplete_suggestions(text)
        matches = state_cache["matches"]
        if state < len(matches):
            return matches[state]
        return None

    old_completer = readline.get_completer()
    old_delims = readline.get_completer_delims()
    try:
        readline.set_completer(_completer)
        readline.set_completer_delims(" \t\n")
        readline.parse_and_bind("tab: complete")
        return input(prompt_text)
    finally:
        readline.set_completer(old_completer)
        readline.set_completer_delims(old_delims)


def prompt_positive_int_plain(prompt_text: str, default: int) -> int:
    while True:
        raw = input(prompt_text).strip()
        if not raw:
            return default
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
        print("Bitte eine positive ganze Zahl eingeben.")


def choose_mode_plain() -> str:
    print("\nModus wählen")
    print("  1 Duel (genau 2 Bots)")
    print("  2 Turnier (>=2 Bots, Spiele pro Paarung frei wählbar)")
    choice = prompt_choice_plain("Auswahl [1/2]: ", {"1", "2"})
    return "duel" if choice == "1" else "tournament"


def choose_bots_plain(candidates: list[BotSpec], mode: str) -> list[BotSpec]:
    while True:
        print("\nGefundene Bot-Entrypoints")
        print("-" * 120)
        print(f"{'#':>3}  {'Src':<3}  {'Name':<34}  {'Pfad':<62}  {'Python'}")
        print("-" * 120)
        for idx, bot in enumerate(candidates, start=1):
            src = "C" if bot.is_custom else "A"
            print(
                f"{idx:>3}  {src:<3}  {bot.name:<34}  {rel_path_str(bot.path):<62}  {bot.python_exec}"
            )
        if not candidates:
            print("(keine Bots in Liste)")
        print("-" * 120)
        print("  a  Custom Pfad hinzufügen")
        print("  r  Custom Pfad entfernen")

        raw = input("Bot-Auswahl (z.B. 1,4,7) oder a/r: ").strip()
        if raw.lower() == "a":
            custom_path = input_with_path_autocomplete("Custom Bot-Datei (.py): ").strip()
            _, message = add_custom_bot_candidate(candidates, custom_path)
            print(message)
            continue
        if raw.lower() == "r":
            removable: list[tuple[int, BotSpec]] = [
                (idx, bot) for idx, bot in enumerate(candidates) if bot.is_custom
            ]
            if not removable:
                print("Keine Custom-Pfade zum Entfernen vorhanden.")
                continue

            print("\nCustom-Pfade")
            for idx, (_, bot) in enumerate(removable, start=1):
                source = "auto+custom" if bot.is_discovered else "custom"
                print(f"  {idx:>2} {rel_path_str(bot.path)} [{source}]")
            raw_remove = input("Welche entfernen (z.B. 1,3): ").strip()
            try:
                remove_idxs = parse_index_selection(raw_remove, len(removable))
            except ValueError as exc:
                print(exc)
                continue

            paths_to_remove: list[str] = []
            candidate_indexes: list[int] = []
            for list_idx in remove_idxs:
                cand_idx, bot = removable[list_idx]
                candidate_indexes.append(cand_idx)
                paths_to_remove.append(str(Path(bot.path).resolve()))

            removed = remove_custom_bot_paths(paths_to_remove)
            for cand_idx in sorted(candidate_indexes, reverse=True):
                if cand_idx >= len(candidates):
                    continue
                bot = candidates[cand_idx]
                if bot.is_discovered:
                    bot.is_custom = False
                else:
                    del candidates[cand_idx]
            print(f"{removed} Custom-Pfade entfernt.")
            continue

        try:
            indices = parse_index_selection(raw, len(candidates))
        except ValueError as exc:
            print(exc)
            continue

        selected = [candidates[idx] for idx in indices]
        if mode == "duel" and len(selected) != 2:
            print("Duel benötigt exakt 2 Bots.")
            continue
        if mode == "tournament" and len(selected) < 2:
            print("Turnier benötigt mindestens 2 Bots.")
            continue
        return selected


def build_new_state_plain() -> RunState | None:
    print("\nNeuer Lauf")
    print("Suche Bots rekursiv im aktuellen Workspace...")
    candidates = discover_bots()
    loaded_custom = load_saved_custom_candidates(candidates)
    if loaded_custom > 0:
        print(f"{loaded_custom} gespeicherte Custom-Pfade geladen.")
    if len(candidates) == 0:
        print("Keine Bots automatisch gefunden. Du kannst gleich Custom-Pfade hinzufügen.")

    mode = choose_mode_plain()
    selected = choose_bots_plain(candidates, mode)
    default_workers = resolve_parallel_workers(mode)
    parallel_workers = prompt_positive_int_plain(
        f"Parallel Worker [Default {default_workers}]: ",
        default_workers,
    )

    if mode == "duel":
        games = prompt_positive_int_plain("Anzahl Spiele [Default 100]: ", 100)
        schedule = build_duel_schedule(games)
        games_per_pair = games
    else:
        while True:
            games_per_pair = prompt_positive_int_plain(
                "Spiele pro Paarung [Default 8]: ",
                8,
            )
            if games_per_pair >= 2:
                break
            print("Bitte mindestens 2 Spiele pro Paarung wählen.")
        schedule = build_tournament_schedule(len(selected), games_per_pair)

    bots = [BotStats(name=bot.name, path=bot.path) for bot in selected]
    config = RunConfig(
        mode=mode,
        games_per_pair=games_per_pair,
        timeout_sec=MAX_GAME_TIME,
        base_port=BASE_PORT,
        parallel_workers=resolve_parallel_workers(mode, parallel_workers),
    )
    now = utc_now_iso()
    state = RunState(
        version=STATE_VERSION,
        mode=mode,
        run_id=make_run_id(mode, now),
        config=config,
        bots=bots,
        schedule=schedule,
        next_game_idx=0,
        started_at=now,
        updated_at=now,
        completed=False,
        game_records=[],
    )
    save_state(state)
    return state


def run_menu_plain() -> RunState | None:
    loaded_state = load_state()
    resumable = is_resumable(loaded_state)

    while True:
        print("\nDUEL/Tournament TUI")
        print("  1 Neuer Lauf")
        if resumable:
            print("  2 Resume")
            print("  3 Ergebnis-Viewer")
            print("  4 Beenden")
            choice = prompt_choice_plain("Auswahl [1/2/3/4]: ", {"1", "2", "3", "4"})
            if choice == "1":
                return build_new_state_plain()
            if choice == "2":
                return loaded_state
            if choice == "3":
                show_results_viewer_plain()
                continue
            return None

        print("  2 Ergebnis-Viewer")
        print("  3 Beenden")
        choice = prompt_choice_plain("Auswahl [1/2/3]: ", {"1", "2", "3"})
        if choice == "1":
            return build_new_state_plain()
        if choice == "2":
            show_results_viewer_plain()
            continue
        return None


def curses_color_map() -> dict[str, int]:
    colors = {
        "normal": 0,
        "cyan": 0,
        "blue": 0,
        "green": 0,
        "yellow": 0,
        "red": 0,
        "magenta": 0,
        "white": 0,
        "dim": curses.A_DIM,
    }
    if not curses.has_colors():
        return colors

    curses.start_color()
    try:
        curses.use_default_colors()
    except curses.error:
        pass

    pairs = [
        ("cyan", curses.COLOR_CYAN),
        ("blue", curses.COLOR_BLUE),
        ("green", curses.COLOR_GREEN),
        ("yellow", curses.COLOR_YELLOW),
        ("red", curses.COLOR_RED),
        ("magenta", curses.COLOR_MAGENTA),
        ("white", curses.COLOR_WHITE),
    ]
    for idx, (name, fg) in enumerate(pairs, start=1):
        try:
            curses.init_pair(idx, fg, -1)
            colors[name] = curses.color_pair(idx)
        except curses.error:
            colors[name] = 0
    return colors


def curses_addnstr(win: curses.window, y: int, x: int, text: str, attr: int = 0) -> None:
    height, width = win.getmaxyx()
    if y < 0 or y >= height:
        return
    if x >= width:
        return
    if x < 0:
        text = text[-x:]
        x = 0
    max_len = width - x - 1
    if max_len <= 0:
        return
    try:
        win.addnstr(y, x, text, max_len, attr)
    except curses.error:
        pass


def curses_box(
    stdscr: curses.window,
    y: int,
    x: int,
    height: int,
    width: int,
    title: str,
    title_attr: int = 0,
) -> curses.window | None:
    if height < 3 or width < 8:
        return None
    try:
        panel = stdscr.derwin(height, width, y, x)
        panel.box()
        curses_addnstr(panel, 0, 2, f" {title} ", curses.A_BOLD | title_attr)
        return panel
    except curses.error:
        return None


def curses_prompt_input(
    stdscr: curses.window,
    title: str,
    lines: list[str],
    prompt: str,
    default: str | None = None,
    autocomplete: Callable[[str], list[str]] | None = None,
) -> str | None:
    buffer = ""
    completion_seed = ""
    completion_matches: list[str] = []
    completion_index = 0
    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        curses_addnstr(
            stdscr,
            0,
            0,
            truncate_text(f"DUEL CONTROL CENTER | {title}", width - 1),
            curses.A_BOLD,
        )

        usable_lines = max(1, height - 7)
        start_idx = max(0, len(lines) - usable_lines)
        for row, line in enumerate(lines[start_idx : start_idx + usable_lines], start=2):
            curses_addnstr(stdscr, row, 0, truncate_text(line, width - 1))

        prompt_line = min(height - 3, 2 + usable_lines)
        if default is not None:
            prompt_full = f"{prompt} [{default}]: "
        else:
            prompt_full = f"{prompt}: "
        curses_addnstr(stdscr, prompt_line, 0, truncate_text(prompt_full, width - 1), curses.A_BOLD)
        value = buffer if buffer else ""
        curses_addnstr(stdscr, prompt_line, min(len(prompt_full), max(0, width - 1)), truncate_text(value, max(0, width - len(prompt_full) - 1)))
        hint = "Enter bestätigen | Esc zurück | Backspace löschen | Tab autocomplete"
        curses_addnstr(stdscr, height - 1, 0, truncate_text(hint, width - 1), curses.A_DIM)
        stdscr.refresh()

        try:
            curses.curs_set(1)
        except curses.error:
            pass

        key = stdscr.getch()
        if key in (10, 13, curses.KEY_ENTER):
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            if buffer:
                return buffer.strip()
            if default is not None:
                return default
            return ""
        if key == 27:
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            return None
        if key in (curses.KEY_BACKSPACE, 127, 8):
            buffer = buffer[:-1]
            completion_matches = []
            completion_seed = ""
            continue
        if key == 9 and autocomplete is not None:
            if buffer != completion_seed:
                completion_seed = buffer
                completion_matches = autocomplete(buffer)
                completion_index = 0
            elif completion_matches:
                completion_index = (completion_index + 1) % len(completion_matches)
            if completion_matches:
                buffer = completion_matches[completion_index]
            continue
        if key == curses.KEY_RESIZE:
            continue
        if 32 <= key <= 126:
            if len(buffer) < 256:
                buffer += chr(key)
                completion_matches = []
                completion_seed = ""


def curses_select_menu(
    stdscr: curses.window,
    title: str,
    options: list[str],
    info_lines: list[str] | None = None,
    initial_index: int = 0,
) -> int | None:
    if not options:
        return None
    index = max(0, min(initial_index, len(options) - 1))
    scroll = 0

    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        curses_addnstr(
            stdscr,
            0,
            0,
            truncate_text(f"DUEL CONTROL CENTER | {title}", width - 1),
            curses.A_BOLD,
        )

        row = 2
        if info_lines:
            for line in info_lines:
                if row >= height - 3:
                    break
                curses_addnstr(stdscr, row, 0, truncate_text(line, width - 1))
                row += 1
            row += 1

        available = max(1, height - row - 2)
        if index < scroll:
            scroll = index
        if index >= scroll + available:
            scroll = index - available + 1

        for visual_row in range(available):
            option_idx = scroll + visual_row
            if option_idx >= len(options):
                break
            marker = "▸" if option_idx == index else " "
            line = f"{marker} {options[option_idx]}"
            attr = curses.A_REVERSE if option_idx == index else 0
            curses_addnstr(stdscr, row + visual_row, 0, truncate_text(line, width - 1), attr)

        hint = "↑/↓ wählen | Enter bestätigen | Esc zurück"
        curses_addnstr(stdscr, height - 1, 0, truncate_text(hint, width - 1), curses.A_DIM)
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            index = (index - 1) % len(options)
        elif key in (curses.KEY_DOWN, ord("j")):
            index = (index + 1) % len(options)
        elif key in (10, 13, curses.KEY_ENTER):
            return index
        elif key == 27:
            return None


def curses_select_bots(
    stdscr: curses.window,
    candidates: list[BotSpec],
    mode: str,
) -> list[int] | None:
    selected: set[int] = set()
    index = 0
    scroll = 0
    status = "Space markieren | A: add custom | R: remove custom | Enter bestätigen"

    def remove_selected_custom(idx: int) -> str:
        nonlocal index, selected
        if idx < 0 or idx >= len(candidates):
            return "Kein Bot ausgewählt."
        bot = candidates[idx]
        if not bot.is_custom:
            return "Nur Custom-Pfade können entfernt werden."

        path = str(Path(bot.path).resolve())
        removed = remove_custom_bot_paths([path])
        if bot.is_discovered:
            bot.is_custom = False
            return "Custom-Markierung entfernt (Auto-Discovery bleibt)."

        del candidates[idx]
        new_selected: set[int] = set()
        for old in selected:
            if old == idx:
                continue
            if old > idx:
                new_selected.add(old - 1)
            else:
                new_selected.add(old)
        selected = new_selected
        if candidates:
            index = min(index, len(candidates) - 1)
        else:
            index = 0
        return "Custom-Pfad entfernt." if removed > 0 else "Custom-Pfad war nicht gespeichert."

    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        curses_addnstr(stdscr, 0, 0, truncate_text("DUEL CONTROL CENTER | Bot Selection", width - 1), curses.A_BOLD)

        mode_text = "Duel: exakt 2 Bots wählen." if mode == "duel" else "Turnier: mindestens 2 Bots wählen."
        curses_addnstr(stdscr, 2, 0, truncate_text(mode_text, width - 1))
        curses_addnstr(stdscr, 3, 0, truncate_text(f"Ausgewählt: {len(selected)}", width - 1))

        row = 5
        available = max(1, height - row - 2)
        if candidates:
            if index < scroll:
                scroll = index
            if index >= scroll + available:
                scroll = index - available + 1

        for visual_row in range(available):
            candidate_idx = scroll + visual_row
            if candidate_idx >= len(candidates):
                break
            bot = candidates[candidate_idx]
            mark = "[x]" if candidate_idx in selected else "[ ]"
            rel = rel_path_str(bot.path)
            source_flag = "C" if bot.is_custom else "A"
            line = (
                f"{mark} {candidate_idx + 1:>3} [{source_flag}] "
                f"{truncate_text(bot.name, 26):<26} {rel}"
            )
            attr = curses.A_REVERSE if candidate_idx == index else 0
            curses_addnstr(stdscr, row + visual_row, 0, truncate_text(line, width - 1), attr)
        if not candidates:
            curses_addnstr(
                stdscr,
                row,
                0,
                truncate_text("Keine Bots in Liste. Drücke 'a' für Custom-Pfad.", width - 1),
                curses.A_DIM,
            )

        curses_addnstr(stdscr, height - 2, 0, truncate_text(status, width - 1))
        hint = "↑/↓ bewegen | Space toggeln | A add | R remove | Enter | Esc"
        curses_addnstr(stdscr, height - 1, 0, truncate_text(hint, width - 1), curses.A_DIM)
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")) and candidates:
            index = (index - 1) % len(candidates)
        elif key in (curses.KEY_DOWN, ord("j")) and candidates:
            index = (index + 1) % len(candidates)
        elif key == ord(" ") and candidates:
            if index in selected:
                selected.remove(index)
            else:
                selected.add(index)
        elif key in (ord("a"), ord("A")):
            custom_path = curses_prompt_input(
                stdscr,
                "Custom Bot Path",
                [
                    "Pfad zu einer Bot-Datei (*.py) eingeben.",
                    "Relative oder absolute Pfade sind erlaubt.",
                    "Tab vervollständigt Pfade.",
                ],
                "Pfad",
                None,
                autocomplete=path_autocomplete_suggestions,
            )
            if custom_path is None:
                status = "Custom-Pfad abgebrochen."
                continue
            new_idx, message = add_custom_bot_candidate(candidates, custom_path)
            status = message
            if new_idx is not None:
                index = new_idx
            continue
        elif key in (ord("r"), ord("R")):
            status = remove_selected_custom(index)
            continue
        elif key in (10, 13, curses.KEY_ENTER):
            if mode == "duel" and len(selected) != 2:
                status = "Duel benötigt exakt 2 Bots."
                continue
            if mode == "tournament" and len(selected) < 2:
                status = "Turnier benötigt mindestens 2 Bots."
                continue
            return sorted(selected)
        elif key == 27:
            return None


def curses_message(
    stdscr: curses.window,
    title: str,
    lines: list[str],
    wait_for_key: bool = True,
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    curses_addnstr(stdscr, 0, 0, truncate_text(f"DUEL CONTROL CENTER | {title}", width - 1), curses.A_BOLD)
    usable = max(1, height - 4)
    for row, line in enumerate(lines[:usable], start=2):
        curses_addnstr(stdscr, row, 0, truncate_text(line, width - 1))
    if wait_for_key:
        hint = "Beliebige Taste..."
        curses_addnstr(stdscr, min(height - 1, 2 + usable), 0, truncate_text(hint, width - 1), curses.A_DIM)
    stdscr.refresh()
    if wait_for_key:
        stdscr.getch()


def curses_scrollable_lines_view(
    stdscr: curses.window,
    title: str,
    lines: list[str],
) -> None:
    top = 0
    x_offset = 0
    safe_lines = lines if lines else ["(leer)"]
    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        body_h = max(1, height - 3)
        max_top = max(0, len(safe_lines) - body_h)
        top = min(max(0, top), max_top)

        header = f"DUEL CONTROL CENTER | {title} | lines={len(safe_lines)} | x={x_offset}"
        curses_addnstr(stdscr, 0, 0, truncate_text(header, width - 1), curses.A_BOLD)

        for row in range(body_h):
            idx = top + row
            if idx >= len(safe_lines):
                break
            line = safe_lines[idx]
            clipped = line[x_offset:] if x_offset < len(line) else ""
            curses_addnstr(stdscr, row + 1, 0, truncate_text(clipped, width - 1))

        hint = "↑/↓ PgUp/PgDn Home/End scroll | ←/→ horizontal | Esc/q zurück"
        curses_addnstr(stdscr, height - 1, 0, truncate_text(hint, width - 1), curses.A_DIM)
        stdscr.refresh()

        key = stdscr.getch()
        if key in (27, ord("q"), ord("Q")):
            return
        if key in (curses.KEY_UP, ord("k")):
            top = max(0, top - 1)
            continue
        if key in (curses.KEY_DOWN, ord("j")):
            top = min(max_top, top + 1)
            continue
        if key == curses.KEY_PPAGE:
            top = max(0, top - body_h)
            continue
        if key == curses.KEY_NPAGE:
            top = min(max_top, top + body_h)
            continue
        if key == curses.KEY_HOME:
            top = 0
            continue
        if key == curses.KEY_END:
            top = max_top
            continue
        if key == curses.KEY_LEFT:
            x_offset = max(0, x_offset - 4)
            continue
        if key == curses.KEY_RIGHT:
            x_offset += 4
            continue


def result_file_label(path: Path, payload: dict[str, Any] | None) -> str:
    if payload is None:
        return f"{path.parent.name} | <ungültig>"
    mode = str(payload.get("mode", "unknown"))
    completed_at = str(payload.get("completed_at", "unknown"))
    total_games = payload.get("total_games", "?")
    return f"{path.parent.name} | {mode} | games={total_games} | completed={completed_at}"


def show_results_viewer_curses(stdscr: curses.window) -> None:
    files = available_result_files()
    if not files:
        curses_message(stdscr, "Ergebnis-Viewer", ["Keine Ergebnisdatei gefunden."], wait_for_key=True)
        return

    payload_cache: dict[Path, dict[str, Any] | None] = {path: load_results_payload(path) for path in files}
    labels = [result_file_label(path, payload_cache.get(path)) for path in files]

    selected_idx = 0
    while True:
        choice = curses_select_menu(
            stdscr,
            "Ergebnis-History",
            labels,
            info_lines=["Run auswählen und Enter drücken."],
            initial_index=selected_idx,
        )
        if choice is None:
            return

        selected_idx = choice
        selected = files[choice]
        payload = payload_cache.get(selected)
        if payload is None:
            curses_message(stdscr, "Fehler", [f"Konnte {selected} nicht lesen."], wait_for_key=True)
            continue

        while True:
            detail_choice = curses_select_menu(
                stdscr,
                "Run Details",
                ["Summary", "Games", "Error-Games", "Logs", "Zurück"],
                info_lines=[str(selected)],
            )
            if detail_choice is None or detail_choice == 4:
                break
            if detail_choice == 0:
                curses_scrollable_lines_view(
                    stdscr,
                    "Summary",
                    build_results_summary_lines(selected, payload),
                )
                continue
            if detail_choice == 1:
                curses_scrollable_lines_view(
                    stdscr,
                    "Games",
                    build_results_records_lines(payload, errors_only=False),
                )
                continue
            if detail_choice == 2:
                curses_scrollable_lines_view(
                    stdscr,
                    "Error-Games",
                    build_results_records_lines(payload, errors_only=True),
                )
                continue

            logs = available_log_files_for_summary(selected)
            if not logs:
                curses_message(stdscr, "Logs", ["Keine Logs gefunden."], wait_for_key=True)
                continue

            log_idx = 0
            while True:
                log_choice = curses_select_menu(
                    stdscr,
                    "Logs",
                    [str(path.relative_to(selected.parent)) for path in logs],
                    info_lines=["Log auswählen"],
                    initial_index=log_idx,
                )
                if log_choice is None:
                    break
                log_idx = log_choice
                selected_log = logs[log_choice]
                curses_scrollable_lines_view(
                    stdscr,
                    f"Log: {selected_log.name}",
                    build_log_preview_lines(selected_log, max_lines=5000),
                )


def build_state_from_selection(
    mode: str,
    selected: list[BotSpec],
    duel_games: int,
    tournament_games_per_pair: int,
    parallel_workers: int,
) -> RunState:
    if mode == "duel":
        schedule = build_duel_schedule(duel_games)
        games_per_pair = duel_games
    else:
        games_per_pair = max(2, tournament_games_per_pair)
        schedule = build_tournament_schedule(len(selected), games_per_pair)

    bots = [BotStats(name=bot.name, path=bot.path) for bot in selected]
    config = RunConfig(
        mode=mode,
        games_per_pair=games_per_pair,
        timeout_sec=MAX_GAME_TIME,
        base_port=BASE_PORT,
        parallel_workers=resolve_parallel_workers(mode, parallel_workers),
    )
    now = utc_now_iso()
    return RunState(
        version=STATE_VERSION,
        mode=mode,
        run_id=make_run_id(mode, now),
        config=config,
        bots=bots,
        schedule=schedule,
        next_game_idx=0,
        started_at=now,
        updated_at=now,
        completed=False,
        game_records=[],
    )


def run_menu_curses() -> RunState | None:
    result_state: RunState | None = None

    def _menu(stdscr: curses.window) -> None:
        nonlocal result_state
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        curses_color_map()

        loaded_state = load_state()
        resumable = is_resumable(loaded_state)

        while True:
            menu_lines = ["Neuer Lauf"]
            if resumable:
                menu_lines.append("Resume")
                menu_lines.append("Ergebnis-Viewer")
                menu_lines.append("Beenden")
                choice = curses_select_menu(
                    stdscr,
                    "Main Menu",
                    menu_lines,
                    info_lines=["Mit Pfeiltasten navigieren."],
                )
                if choice is None or choice == 3:
                    result_state = None
                    return
                if choice == 0:
                    break
                if choice == 1:
                    result_state = loaded_state
                    return
                if choice == 2:
                    show_results_viewer_curses(stdscr)
                    continue
            else:
                menu_lines.append("Ergebnis-Viewer")
                menu_lines.append("Beenden")
                choice = curses_select_menu(
                    stdscr,
                    "Main Menu",
                    menu_lines,
                    info_lines=["Mit Pfeiltasten navigieren."],
                )
                if choice is None or choice == 2:
                    result_state = None
                    return
                if choice == 0:
                    break
                if choice == 1:
                    show_results_viewer_curses(stdscr)
                    continue

        curses_message(stdscr, "Bot Discovery", ["Suche Bots rekursiv..."], wait_for_key=False)
        candidates = discover_bots()
        loaded_custom = load_saved_custom_candidates(candidates)
        if loaded_custom > 0:
            curses_message(
                stdscr,
                "Custom Paths",
                [f"{loaded_custom} gespeicherte Custom-Pfade geladen."],
                wait_for_key=False,
            )
        if len(candidates) == 0:
            curses_message(
                stdscr,
                "Hinweis",
                [
                    "Keine Bots automatisch gefunden.",
                    "Du kannst im nächsten Schritt Custom-Pfade hinzufügen (Taste A).",
                ],
                wait_for_key=True,
            )

        mode_choice = curses_select_menu(
            stdscr,
            "Mode",
            [
                "Duel (genau 2 Bots)",
                "Turnier (>=2 Bots, Spiele pro Paarung frei wählbar)",
            ],
            info_lines=["Esc bricht den neuen Lauf ab."],
        )
        if mode_choice is None:
            result_state = None
            return
        mode = "duel" if mode_choice == 0 else "tournament"

        selected_indices = curses_select_bots(stdscr, candidates, mode)
        if selected_indices is None:
            result_state = None
            return
        selected = [candidates[idx] for idx in selected_indices]

        duel_games = 100
        tournament_games_per_pair = 8
        default_workers = resolve_parallel_workers(mode)
        parallel_workers = default_workers
        if mode == "duel":
            while True:
                raw_games = curses_prompt_input(
                    stdscr,
                    "Duel Settings",
                    ["Anzahl Spiele für Duel festlegen."],
                    "Spiele",
                    "100",
                )
                if raw_games is None:
                    result_state = None
                    return
                if raw_games.isdigit() and int(raw_games) > 0:
                    duel_games = int(raw_games)
                    break
                curses_message(stdscr, "Fehler", ["Bitte positive ganze Zahl eingeben."], wait_for_key=True)
        else:
            while True:
                raw_tournament_games = curses_prompt_input(
                    stdscr,
                    "Tournament Settings",
                    [
                        "Spiele pro Paarung festlegen (mindestens 2).",
                        "Empfehlung für längere Tests: 8 oder mehr.",
                    ],
                    "Spiele pro Paarung",
                    "8",
                )
                if raw_tournament_games is None:
                    result_state = None
                    return
                if raw_tournament_games.isdigit() and int(raw_tournament_games) >= 2:
                    tournament_games_per_pair = int(raw_tournament_games)
                    break
                curses_message(
                    stdscr,
                    "Fehler",
                    ["Bitte eine ganze Zahl >= 2 eingeben."],
                    wait_for_key=True,
                )

        while True:
            raw_workers = curses_prompt_input(
                stdscr,
                "Parallel Workers",
                [
                    "Mehrere Spiele parallel ausführen.",
                    "Hinweis: Höhere Werte nutzen mehr CPU/RAM.",
                ],
                "Worker",
                str(default_workers),
            )
            if raw_workers is None:
                result_state = None
                return
            if raw_workers.isdigit() and int(raw_workers) > 0:
                parallel_workers = int(raw_workers)
                break
            curses_message(stdscr, "Fehler", ["Bitte positive ganze Zahl eingeben."], wait_for_key=True)

        result_state = build_state_from_selection(
            mode,
            selected,
            duel_games,
            tournament_games_per_pair,
            parallel_workers,
        )
        save_state(result_state)

    curses.wrapper(_menu)
    return result_state


def read_log_tail_lines(path: Path | None, max_lines: int) -> list[str]:
    if path is None:
        return ["Kein aktives Spiel-Log."]
    content = read_text_if_exists(path)
    if not content:
        return [f"{path.name}: (noch keine Ausgabe)"]
    lines = [ANSI_ESCAPE_RE.sub("", line) for line in content.splitlines()]
    if not lines:
        return [f"{path.name}: (leer)"]
    return lines[-max_lines:]


def draw_curses_dashboard(
    stdscr: curses.window,
    state: RunState,
    current_match: MatchSpec | None,
    last_record: GameRecord | None,
    status_line: str,
    colors: dict[str, int],
    live_log_path: Path | None = None,
    log_x_offset: int = 0,
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    if height < 16 or width < 80:
        fallback_lines = render_plain_dashboard(state, current_match, last_record).splitlines()
        for row, line in enumerate(fallback_lines[: max(1, height - 2)]):
            curses_addnstr(stdscr, row, 0, truncate_text(line, width - 1))
        curses_addnstr(stdscr, max(0, height - 1), 0, "Terminal zu klein (min 80x16).", colors["yellow"])
        stdscr.refresh()
        return

    total_games = len(state.schedule)
    done = state.next_game_idx
    started_at = parse_iso_datetime(state.started_at)
    elapsed = 0.0
    if started_at is not None:
        elapsed = max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds())
    speed = done / max(elapsed, 1e-9) if done > 0 else 0.0
    eta = ((total_games - done) / max(speed, 1e-9)) if speed > 0 and done < total_games else 0.0

    header = (
        f"DUEL CONTROL CENTER | mode={state.mode.upper()} | "
        f"state={'DONE' if done >= total_games else 'RUNNING'} | "
        f"progress={done}/{total_games} | speed={speed:.2f} g/s | "
        f"elapsed={format_duration(elapsed)} | eta={format_duration(eta)}"
    )
    curses_addnstr(stdscr, 0, 0, truncate_text(header, width - 1), colors["cyan"] | curses.A_BOLD)

    body_y = 2
    footer_h = 3
    body_h = height - body_y - footer_h
    left_w = int(width * 0.58)
    left_w = max(44, min(left_w, width - 30))
    right_x = left_w + 1
    right_w = width - right_x

    rank_box = curses_box(stdscr, body_y, 0, body_h, left_w, "Rankings", colors["cyan"])
    if rank_box is not None:
        ranked = rank_bots(state.bots)
        name_w = max(8, left_w - 37)
        header_row = f"{'#':>2} {'Bot':<{name_w}} {'Elo':>7} {'W':>3} {'D':>3} {'L':>3} {'E':>3} {'T':>3} {'Sc':>6}"
        curses_addnstr(rank_box, 1, 1, truncate_text(header_row, left_w - 2), curses.A_BOLD)
        max_rows = max(0, body_h - 3)
        for row, bot in enumerate(ranked[:max_rows], start=2):
            line = (
                f"{row - 1:>2} {truncate_text(bot.name, name_w):<{name_w}} {bot.elo:>7.1f} "
                f"{bot.wins:>3} {bot.draws:>3} {bot.losses:>3} {bot.errors:>3} {bot.timeouts:>3} {bot.score:>6.3f}"
            )
            curses_addnstr(rank_box, row, 1, truncate_text(line, left_w - 2))

    overview_h = min(12, body_h - 8)
    overview_h = max(7, overview_h)
    live_h = min(8, body_h - overview_h - 3)
    live_h = max(6, live_h)
    log_h = max(4, body_h - overview_h - live_h)

    overview_box = curses_box(stdscr, body_y, right_x, overview_h, right_w, "Overview", colors["blue"] if "blue" in colors else colors["white"])
    if overview_box is not None:
        draws = sum(bot.draws for bot in state.bots) // 2
        errors = sum(bot.errors for bot in state.bots) // 2
        timeouts = sum(bot.timeouts for bot in state.bots) // 2
        decisive = done - draws - errors
        lines = [
            f"Bots      : {len(state.bots)}",
            f"Workers   : {effective_worker_count(state)}",
            f"Timeout   : {state.config.timeout_sec}s",
            f"Base Port : {state.config.base_port}",
            "",
            meter_line_plain("Games", done, total_games, max(10, right_w - 20)),
            meter_line_plain("Decisive", decisive, max(done, 1), max(10, right_w - 20)),
            meter_line_plain("Draws", draws, max(done, 1), max(10, right_w - 20)),
            meter_line_plain("Errors", errors, max(done, 1), max(10, right_w - 20)),
            meter_line_plain("Timeouts", timeouts, max(done, 1), max(10, right_w - 20)),
        ]
        for row, line in enumerate(lines[: max(0, overview_h - 2)], start=1):
            curses_addnstr(overview_box, row, 1, truncate_text(line, right_w - 2))

    live_y = body_y + overview_h
    live_box = curses_box(stdscr, live_y, right_x, live_h, right_w, "Live Match", colors["magenta"])
    if live_box is not None:
        if current_match is None:
            current_line = "Kein laufendes Spiel."
        else:
            one = state.bots[current_match.bot_one_idx].name
            two = state.bots[current_match.bot_two_idx].name
            current_line = f"Aktuell: {one} vs {two}"
        live_lines = ["Current:", current_line, "", f"Letztes: {format_record_summary(last_record)}"]
        for row, line in enumerate(live_lines[: max(0, live_h - 2)], start=1):
            curses_addnstr(live_box, row, 1, truncate_text(line, right_w - 2))

    log_y = live_y + live_h
    log_title = "Live Log"
    if live_log_path is not None:
        log_title = f"Live Log ({live_log_path.name})"
    log_box = curses_box(stdscr, log_y, right_x, log_h, right_w, log_title, colors["green"])
    if log_box is not None:
        visible_rows = max(1, log_h - 2)
        tail_lines = read_log_tail_lines(live_log_path, max_lines=visible_rows * 6)
        for row, line in enumerate(tail_lines[-visible_rows:], start=1):
            clipped = line[log_x_offset:] if log_x_offset < len(line) else ""
            curses_addnstr(log_box, row, 1, truncate_text(clipped, right_w - 2))

    progress_ratio = done / max(total_games, 1)
    progress_w = max(10, width - 28)
    filled = int(progress_ratio * progress_w)
    progress_bar = "█" * filled + "░" * (progress_w - filled)
    footer = f"Progress [{progress_bar}] {progress_ratio * 100:5.1f}%"
    hints = f"{status_line} | q: stop | \u2190/\u2192: log horizontal scroll"
    curses_addnstr(stdscr, height - 2, 0, truncate_text(footer, width - 1), colors["cyan"])
    curses_addnstr(stdscr, height - 1, 0, truncate_text(hints, width - 1), colors["dim"])
    stdscr.refresh()


def run_state_curses(state: RunState) -> Path | None:
    run_dir = ensure_run_dirs(state)
    results_path: Path | None = None
    aborted = False
    stop_after_current = False
    log_x_offset = 0

    def _runner(stdscr: curses.window) -> None:
        nonlocal results_path, aborted, stop_after_current, log_x_offset
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.nodelay(True)
        stdscr.timeout(100)
        colors = curses_color_map()

        total_games = len(state.schedule)
        last_record = state.game_records[-1] if state.game_records else None

        def _current_log_path(match: MatchSpec | None) -> Path | None:
            if match is None:
                return None
            server_log_path, _, _ = game_log_paths(run_dir, match.game_idx)
            return server_log_path

        def _handle_key(key: int) -> None:
            nonlocal stop_after_current, log_x_offset
            if key in (ord("q"), ord("Q")):
                stop_after_current = True
            elif key == curses.KEY_LEFT:
                log_x_offset = max(0, log_x_offset - 4)
            elif key == curses.KEY_RIGHT:
                log_x_offset += 4

        while state.next_game_idx < total_games:
            workers = min(
                effective_worker_count(state),
                total_games - state.next_game_idx,
            )
            batch_start = state.next_game_idx
            batch_end = min(total_games, batch_start + workers)
            batch_matches = state.schedule[batch_start:batch_end]
            current_match = batch_matches[0] if batch_matches else None

            status = (
                f"stop requested | workers={workers}"
                if stop_after_current
                else f"running | workers={workers}"
            )
            current_log_path = _current_log_path(current_match)
            draw_curses_dashboard(
                stdscr,
                state,
                current_match,
                last_record,
                status,
                colors,
                live_log_path=current_log_path,
                log_x_offset=log_x_offset,
            )
            _handle_key(stdscr.getch())

            def _poll(completed: int, pending: int, total_batch: int) -> None:
                _handle_key(stdscr.getch())
                status_line = (
                    f"stop requested | workers={workers} | done={completed}/{total_batch}"
                    if stop_after_current
                    else f"running | workers={workers} | done={completed}/{total_batch}"
                )
                draw_curses_dashboard(
                    stdscr,
                    state,
                    current_match,
                    last_record,
                    status_line,
                    colors,
                    live_log_path=current_log_path,
                    log_x_offset=log_x_offset,
                )

            outcomes = run_match_batch(
                state=state,
                matches=batch_matches,
                workers=workers,
                run_dir=run_dir,
                poll_hook=_poll,
            )

            for outcome in outcomes:
                last_record = apply_match_outcome(state, outcome)
                status_line = (
                    f"stop requested | workers={workers}"
                    if stop_after_current
                    else f"running | workers={workers}"
                )
                draw_curses_dashboard(
                    stdscr,
                    state,
                    None,
                    last_record,
                    status_line,
                    colors,
                    live_log_path=None,
                    log_x_offset=log_x_offset,
                )

            if stop_after_current:
                aborted = True
                break

        if aborted:
            save_state(state)
            save_results(state, update_latest=False)
            draw_curses_dashboard(
                stdscr,
                state,
                None,
                last_record,
                "stopped",
                colors,
                live_log_path=None,
                log_x_offset=log_x_offset,
            )
            stdscr.nodelay(False)
            stdscr.timeout(-1)
            stdscr.getch()
            return

        state.completed = True
        save_state(state)
        results_path = save_results(state)
        draw_curses_dashboard(
            stdscr,
            state,
            None,
            last_record,
            "completed",
            colors,
            live_log_path=None,
            log_x_offset=log_x_offset,
        )
        stdscr.nodelay(False)
        stdscr.timeout(-1)
        stdscr.getch()

    curses.wrapper(_runner)
    return results_path


def get_terminal_width(default: int = 120) -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return default


def truncate_text(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return text[:1]
    return text[: width - 1] + "…"


def meter_line_plain(label: str, current: int, total: int, width: int = 28) -> str:
    bounded_total = max(total, 1)
    ratio = min(max(current / bounded_total, 0.0), 1.0)
    filled = int(ratio * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"{label:<11} {bar} {ratio * 100:5.1f}%"


def boxed(title: str, lines: list[str], width: int) -> list[str]:
    width = max(24, width)
    inner = width - 2
    title_token = f" {title} "
    top = "┌" + title_token + "─" * max(0, inner - len(title_token)) + "┐"
    body = [f"│{truncate_text(line, inner).ljust(inner)}│" for line in lines]
    bottom = "└" + "─" * inner + "┘"
    return [top, *body, bottom]


def render_plain_dashboard(
    state: RunState,
    current_match: MatchSpec | None,
    last_record: GameRecord | None,
) -> str:
    width = get_terminal_width()
    total_games = len(state.schedule)
    done = state.next_game_idx
    started_at = parse_iso_datetime(state.started_at)
    elapsed = 0.0
    if started_at is not None:
        elapsed = max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds())
    speed = done / max(elapsed, 1e-9) if done > 0 else 0.0
    eta = ((total_games - done) / max(speed, 1e-9)) if speed > 0 and done < total_games else 0.0

    header = (
        f"DUEL CONTROL CENTER | mode={state.mode.upper()} | "
        f"state={'DONE' if done >= total_games else 'RUNNING'} | "
        f"progress={done}/{total_games} | speed={speed:.2f} g/s | "
        f"elapsed={format_duration(elapsed)} | eta={format_duration(eta)}"
    )
    lines: list[str] = [truncate_text(header, width), ""]

    ranked = rank_bots(state.bots)
    rank_lines = [f"{'#':>2} {'Bot':<28} {'Elo':>7} {'W':>3} {'D':>3} {'L':>3} {'E':>3} {'T':>3} {'Sc':>6}"]
    for idx, bot in enumerate(ranked, start=1):
        rank_lines.append(
            f"{idx:>2} {truncate_text(bot.name, 28):<28} {bot.elo:>7.1f} "
            f"{bot.wins:>3} {bot.draws:>3} {bot.losses:>3} {bot.errors:>3} {bot.timeouts:>3} {bot.score:>6.3f}"
        )
    lines.extend(boxed("Rankings", rank_lines, width))
    lines.append("")

    draws = sum(bot.draws for bot in state.bots) // 2
    errors = sum(bot.errors for bot in state.bots) // 2
    timeouts = sum(bot.timeouts for bot in state.bots) // 2
    decisive = done - draws - errors
    overview = [
        f"Bots      : {len(state.bots)}",
        f"Workers   : {effective_worker_count(state)}",
        f"Timeout   : {state.config.timeout_sec}s",
        f"Base Port : {state.config.base_port}",
        "",
        meter_line_plain("Games", done, total_games),
        meter_line_plain("Decisive", decisive, max(done, 1)),
        meter_line_plain("Draws", draws, max(done, 1)),
        meter_line_plain("Errors", errors, max(done, 1)),
        meter_line_plain("Timeouts", timeouts, max(done, 1)),
    ]
    lines.extend(boxed("Overview", overview, width))
    lines.append("")

    if current_match is None:
        match_line = "Kein laufendes Spiel."
    else:
        one = state.bots[current_match.bot_one_idx].name
        two = state.bots[current_match.bot_two_idx].name
        match_line = f"Aktuell: {one} vs {two}"
    live_lines = [match_line, "", f"Letztes Ergebnis: {format_record_summary(last_record)}"]
    lines.extend(boxed("Live Match", live_lines, width))
    lines.append("")

    event_lines: list[str] = []
    for rec in reversed(state.game_records[-8:]):
        if rec.result == RESULT_WIN_ONE:
            tag = "WIN "
            winner = rec.bot_one
        elif rec.result == RESULT_WIN_TWO:
            tag = "WIN "
            winner = rec.bot_two
        elif rec.result == RESULT_DRAW:
            tag = "DRAW"
            winner = "-"
        else:
            tag = "ERR "
            winner = "-"
        event_lines.append(
            f"#{rec.game_idx + 1:03d} {tag} {rec.bot_one} vs {rec.bot_two} "
            f"(winner: {winner}, {rec.duration_sec:.1f}s, {rec.reason})"
        )
    if not event_lines:
        event_lines.append("Noch keine Ereignisse.")
    lines.extend(boxed("Event Log", event_lines, width))
    return "\n".join(lines)


def clear_screen_plain() -> None:
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")
    else:
        print("\n")


def run_state_plain(state: RunState) -> Path:
    run_dir = ensure_run_dirs(state)
    total_games = len(state.schedule)
    last_record = state.game_records[-1] if state.game_records else None

    while state.next_game_idx < total_games:
        workers = min(
            effective_worker_count(state),
            total_games - state.next_game_idx,
        )
        batch_start = state.next_game_idx
        batch_end = min(total_games, batch_start + workers)
        batch_matches = state.schedule[batch_start:batch_end]
        current_match = batch_matches[0] if batch_matches else None

        clear_screen_plain()
        print(render_plain_dashboard(state, current_match, last_record))

        outcomes = run_match_batch(
            state=state,
            matches=batch_matches,
            workers=workers,
            run_dir=run_dir,
            poll_hook=None,
        )

        for outcome in outcomes:
            last_record = apply_match_outcome(state, outcome)

    state.completed = True
    save_state(state)
    results_path = save_results(state)

    clear_screen_plain()
    print(render_plain_dashboard(state, None, last_record))
    return results_path


def print_final_table_plain(state: RunState) -> None:
    ranked = rank_bots(state.bots)
    print("\nFinale Rangliste")
    print("-" * 90)
    print(f"{'#':>2} {'Bot':<28} {'Elo':>7} {'W':>4} {'D':>4} {'L':>4} {'E':>4} {'Games':>6} {'Score':>8}")
    print("-" * 90)
    for idx, bot in enumerate(ranked, start=1):
        print(
            f"{idx:>2} {truncate_text(bot.name, 28):<28} {bot.elo:>7.1f} "
            f"{bot.wins:>4} {bot.draws:>4} {bot.losses:>4} {bot.errors:>4} "
            f"{bot.games:>6} {bot.score:>8.3f}"
        )
    print("-" * 90)


def main() -> int:
    state: RunState | None = None
    if sys.stdin.isatty() and sys.stdout.isatty():
        try:
            state = run_menu_curses()
        except Exception as exc:
            print(f"Curses-Menü nicht verfügbar ({exc}). Fallback auf Textmenü.")
            state = run_menu_plain()
    else:
        state = run_menu_plain()

    if state is None:
        return 0

    if is_resumable(state):
        start_line = (
            f"Starte Lauf: {state.mode} | "
            f"{state.next_game_idx}/{len(state.schedule)} gespielt"
        )
        print(start_line)
    else:
        print("Hinweis: Lauf ist bereits abgeschlossen.")

    results_path: Path | None = None
    if sys.stdin.isatty() and sys.stdout.isatty():
        try:
            results_path = run_state_curses(state)
        except Exception as exc:
            print(f"Curses-Run nicht verfügbar ({exc}). Fallback auf Textausgabe.")
            results_path = run_state_plain(state)
    else:
        results_path = run_state_plain(state)

    if results_path is None:
        print("\nLauf angehalten. Du kannst später per Resume fortsetzen.")
        print(f"State gespeichert in: {SAVE_FILE}")
        return 0

    print("\nFertig.")
    print_final_table_plain(state)
    print(f"Ergebnisse gespeichert in: {results_path}")
    print(f"State gespeichert in: {SAVE_FILE}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
