# Software-Challenge 2026 - Piranhas Bot Workspace

Diese Codebase ist ein lokales Entwicklungs- und Benchmark-Workspace fuer mehrere Piranhas-Bot-Implementierungen:

- Python (mehrere Alpha-Beta Varianten)
- Cython (`cython_v1`, `cython_v2`, `cython_v3`)
- Rust (`bots/rust`)
- C++/pybind11 Hybrid (`bots/cpp`)
- externe Vergleichsgegner (`opponent_bots/*`)

Der zentrale Runner fuer Duelle und Turniere ist `benchmark.py` (interaktives Text-/Curses-Menue, Resume, Result-Viewer, Logs).

## Inhalt

1. [Schnellstart](#schnellstart)
2. [Repository-Struktur](#repository-struktur)
3. [Haupt-Workflow: benchmark.py](#haupt-workflow-benchmarkpy)
4. [Bot-Implementierungen](#bot-implementierungen)
5. [Build-Anleitungen](#build-anleitungen)
6. [Training und Optimierung](#training-und-optimierung)
7. [Submission-Packaging](#submission-packaging)
8. [Ergebnisse, Status, Artefakte](#ergebnisse-status-artefakte)
9. [Legacy-Skripte](#legacy-skripte)
10. [Troubleshooting](#troubleshooting)
11. [Command Cheat Sheet](#command-cheat-sheet)

## Schnellstart

### Voraussetzungen

- Python `3.12` (siehe `.python-version`)
- Java Runtime (fuer `server/server.jar`)
- optional: Rust Toolchain (`cargo`) fuer `bots/rust`, `bots/cython_v3`, `bots/cpp`
- optional: C/C++ Toolchain + Cython fuer native Erweiterungen

### Basis-Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -U pip setuptools wheel
.venv/bin/pip install -r requirements.txt
```

### Direkter Einstieg in Duell/Turnier

```bash
.venv/bin/python benchmark.py
```

`benchmark.py` startet ein Menue, findet Bots rekursiv im Workspace und kann neue Runs starten oder alte fortsetzen.

## Repository-Struktur

```text
.
|- benchmark.py                  # zentraler duel/tournament runner (TUI + plain)
|- single_game.py                # einzelnes Match mit Live-Bot-Logs (legacy)
|- tournament.py                 # einfacher round-robin runner (legacy)
|- scripts/
|  |- ga_optimize_v2.py          # GA fuer cython_v2 eval weights
|  |- run_unattended_ga.py       # long-run wrapper um GA
|  |- tournament_new_vs_old.py   # direktvergleich cython_v2 vs cython_v1
|  `- package_bots.py            # erzeugt submission_*.zip
|- bots/
|  |- python/
|  |- cython_v1/
|  |- cython_v2/
|  |- cython_v3/
|  |- rust/
|  |- cpp/
|  `- my_player/                 # lokales packaged bot layout
|- opponent_bots/                # externe bots/wrapper
|- server/                       # game server jars + config
|- results/                      # summary jsons + run artefakte
|- replays/                      # replay xml/xml.gz
|- logs/                         # server logs
|- docs/                         # latex report
|- duel_state.json               # resumable run state (benchmark.py)
`- custom_bot_paths.json         # persistente custom bot entrypoints
```

## Haupt-Workflow: benchmark.py

`benchmark.py` ist der Standard-Einstieg fuer lokale Evaluierung.

### Was der Runner kann

- Modus `duel` (genau 2 Bots) oder `tournament` (>=2 Bots)
- rekursive Bot-Erkennung im aktuellen Workspace
- manuelles Hinzufuegen/Entfernen von Custom-Bot-Dateien (`.py`)
- side switching in beiden Modi (fairness)
- parallele Ausfuehrung mit mehreren Workern
- robuste Gewinnererkennung aus Server- und Bot-Logs
- ELO-Tracking (K=32), W/L/D/Error/Timeout Stats
- live Dashboard (curses bei TTY, sonst plain text)
- persistenter Resume ueber `duel_state.json`
- Result-Viewer fuer fruehere Runs

### Bot Discovery

Automatisch erkannt werden Python-Dateien, die typische Socha-Marker enthalten:

- `from socha.starter import Starter`
- `IClientHandler`
- oder vergleichbare Marker in AST/Text

Der Scanner ignoriert u.a.:

- `.git`, `.venv`, `__pycache__`, `results`, `node_modules`, weitere Cache-Ordner

### Run- und Ergebnisdateien

Bei jedem Run werden geschrieben:

- `duel_state.json` (state + progress, resume-faehig)
- `results/runs/<run_id>/summary.json`
- `results/runs/<run_id>/logs/game_<idx>_{server|bot_one|bot_two}.log`
- zusaetzlich latest summary:
  - `results/duel_results.json` oder
  - `results/tournament_results.json`

### Wichtige Defaults im Runner

- Base Port: `16000`
- Port step pro Spiel: `10`
- Spiel-Timeout: `300s`
- Turn-Timeout-Detection aus Bot-Logs: `2.0s`
- Worker:
  - duel: default `1`
  - tournament: default `min(4, cpu_count)`
  - optional override ueber `DUEL_WORKERS`

## Bot-Implementierungen

## `bots/python`

Mehrere reine Python-Referenzbots:

- `client.py`: Alpha-Beta + TT
- `client_v2.py`: weiterentwickelte Variante mit LMR/Killer/Move Ordering
- `client_optimized.py`: optimierte AB-Variante mit Zeitmanagement
- `client_alt.py`, `simon.py`: einfachere Baselines

Alle laufen ueber `socha.starter.Starter(...)`.

## `bots/cython_v1`

- Cython-Kern mit `board/evaluate/search/zobrist`
- Entry: `client_cython.py`
- Build ueber `setup.py build_ext --inplace`

## `bots/cython_v2`

Wie `v1`, plus runtime-gewichtete Evaluation:

- Entry: `client_cython.py`
- Baseline ohne env-weights: `client_cython_baseline.py`
- Build: `setup.py build_ext --inplace`
- Microbench: `micro_benchmark.py`

Environment-Parameter fuer Eval:

```bash
CYTHON_V2_EVAL_PARAMS="17.74,3.0,2.0,4.0,0.63"
```

Format: 5 Float-Werte.

## `bots/cython_v3`

Hybrid aus Python/Cython-Bridge und Rust-Core:

- Rust Lib: `rust_core` (`librust_core.so`)
- Python bridge: `cython_core/bridge.py`
- optionale Cython bridge: `cython_core/bridge_cy.pyx`
- Entry: `client_cython.py`
- festes Zeitbudget: `1700ms` pro Zug

Hilfsskripte:

- `build.sh` (rust + cython)
- `build_rust.sh`
- `prepare_submission.sh`
- `start.sh`, `run_client.sh`

## `bots/rust`

Reiner Rust-Bot:

- Binary: `target/release/piranhas-bot`
- Wrapper fuer pythonbasierte Runner: `pur_rust_client.py`
- `start.sh` baut automatisch neu, falls Quellen neuer sind
- CLI Parsing in Rust: `-h/--host`, `-p/--port`, `-r/--reservation`

## `bots/cpp`

Hybridbot mit nativer C++ Engine (pybind11) + Python Fallback:

- Packaging/Build ueber `pyproject.toml` + `scikit-build-core`
- Entry: `logic.py`
- Native Modul: `_piranhas_core.*.so`
- umfangreiche Tests unter `tests/`
- Bench und Tuning unter `bench/` und `scripts/`

Wichtige Runtime-Flags (env):

- `PIRANHAS_MOVE_HARD_CAP_NS`
- `PIRANHAS_RETURN_RESERVE_NS`
- `PIRANHAS_MIN_SEARCH_WINDOW_NS`
- `PIRANHAS_DEBUG`

## `opponent_bots`

Externe Vergleichsgegner inkl. Wrapper:

- `CrackedLine@v1/run.py`
- `CrackedLine@razzzzzer/run.py`

Diese Pfade koennen in `custom_bot_paths.json` hinterlegt und im `benchmark.py` Menue direkt selektiert werden.

## Build-Anleitungen

## Cython v1

```bash
cd bots/cython_v1
../../.venv/bin/pip install -r requirements.txt
../../.venv/bin/python setup.py build_ext --inplace
```

## Cython v2

```bash
cd bots/cython_v2
../../.venv/bin/pip install -r requirements.txt
../../.venv/bin/python setup.py build_ext --inplace
```

## Cython v3

```bash
cd bots/cython_v3
../../.venv/bin/pip install -r requirements.txt
./build.sh
```

## Rust bot

```bash
cd bots/rust
cargo build --release
```

## C++ bot

```bash
cd bots/cpp
../../.venv/bin/pip install -e .
```

Optional mit Dev-Extras:

```bash
cd bots/cpp
../../.venv/bin/pip install -e .[dev]
```

## Server starten (manuell)

```bash
cd server
./start.sh --port 13050
```

oder:

```bash
java -jar server/server.jar --port 13050
```

## Training und Optimierung

## 1) Direktvergleich alt vs neu

```bash
.venv/bin/python scripts/tournament_new_vs_old.py 50
```

Spielt `bots/cython_v2/client_cython.py` gegen `bots/cython_v1/client_cython.py` mit Seitenwechsel.

## 2) GA fuer `cython_v2` Eval-Gewichte

Script: `scripts/ga_optimize_v2.py`

Kerneigenschaften:

- optimiert 5 Gewichte (`best_swarm, swarm_count, material, isolated, distance`)
- Gegnerliste standardmaessig:
  - `bots/cython_v1/client_cython.py`
  - `bots/python/client_optimized.py`
- checkpoint/resume support
- Bewertung ueber W/L/D/E aus echten Spielen

Beispiel:

```bash
.venv/bin/python scripts/ga_optimize_v2.py \
  --population-size 16 \
  --generations 50 \
  --elite-count 4 \
  --games-per-opponent 4 \
  --timeout-s 300 \
  --checkpoint ga_v2_checkpoint.json \
  --opponent bots/cython_v1/client_cython.py \
  --opponent bots/python/client_optimized.py
```

Resume:

```bash
.venv/bin/python scripts/ga_optimize_v2.py --resume --checkpoint ga_v2_checkpoint.json
```

## 3) Unattended Long-Run GA

Script: `scripts/run_unattended_ga.py`

Highlights:

- chunked GA runs mit retries
- lock file gegen doppelte Instanzen
- live status:
  - JSON: `<log-dir>/progress_live.json`
  - Text: `<log-dir>/progress_live.txt`
- optional finale Validierung gegen Gegnerliste
- dynamische Gegnerliste aus Datei (waehrend Lauf editierbar)

Beispiel:

```bash
.venv/bin/python scripts/run_unattended_ga.py \
  --hours 72 \
  --chunk-generations 5 \
  --population-size 16 \
  --elite-count 4 \
  --games-per-opponent 2 \
  --timeout-s 180 \
  --checkpoint ga_v2_longrun_checkpoint.json \
  --log-dir log/ga_longrun
```

Stoppen:

```bash
touch log/ga_longrun/STOP
```

## 4) Micro-Benchmark fuer `cython_v2`

```bash
cd bots/cython_v2
../../.venv/bin/python micro_benchmark.py \
  --plies "0,6,12,20,30" \
  --time-limit 0.25 \
  --json-out bench/micro_benchmark_latest.json
```

## 5) C++ Bot Tuning/Tests

Beispiele:

```bash
cd bots/cpp
../../.venv/bin/python -m unittest discover -s tests -v
../../.venv/bin/python bench/bench_positions.py
../../.venv/bin/python scripts/selfplay_generate.py --output data/selfplay_games.jsonl
```

## Submission-Packaging

## Zentrale Sammel-Paketierung

`scripts/package_bots.py` erstellt mehrere `submission_*.zip` Dateien in `submissions/`:

- `submission_cython_v1.zip`
- `submission_cython_v2.zip`
- `submission_cython_v3.zip`
- `submission_cpp_client.zip`
- `submission_rust_bot.zip`

Script nutzt u.a.:

- Wheels aus `submissions/my_player.zip`
- gebaute Artefakte aus `bots/cython_v3` und `bots/cpp`
- `cargo build --release` fuer `bots/rust`

Aufruf:

```bash
.venv/bin/python scripts/package_bots.py
```

## Cython v3 Einzelpaket

```bash
cd bots/cython_v3
./prepare_submission.sh
```

## Ergebnisse, Status, Artefakte

- `duel_state.json`: aktueller/letzter Run-State fuer Resume
- `custom_bot_paths.json`: persistente manuell hinzugefuegte Bot-Dateipfade
- `results/runs/<run_id>/summary.json`: kompletter Runreport
- `results/runs/<run_id>/logs/`: pro Spiel server/bot logs
- `results/duel_results.json`, `results/tournament_results.json`: latest snapshots
- `logs/`: sonstige Serverlogs
- `replays/`: Replay XML/XML.GZ

## Legacy-Skripte

## `single_game.py`

- startet Server + 2 Bots
- zeigt Bot-Ausgaben live
- schreibt am Ende eine zusammengefuehrte Debug-Datei (`game_debug_<timestamp>.txt`)

Aufruf:

```bash
.venv/bin/python single_game.py <bot1.py> <bot2.py>
```

Hinweis: Standardwerte im Skript (`client_cython.py`, `simon.py`) sind historisch und passen nicht zur aktuellen `bots/*` Struktur ohne Pfadangabe.

## `tournament.py`

Einfacher round-robin Runner, der `client*.py` im Repository-Root sucht. Ebenfalls eher historisch im Vergleich zu `benchmark.py`.

## Troubleshooting

## `spawn_failed` oder Python nicht gefunden

Sicherstellen, dass `.venv` existiert und Commands ueber `.venv/bin/python` laufen.

## Rust-Library fehlt (`librust_core.so`)

```bash
cd bots/cython_v3
./build_rust.sh
```

## Lock aktiv bei unattended GA

Fehler: `Lock active: <checkpoint>.lock`

- laufenden Prozess pruefen
- nur wenn sicher kein Prozess laeuft: lock datei loeschen

## Keine Bots im benchmark Menue gefunden

- Bot muss `.py` sein
- Starter/IClientHandler Marker muessen im Code erkennbar sein
- sonst ueber `a` (custom path) manuell hinzufuegen

## Gewinner nicht eindeutig erkannt

Der Runner faellt auf mehrere Parsing-Regeln und Score-Tiebreaks zurueck. Bei unklaren Faellen `results/runs/<run_id>/logs/*` pruefen.

## Command Cheat Sheet

```bash
# Basis
.venv/bin/pip install -r requirements.txt

# Haupt-runner
.venv/bin/python benchmark.py

# cython builds
(cd bots/cython_v1 && ../../.venv/bin/python setup.py build_ext --inplace)
(cd bots/cython_v2 && ../../.venv/bin/python setup.py build_ext --inplace)
(cd bots/cython_v3 && ./build.sh)

# rust + cpp
(cd bots/rust && cargo build --release)
(cd bots/cpp && ../../.venv/bin/pip install -e .)

# GA
.venv/bin/python scripts/ga_optimize_v2.py --help
.venv/bin/python scripts/run_unattended_ga.py --help

# packaging
.venv/bin/python scripts/package_bots.py
```

## Dokumentation im Repo

- `bots/cpp/README.md` fuer nativen C++ Bot (Setup, Tests, Bench, Tuning)
- `bots/cython_v3/README.md` fuer Rust-Bridge Bot
- `scripts/SERVER_LONGRUN_GA_GUIDE.md` fuer serverorientierten long-run Ablauf
- `docs/` fuer den LaTeX-Bericht (`docs/build.sh`, `docs/makefile`)
