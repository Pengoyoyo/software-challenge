#!/usr/bin/env python3
"""
Tournament: New Cython bot vs Old Cython bot with side-switching.
"""

import subprocess
import time
import sys
import re
import os
import signal
import socket
from pathlib import Path

SERVER_JAR = "software-challenge-server/server.jar"
BASE_PORT = 13050
PYTHON_PATH = str(Path(__file__).parent / ".venv" / "bin" / "python")

NEW_BOT = str(Path(__file__).parent / "cython_v2" / "client_cython.py")
OLD_BOT = "/home/rasmus/Documents/Software-Challenge/cython_v1/client_cython.py"


def find_free_port(start: int = BASE_PORT) -> int:
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("localhost", port)) != 0:
                return port
    return start


def run_game(bot1: str, bot2: str, game_id: int) -> dict:
    """Run a single game. bot1=ONE, bot2=TWO. Returns result dict."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    port = find_free_port(BASE_PORT + game_id * 10)
    server_log = Path(f"/tmp/tourney_server_{game_id}.log")
    bot1_log = Path(f"/tmp/tourney_bot1_{game_id}.log")
    bot2_log = Path(f"/tmp/tourney_bot2_{game_id}.log")

    result = {"winner": "UNKNOWN", "bot1_depths": "n/a", "bot2_depths": "n/a",
              "bot1_crash": False, "bot2_crash": False}

    server_proc = bot1_proc = bot2_proc = None

    try:
        with open(server_log, "w") as f:
            server_proc = subprocess.Popen(
                ["java", "-jar", SERVER_JAR, "--port", str(port)],
                cwd=str(Path(__file__).parent),
                stdout=f, stderr=subprocess.STDOUT,
                preexec_fn=os.setsid,
            )
        time.sleep(2.5)

        with open(bot1_log, "w") as f1:
            bot1_proc = subprocess.Popen(
                [PYTHON_PATH, "-u", bot1, "--port", str(port)],
                cwd=str(Path(__file__).parent),
                stdout=f1, stderr=subprocess.STDOUT,
                env=env, preexec_fn=os.setsid,
            )
        time.sleep(0.5)

        with open(bot2_log, "w") as f2:
            bot2_proc = subprocess.Popen(
                [PYTHON_PATH, "-u", bot2, "--port", str(port)],
                cwd=str(Path(__file__).parent),
                stdout=f2, stderr=subprocess.STDOUT,
                env=env, preexec_fn=os.setsid,
            )

        # Wait for finish
        start_t = time.time()
        while time.time() - start_t < 300:
            if bot1_proc.poll() is not None and bot2_proc.poll() is not None:
                break
            time.sleep(1)
        time.sleep(1)

        # Check for crashes
        if bot1_proc.poll() is not None and bot1_proc.returncode != 0:
            result["bot1_crash"] = True
        if bot2_proc.poll() is not None and bot2_proc.returncode != 0:
            result["bot2_crash"] = True

        # Parse server log
        if server_log.exists():
            content = server_log.read_text()
            if "LOST_CONNECTION" in content:
                if "ONE hat das Spiel verlassen" in content:
                    result["winner"] = "TWO"
                elif "TWO hat das Spiel verlassen" in content:
                    result["winner"] = "ONE"
            m = re.search(r"scores=\[\[Siegpunkte=(\d+).*?\], \[Siegpunkte=(\d+)", content)
            if m:
                s1, s2 = int(m.group(1)), int(m.group(2))
                if s1 > s2:
                    result["winner"] = "ONE"
                elif s2 > s1:
                    result["winner"] = "TWO"
                elif s1 == s2 and s1 > 0:
                    result["winner"] = "DRAW"

        # Parse depths
        if bot1_log.exists():
            result["bot1_depths"] = extract_depths(bot1_log.read_text())
        if bot2_log.exists():
            result["bot2_depths"] = extract_depths(bot2_log.read_text())

    except Exception as e:
        result["winner"] = f"ERROR: {e}"
    finally:
        for proc in [bot1_proc, bot2_proc, server_proc]:
            if proc is not None:
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
        for f in [server_log, bot1_log, bot2_log]:
            f.unlink(missing_ok=True)

    return result


def extract_depths(log: str) -> str:
    depths = []
    for line in log.split("\n"):
        m = re.search(r"d(\d+):", line)
        if m:
            depths.append(int(m.group(1)))
    if depths:
        return f"max={max(depths)}"
    return "n/a"


def main():
    num_games = int(sys.argv[1]) if len(sys.argv) > 1 else 10

    print("=" * 65)
    print(f"  NEW vs OLD Cython  ({num_games} games, sides alternate)")
    print("=" * 65)

    new_wins = 0
    old_wins = 0
    draws = 0
    errors = 0

    for i in range(num_games):
        if i % 2 == 0:
            bot_one, bot_two = NEW_BOT, OLD_BOT
            new_side, old_side = "ONE", "TWO"
        else:
            bot_one, bot_two = OLD_BOT, NEW_BOT
            new_side, old_side = "TWO", "ONE"

        print(f"\n[{i+1}/{num_games}] NEW={new_side} OLD={old_side} ... ", end="", flush=True)

        r = run_game(bot_one, bot_two, i)

        if r["winner"] == new_side:
            new_wins += 1
            tag = "NEW WINS"
        elif r["winner"] == old_side:
            old_wins += 1
            tag = "OLD WINS"
        elif r["winner"] == "DRAW":
            draws += 1
            tag = "DRAW"
        else:
            errors += 1
            tag = f"??? ({r['winner']})"

        crash_info = ""
        if r["bot1_crash"]:
            crash_info += " [ONE crashed]"
        if r["bot2_crash"]:
            crash_info += " [TWO crashed]"

        print(f"{tag}  ONE({r['bot1_depths']}) TWO({r['bot2_depths']}){crash_info}")
        print(f"         Score: NEW {new_wins}-{old_wins} OLD  (draws={draws} err={errors})")

        time.sleep(1)

    print("\n" + "=" * 65)
    total = new_wins + old_wins + draws
    pct = new_wins / max(1, total) * 100
    print(f"  RESULT: NEW {new_wins} - {old_wins} OLD  ({draws} draws, {errors} errors)")
    print(f"  NEW win rate: {pct:.0f}%")
    print("=" * 65)


if __name__ == "__main__":
    main()
