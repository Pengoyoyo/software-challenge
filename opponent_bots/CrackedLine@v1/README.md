# CrackedLine v1 (Rust-first Piranhas Bot)

Dieses Projekt implementiert den Piranhas-Bot **hauptsaechlich in Rust**:

- Rust-Core: `state`, `movegen`, `eval`, `search`, `tt`, `time_manager`
- Python: nur Socha-Adapter (`logic.py`, `state_adapter.py`, `rust_bridge.py`)

## Start

```bash
python client.py
```

`client.py` baut automatisch das Rust-Binary (`cargo build --release`) und startet danach `logic.py`.

## Manuell bauen

```bash
cargo build --release
```

Binary:

```bash
target/release/piranhas-rs-engine
```

## Tests

```bash
cargo test
python -m unittest discover -s tests -p 'test_*.py' -v
```

## Relevante Env-Flags

- `PIRANHAS_MOVE_HARD_CAP_NS` (default `1800000000`, hart gecappt auf max `1850000000`)
- `PIRANHAS_RETURN_RESERVE_NS` (default `180000000`)
- `PIRANHAS_MIN_SEARCH_WINDOW_NS` (default `5000000`)
- `PIRANHAS_ENGINE_IO_MARGIN_NS` (default `60000000`)
- `PIRANHAS_LOG_GUARD_NS` (default `40000000`)
- `PIRANHAS_PREWARM_TIMEOUT_NS` (default `1600000000`)
- `PIRANHAS_DEBUG` (`1`/`0`)
- `PIRANHAS_SAVE_LOGS` (`1`/`0`, default `1`)
- `PIRANHAS_LOG_DIR` (default `artifacts/game_logs`)
- `PIRANHAS_LOG_FILE` (optional exakter Dateipfad; uebersteuert `PIRANHAS_LOG_DIR`)
- `PIRANHAS_TT_MB` (default `6144` im Engine-Binary)
- `CLIENT_SKIP_BUILD=1` (Build in `client.py` ueberspringen)
- `CLIENT_FORCE_BUILD=1` (Build erzwingen)
- `PIRANHAS_POLICY_CACHE_PATH` (default `artifacts/opening_policy_cache.bin`)
- `PIRANHAS_BOOK_MAX_MB` (default `256`)
- `PIRANHAS_BOOK_FORCE_CONFIDENCE` (default `85`)
- `PIRANHAS_BOOK_HINT_CONFIDENCE` (default `65`)
- `PIRANHAS_EVAL_PROFILE` (optional: `default`, `swarm`, `disconnect`, `mobility`)
- `PIRANHAS_EVAL_WEIGHTS_FILE` (optional: `key=value` Datei fuer Eval-Gewichte)
  - Neue v3.2 Felder: `w_articulation_pressure`, `w_round_end_tempo`

Hinweis Zeitlimit:
- Das effektive Suchbudget wird hart auf `1.85s` gecappt (Python- und Rust-seitig).
- Zusätzlich gibt es einen I/O-Timeout auf die Rust-Antwort, damit kein Blockieren ueber die Deadline hinaus passiert.
- Zusätzlich gibt es im Python-Bot einen Watchdog um den Search-Call; bei Timeout wird sofort auf Fallback-Zug gewechselt.

Hinweis Logging:
- Alle Bot-Logs werden weiterhin auf `stderr` ausgegeben und zusaetzlich in eine Datei geschrieben.
- Standardpfad pro Start: `artifacts/game_logs/game_<timestamp>_<pid>.log`.

## Zug-Logging (stderr)

Pro Zug wird ein zusammenhaengender Block auf `stderr` ausgegeben:

```text
=== Zug 2 ===
Rust Search: 39 moves, team=2
d1: -20 | 272n 0h 677478nps 0.00s
...
-> (1, 9) (DownRight ↘)
```

- Die `dX`-Zeilen zeigen Iterationsmetriken aus dem Rust-Search.
- Die letzte Zeile zeigt Startkoordinate und Richtung des gewaehlten Zuges.

## Tiefe+NPS Benchmark (A/B)

Deterministischer Snapshot-Benchmark:

```bash
python bench/ab_depth_nps.py --bench-budget-ms 200
```

A/B gegen zwei Binaries:

```bash
python bench/ab_depth_nps.py \
  --base-binary target/release/piranhas-rs-engine \
  --cand-binary /path/to/candidate-engine \
  --bench-budget-ms 200
```

## Selfplay SPRT Gate (Elo)

SPRT (H0=0 Elo, H1=+35 Elo) fuer Candidate vs Base:

```bash
python bench/selfplay_sprt.py \
  --base-binary artifacts/piranhas-base \
  --cand-binary artifacts/piranhas-cand \
  --move-budget-ms 250 \
  --elo0 0 \
  --elo1 35 \
  --alpha 0.05 \
  --beta 0.05
```

- Gibt pro Spiel `W/L/D`, `elo_hat` und `llr` aus.
- Stoppt automatisch bei `ACCEPT H1`, `ACCEPT H0` oder `max-games`.
- Relative Pfade werden gegen das Projekt-Root aufgeloest.
- Spiele laufen als Seed-Paare (gleicher Seed, Farben getauscht) fuer geringere Varianz.
- Optional sind unterschiedliche Eval-Setups moeglich:
  - `--base-eval-profile ...`, `--cand-eval-profile ...`
  - `--base-eval-weights ...`, `--cand-eval-weights ...`

Vorbereitung:

```bash
cargo build --release
mkdir -p artifacts
cp target/release/piranhas-rs-engine artifacts/piranhas-base
cp target/release/piranhas-rs-engine artifacts/piranhas-cand
```

## Opening Policy Cache (OPC1)

Offline-Generierung:

```bash
python bench/build_policy_cache.py \
  --games 64 \
  --turn-max 14 \
  --analysis-budget-ms 900 \
  --playout-budget-ms 80 \
  --workers 0 \
  --progress-every 25 \
  --output artifacts/opening_policy_cache.bin
```

Empfohlener 2-Tage-Run (80% CPU):

```bash
python bench/build_policy_cache.py \
  --games 12000 \
  --seed-count 4000 \
  --turn-max 14 \
  --analysis-budget-ms 900 \
  --playout-budget-ms 80 \
  --workers 0 \
  --progress-every 100 \
  --min-samples 8 \
  --min-confidence 70 \
  --output artifacts/opening_policy_cache.bin
```

`--workers 0` nutzt automatisch `floor(0.8 * nproc)`.

## Eval SPSA Tuning

Dry-Run:

```bash
python bench/tune_eval_spsa.py --dry-run --binary target/release/piranhas-rs-engine
```

STC Run:

```bash
python bench/tune_eval_spsa.py \
  --binary target/release/piranhas-rs-engine \
  --iterations-a 24 \
  --iterations-b 12 \
  --games-per-eval-a 24 \
  --games-per-eval-b 36 \
  --move-budget-ms 90
```

Artefakte landen unter `artifacts/eval_tuning/<timestamp>/`:
- `baseline.weights`
- `best.weights`
- `final.weights`
- `meta.json`
- stage-spezifische Iterationslogs

Runtime-Nutzung:

- `confidence >= 85` und genug Samples: Zug wird direkt aus Cache gespielt.
- `65 <= confidence < 85`: Cache dient als Root-Hint, Suche entscheidet final.

## Struktur

- `src/state.rs`: Board, Hash, make/unmake, Terminalfunktionen
- `src/movegen.rs`: legale Zuege + Captures
- `src/eval.rs`: wertbasierte HCE
- `src/search.rs`: ID + Negamax + Alpha-Beta + PVS + QSearch + TT + Pruning
- `src/tt.rs`: Transposition Table
- `src/time_manager.rs`: Deadline/Iterationskontrolle
- `logic.py`: Socha-Entry
- `rust_bridge.py`: persistenter Rust-Subprozess
