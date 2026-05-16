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

## Pending ⏳

### Task 8: Inkrementelle Konnektivität (großer Performance-Sprung)
**Status:** Pending ⏳ | **Priority:** High
- Union-Find oder inkrementelle `component_id`-Tracking in `Position`.
- Ziel: O(1) statt O(Fische) für `component_count`, `largest_component_value`, `component_spread`.
- Komplexität: Hoch (Split-Erkennung bei `make_move`).

### Task 11: Self-Play-Infrastruktur
**Status:** Pending ⏳ | **Priority:** Medium
- Neues Binary `selfplay.rs`.
- Zwei Engine-Versionen gegeneinander spielen.
- Winrate-Tracking über 100+ Partien.

### Task 12: Criterion-Benchmarks
**Status:** Pending ⏳ | **Priority:** Low
- Benchmarks für Eval, Move-Gen, Suche.

## Learnings 📝

### Lessons.md-Einträge
- **Undo::default() initialisiert [Option<u16>; 2] mit [None, None]**: War überrascht, dass das korrekt funktioniert.
- **Flood-Fill bei recompute_caches()**: `is_connected()` nach Aufbau der Bitboards aufrufen, nicht davor.
- **Connected-Since Tracking**: Muss in `Undo` gespeichert werden, da `unmake_move` den vorherigen Zustand restoren muss.
