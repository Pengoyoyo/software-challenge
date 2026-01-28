#!/usr/bin/env python3
"""
Einzelnes Spiel zwischen zwei Bots mit sichtbaren Bot-Logs
"""

import subprocess
import time
import sys
import re
from pathlib import Path
from datetime import datetime

SERVER_JAR = "software-challenge-server/server.jar"
PORT = 13050
PYTHON_PATH = str(Path(__file__).parent / ".venv" / "bin" / "python")

def run_single_game(bot1: str, bot2: str):
    """Spielt ein Spiel zwischen zwei Bots."""

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    debug_file = Path(f"game_debug_{timestamp}.txt")
    server_log = Path(f"/tmp/server_{timestamp}.log")
    bot1_log = Path(f"/tmp/bot1_{timestamp}.log")
    bot2_log = Path(f"/tmp/bot2_{timestamp}.log")

    print("=" * 70)
    print(f"  {bot1} (ONE) vs {bot2} (TWO)")
    print("=" * 70)

    # Server starten (Output in Datei)
    print("\n[1/3] Starte Server...")
    with open(server_log, "w") as f:
        server_proc = subprocess.Popen(
            ["java", "-jar", SERVER_JAR, "--port", str(PORT)],
            cwd=str(Path(__file__).parent),
            stdout=f,
            stderr=subprocess.STDOUT,
        )

    time.sleep(2)

    # Bot 1 starten (Output sichtbar + in Datei)
    print(f"[2/3] Starte {bot1} (Team ONE)...")
    bot1_log_handle = open(bot1_log, "w")

    # Environment mit unbuffered output
    import os
    env = os.environ.copy()
    env['PYTHONUNBUFFERED'] = '1'

    bot1_proc = subprocess.Popen(
        [PYTHON_PATH, "-u", bot1, "--port", str(PORT)],
        cwd=str(Path(__file__).parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )

    time.sleep(0.5)

    # Bot 2 starten (Output sichtbar + in Datei)
    print(f"[3/3] Starte {bot2} (Team TWO)...")
    bot2_log_handle = open(bot2_log, "w")
    bot2_proc = subprocess.Popen(
        [PYTHON_PATH, "-u", bot2, "--port", str(PORT)],
        cwd=str(Path(__file__).parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )

    print("\n" + "=" * 70)
    print("Spiel läuft... (Bot-Ausgaben)")
    print("=" * 70 + "\n")

    try:
        # Live-Output der Bots
        import select

        while True:
            # Check if processes are done
            if bot1_proc.poll() is not None and bot2_proc.poll() is not None:
                break

            # Read from bot1
            if bot1_proc.stdout:
                line = bot1_proc.stdout.readline()
                if line:
                    print(f"[ONE] {line}", end="")
                    bot1_log_handle.write(line)
                    bot1_log_handle.flush()

            # Read from bot2
            if bot2_proc.stdout:
                line = bot2_proc.stdout.readline()
                if line:
                    print(f"[TWO] {line}", end="")
                    bot2_log_handle.write(line)
                    bot2_log_handle.flush()

            time.sleep(0.01)

        # Read remaining output
        for line in bot1_proc.stdout:
            print(f"[ONE] {line}", end="")
            bot1_log_handle.write(line)

        for line in bot2_proc.stdout:
            print(f"[TWO] {line}", end="")
            bot2_log_handle.write(line)

        server_proc.wait(timeout=5)

    except KeyboardInterrupt:
        print("\n\nSpiel abgebrochen!")
    finally:
        # Cleanup
        bot1_log_handle.close()
        bot2_log_handle.close()

        for proc in [bot1_proc, bot2_proc, server_proc]:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except:
                try:
                    proc.kill()
                except:
                    pass

        # Debug-Datei erstellen
        print("\n" + "=" * 70)
        print("Erstelle Debug-Datei...")
        print("=" * 70)

        create_debug_file(debug_file, bot1, bot2, server_log, bot1_log, bot2_log)

        # Cleanup temp files
        server_log.unlink(missing_ok=True)
        bot1_log.unlink(missing_ok=True)
        bot2_log.unlink(missing_ok=True)

        print(f"\n✓ Debug-Datei erstellt: {debug_file}")


def create_debug_file(output: Path, bot1: str, bot2: str,
                      server_log: Path, bot1_log: Path, bot2_log: Path):
    """Erstellt eine zusammengefasste Debug-Datei."""

    with open(output, "w") as f:
        f.write("=" * 80 + "\n")
        f.write("GAME DEBUG LOG\n")
        f.write("=" * 80 + "\n")
        f.write(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Bot ONE: {bot1}\n")
        f.write(f"Bot TWO: {bot2}\n")
        f.write("=" * 80 + "\n\n")

        # Server Log
        f.write("\n" + "=" * 80 + "\n")
        f.write("SERVER LOG\n")
        f.write("=" * 80 + "\n")
        if server_log.exists():
            server_content = server_log.read_text()
            f.write(server_content)

            # Extrahiere Gewinner
            winner = None
            if "winner=ONE" in server_content or "Winner: ONE" in server_content:
                winner = "ONE"
            elif "winner=TWO" in server_content or "Winner: TWO" in server_content:
                winner = "TWO"
            elif "DRAW" in server_content or "draw" in server_content.lower():
                winner = "DRAW"
        else:
            f.write("(No server log found)\n")
            winner = "UNKNOWN"

        # Bot 1 Log
        f.write("\n" + "=" * 80 + "\n")
        f.write(f"BOT ONE LOG ({bot1})\n")
        f.write("=" * 80 + "\n")
        if bot1_log.exists():
            f.write(bot1_log.read_text())
        else:
            f.write("(No bot log found)\n")

        # Bot 2 Log
        f.write("\n" + "=" * 80 + "\n")
        f.write(f"BOT TWO LOG ({bot2})\n")
        f.write("=" * 80 + "\n")
        if bot2_log.exists():
            f.write(bot2_log.read_text())
        else:
            f.write("(No bot log found)\n")

        # Zusammenfassung
        f.write("\n" + "=" * 80 + "\n")
        f.write("SUMMARY\n")
        f.write("=" * 80 + "\n")
        f.write(f"Winner: {winner}\n")

        # Extrahiere Tiefen-Statistiken
        if bot1_log.exists():
            bot1_depths = extract_depths(bot1_log.read_text())
            f.write(f"\nBot ONE depths: {bot1_depths}\n")

        if bot2_log.exists():
            bot2_depths = extract_depths(bot2_log.read_text())
            f.write(f"Bot TWO depths: {bot2_depths}\n")


def extract_depths(log_content: str) -> str:
    """Extrahiert die erreichten Suchtiefen aus dem Log."""
    depths = []
    for line in log_content.split("\n"):
        match = re.search(r'd(\d+):', line)
        if match:
            depths.append(int(match.group(1)))

    if depths:
        return f"min={min(depths)}, max={max(depths)}, avg={sum(depths)/len(depths):.1f}"
    return "No depth info found"


if __name__ == "__main__":
    if len(sys.argv) == 3:
        bot1 = sys.argv[1]
        bot2 = sys.argv[2]
    else:
        # Standard: Cython vs LMR
        bot1 = "client_cython.py"
        bot2 = "client_optimized.py"

    run_single_game(bot1, bot2)
