"""
Turnier-Skript: Lässt alle Bots im Ordner gegeneinander spielen.
"""

import json
import re
import subprocess
import time
import os
import signal
from pathlib import Path
import socket
from dataclasses import dataclass, field

# ============================================================================
# Konfiguration
# ============================================================================

GAMES_PER_MATCHUP = 8  # Spiele pro Paarung (hin und rück)
SERVER_JAR = "server/server.jar"
BASE_PORT = 13050
PYTHON_PATH = str(Path(__file__).parent / ".venv" / "bin" / "python")
RESULTS_FILE = "tournament_results.json"

# Bot-Dateien mit Namen (Pfad -> Anzeigename)
BOT_NAMES = {
    "client.py": "AB-Basic",
    "client_alt.py": "Simple-Eval",
    "client_cython.py": "Cython",
    "client_optimized.py": "AB-LMR",
    "client_v2.py": "AB-Incremental",
}

# Bot-Dateien (werden automatisch erkannt oder hier manuell angeben)
BOT_FILES = []  # Leer = automatisch alle client*.py finden


# ============================================================================
# Bot-Tracking
# ============================================================================


@dataclass
class BotStats:
    name: str
    path: str
    wins: int = 0
    losses: int = 0
    draws: int = 0
    errors: int = 0

    @property
    def games(self) -> int:
        return self.wins + self.losses + self.draws

    @property
    def score(self) -> float:
        if self.games == 0:
            return 0
        return (self.wins + 0.5 * self.draws) / self.games

    @property
    def winrate(self) -> str:
        if self.games == 0:
            return "0%"
        return f"{100 * self.wins / self.games:.1f}%"


# ============================================================================
# Hilfsfunktionen
# ============================================================================


def find_bots() -> list[Path]:
    """Findet alle Bot-Dateien im Ordner."""
    if BOT_FILES:
        return [Path(f) for f in BOT_FILES]

    base = Path(__file__).parent
    bots = []

    for f in base.glob("client*.py"):
        # Prüfen ob es ein gültiger Bot ist
        content = f.read_text()
        if "IClientHandler" in content or "Starter" in content or "from starter import" in content:
            bots.append(f)

    return sorted(bots)


def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) == 0


def find_free_port(start_port: int = 13050) -> int:
    port = start_port
    while is_port_in_use(port):
        port += 1
    return port


def run_game(bot1_path: Path, bot2_path: Path, game_id: int) -> tuple[int | None, str]:
    """
    Führt ein Spiel zwischen zwei Bots aus.
    Returns: (result, info)
        result: 1 wenn Bot1 gewinnt, 2 wenn Bot2 gewinnt, 0 bei Unentschieden, None bei Fehler
        info: Zusätzliche Info (z.B. Fehlermeldung)
    """
    port = find_free_port(BASE_PORT + game_id * 10)

    server_proc = None
    bot1_proc = None
    bot2_proc = None

    # Log-Datei für Server-Output
    log_file = Path(f"/tmp/tournament_server_{game_id}.log")
    log_file.unlink(missing_ok=True)

    try:
        # Server starten mit Log-Capture
        with open(log_file, "w") as log_f:
            server_proc = subprocess.Popen(
                ["java", "-jar", SERVER_JAR, "--port", str(port)],
                stdout=log_f,
                stderr=subprocess.STDOUT,
                cwd=str(Path(__file__).parent),
                preexec_fn=os.setsid,
            )

        time.sleep(2)

        # Bots direkt starten (ohne Wrapper)
        bot1_proc = subprocess.Popen(
            [PYTHON_PATH, str(bot1_path), "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(Path(__file__).parent),
            preexec_fn=os.setsid,
        )

        time.sleep(0.5)

        bot2_proc = subprocess.Popen(
            [PYTHON_PATH, str(bot2_path), "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(Path(__file__).parent),
            preexec_fn=os.setsid,
        )

        # Warten auf Spielende (max 5 Minuten)
        timeout = 300
        start = time.time()

        while time.time() - start < timeout:
            if bot1_proc.poll() is not None and bot2_proc.poll() is not None:
                break
            time.sleep(1)

        time.sleep(1)

        # Ergebnis aus Server-Log lesen
        result = None
        info = ""

        if log_file.exists():
            log_content = log_file.read_text()

            # Suche nach Winner im Log
            # Format: "Winner: ONE" oder "Winner: TWO" oder "DRAW"
            if "winner=ONE" in log_content or "Winner: ONE" in log_content or "winner=Team One" in log_content:
                result = 1  # Bot1 ist immer ONE (startet zuerst)
                info = "ONE wins"
            elif "winner=TWO" in log_content or "Winner: TWO" in log_content or "winner=Team Two" in log_content:
                result = 2  # Bot2 ist immer TWO
                info = "TWO wins"
            elif "DRAW" in log_content or "draw" in log_content.lower():
                result = 0
                info = "Draw"
            else:
                # Fallback: Suche nach regularGameEnd
                match = re.search(r'winner=(\w+)', log_content)
                if match:
                    winner = match.group(1).upper()
                    if "ONE" in winner:
                        result = 1
                        info = "ONE wins (regex)"
                    elif "TWO" in winner:
                        result = 2
                        info = "TWO wins (regex)"

        if result is None:
            info = "Could not determine winner from log"

        return result, info

    except Exception as e:
        return None, f"Error: {e}"

    finally:
        # Prozesse beenden
        for proc in [bot1_proc, bot2_proc, server_proc]:
            if proc is not None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except:
                    pass

        # Log aufräumen
        log_file.unlink(missing_ok=True)


def run_tournament(bots: list[BotStats]) -> None:
    """Führt ein Round-Robin-Turnier durch."""
    n = len(bots)
    game_id = 0
    total_games = n * (n - 1) // 2 * GAMES_PER_MATCHUP

    print(f"\nTotal: {total_games} Spiele\n")

    for i in range(n):
        for j in range(i + 1, n):
            for game_num in range(GAMES_PER_MATCHUP):
                # Wechsle Startspieler
                if game_num % 2 == 0:
                    b1, b2 = bots[i], bots[j]
                else:
                    b1, b2 = bots[j], bots[i]

                print(
                    f"  [{game_id + 1}/{total_games}] {b1.name} vs {b2.name}...",
                    end=" ",
                    flush=True,
                )

                result, info = run_game(Path(b1.path), Path(b2.path), game_id)

                if result == 1:
                    b1.wins += 1
                    b2.losses += 1
                    print(f"{b1.name} gewinnt")
                elif result == 2:
                    b2.wins += 1
                    b1.losses += 1
                    print(f"{b2.name} gewinnt")
                elif result == 0:
                    b1.draws += 1
                    b2.draws += 1
                    print("Unentschieden")
                else:
                    b1.errors += 1
                    b2.errors += 1
                    print(f"Fehler: {info}")

                game_id += 1

                # Zwischenergebnisse speichern
                save_results(bots)


def save_results(bots: list[BotStats]) -> None:
    """Speichert die Ergebnisse."""
    results = {
        "bots": [
            {
                "name": b.name,
                "path": b.path,
                "wins": b.wins,
                "losses": b.losses,
                "draws": b.draws,
                "errors": b.errors,
                "games": b.games,
                "score": round(b.score, 3),
                "winrate": b.winrate,
            }
            for b in sorted(bots, key=lambda x: x.score, reverse=True)
        ]
    }

    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2)


def print_standings(bots: list[BotStats]) -> None:
    """Zeigt die Rangliste an."""
    sorted_bots = sorted(bots, key=lambda x: x.score, reverse=True)

    print("\n" + "=" * 70)
    print("  RANGLISTE")
    print("=" * 70)
    print(
        f"{'#':<3} {'Bot':<25} {'W':>4} {'L':>4} {'D':>4} {'Score':>7} {'Winrate':>8}"
    )
    print("-" * 70)

    for i, b in enumerate(sorted_bots, 1):
        print(
            f"{i:<3} {b.name:<25} {b.wins:>4} {b.losses:>4} {b.draws:>4} {b.score:>7.3f} {b.winrate:>8}"
        )

    print("=" * 70)


# ============================================================================
# Main
# ============================================================================


def main():
    print("=" * 60)
    print("  Bot-Turnier")
    print("=" * 60)

    # Bots finden
    bot_files = find_bots()

    if len(bot_files) < 2:
        print("Fehler: Mindestens 2 Bots benötigt!")
        print(f"Gefunden: {bot_files}")
        return

    print(f"\nGefundene Bots ({len(bot_files)}):")
    bots = []
    for f in bot_files:
        display_name = BOT_NAMES.get(f.name, f.stem)
        print(f"  - {display_name} ({f.name})")
        bots.append(BotStats(name=display_name, path=str(f)))

    print(f"\nSpiele pro Paarung: {GAMES_PER_MATCHUP}")

    # Turnier durchführen
    print("\n" + "-" * 60)
    print("Turnier startet...")
    print("-" * 60)

    run_tournament(bots)

    # Ergebnisse anzeigen
    print_standings(bots)
    print(f"\nErgebnisse gespeichert in: {RESULTS_FILE}")


if __name__ == "__main__":
    main()
