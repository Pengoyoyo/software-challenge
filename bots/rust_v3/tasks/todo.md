# Piranhas Bot v2 — Verbesserungsplan

## Done ✅

### Task 1: Terminal-Konstanten vereinheitlichen (WIN_SCORE → MATE_SCORE)
**Status:** Completed ✅ | **Date:** 2026-05-16
- `WIN_SCORE` (1.000.000) entfernt, überall `MATE_SCORE` (900.000) verwendet.
- Alpha-Beta-Invariante wiederhergestellt.

### Task 2: Fallback-Zug auf panic! statt Move::default()
**Status:** Completed ✅ | **Date:** 2026-05-16
- `unwrap_or_else(|| Move::default())` → `expect("Engine did not produce a legal move")`
- Verhindert Versand illegaler Züge an den Server.

### Task 3: Tuning-Code (Umgebungsvariablen) entfernen
**Status:** Completed ✅ | **Date:** 2026-05-16
- `EVAL_WEIGHTS`, `parse_weights_list`, `load_eval_weights` entfernt.
- Direkter Zugriff auf `&DEFAULT_WEIGHTS`.

### Task 4: Tiebreaker-Tracking implementieren
**Status:** Completed ✅ | **Date:** 2026-05-16
- Neues Feld `connected_since: [Option<u16>; 2]` in `Position` + `Undo`.
- `recompute_caches()` setzt `connected_since` wenn bereits verbunden.
- `make_move()` aktualisiert bei erstem Connect.
- `terminal_swarm_score()` und `evaluate()` beachten Tiebreaker.
- 18 Tests inklusive 4 Tiebreaker-Tests.

### Task 5: Eval-Funktion auf &Position refactoren
**Status:** Completed ✅ | **Date:** 2026-05-16
- `generate_moves_for(player)` und `generate_captures_for(player)` in `board.rs`.
- `has_one_move_connect` nimmt `&Position` und klont für Tests.
- `evaluate` nimmt `&Position` statt `&mut Position`.
- `search.rs` entsprechend angepasst (keine `&mut` für Eval).

### Task 6: Aspiration Window verbreitern
**Status:** Completed ✅ | **Date:** 2026-05-16
- `ASP_WINDOW` von 80 auf 300 erhöht.
- Weniger Re-Searches, höhere erreichte Tiefe erwartet.

### Task 7: Quiescence Delta-Pruning fixen
**Status:** Completed ✅ | **Date:** 2026-05-16
- `Q_DELTA` von 160 auf 5000 erhöht.
- Verhindert Pruning von Verbindungszügen mit massiver Konnektivitätsänderung.

### Task 9: Dynamisches Zeitmanagement
**Status:** Completed ✅ | **Date:** 2026-05-16
- Zeitbudget abhängig von `pos.turn`:
  - Turns 0-4: 1900 ms
  - Turns 5-20: 1800 ms
  - Turns 21-40: 1600 ms
  - Turns 41-50: 1200 ms
  - Turns 51+: 800 ms

### Task 10: has_one_move_connect aus Eval entfernen
**Status:** Completed ✅ | **Date:** 2026-05-16
- `has_one_move_connect`-Aufrufe aus `evaluate` entfernt.
- Bleibt in `search.rs` für Extensions.
- `connect_bonus` aus `EvalWeights` entfernt.

### Task 13: Counter-Move entfernen
**Status:** Completed ✅ | **Date:** 2026-05-16
- `counter_moves` Array aus `SearchEngine` entfernt.
- `score_move` und `order_moves` ohne Counter-Parameter.
- Counter-Move-Update in `search_node` entfernt.
- Vereinfachte API, bessere Cache-Lokalität.

### Task 15: TT auf 256 MB erhöhen
**Status:** Completed ✅ | **Date:** 2026-05-16
- `TT_MB` von 128 auf 256 erhöht.

### Task 8: Undo-Optimierung (Inkrementelle Konnektivität)
**Status:** Completed ✅ | **Date:** 2026-05-16
- `Undo`-Struct von ~491 auf ~20 Bytes verkleinert.
- `prev_comp_id/size/value/sum_x/sum_y/n_comps` aus `Undo` entfernt.
- `unmake_move` ruft `_rebuild_components()` auf (O(8) BFS) statt 471 Bytes zurück zu kopieren.
- Alle 18 Tests weiterhin grün. Build warning-frei.

### Task 11: Self-Play-Infrastruktur
**Status:** Completed ✅ | **Date:** 2026-05-16
- `src/bin/selfplay.rs` implementiert.
- Zwei Engine-Instanzen gegeneinander, konfigurierbare Zugzeit, Winrate-Tracking.
- Start: `cargo run --bin selfplay -- <n_games> <red_ms> <blue_ms> [-v]`

### Task 12: Benchmark-Binary
**Status:** Completed ✅ | **Date:** 2026-05-16
- `benches/benchmark.rs` als `[[bin]]`-Target (kein Criterion erforderlich).
- Benchmarks für Eval, Move-Gen, Make+Unmake, Search.
- Start: `cargo run --release --bin benchmark`

## Pending ⏳

*(Keine offenen Tasks)*

## Learnings 📝

### Lessons.md-Einträge
- **Undo::default() initialisiert [Option<u16>; 2] mit [None, None]**: War überrascht, dass das korrekt funktioniert.
- **Flood-Fill bei recompute_caches()**: `is_connected()` nach Aufbau der Bitboards aufrufen, nicht davor.
- **Connected-Since Tracking**: Muss in `Undo` gespeichert werden, da `unmake_move` den vorherigen Zustand restoren muss.
