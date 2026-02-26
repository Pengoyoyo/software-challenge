# Tutorial: Starker Piranhas-Bot als Rust-Baseline

Dieses Dokument ist eine **Baseline** fuer einen neuen Bot. Es beschreibt die besten, praxisnahen Methoden fuer hohe Spielstaerke unter hartem Zeitlimit. Nicht alles muss direkt umgesetzt sein.

## 1) Zielbild

- Regelkonformer, deterministischer Bot.
- Hardcap pro Zug strikt einhalten (z. B. `1.85s`).
- Maximale Tiefe/NPS ohne Instabilitaet.
- Saubere Pipeline: Korrektheit -> Speed -> Staerke -> Tuning.

## 2) Architektur (sprachagnostisch, fuer Rust geeignet)

Empfohlene Module:

- `state`: Board, Move, make/unmake, Hash, Terminalchecks.
- `movegen`: legale Zuege + Capture-Zuege.
- `eval`: schnelle HCE (hand-crafted evaluation).
- `search`: ID + Negamax + Alpha-Beta + PVS + QSearch.
- `tt`: Transposition Table.
- `time`: Hard/Soft-Deadline und Iterationskontrolle.
- `ordering`: TT move, tactical, killer/history/counter.
- `book`: deterministisches Opening-Book.
- `bench/tests`: Regression, Determinismus, Timecap.

## 3) Absolut kritische Korrektheit

Vor jeder Optimierung muss das stimmen:

- Zugweite/Line-Rule exakt.
- Gegner blockieren korrekt.
- Kraken-Regeln korrekt.
- Schlag nur auf Zielfeld.
- Endwertung ist **wertbasiert** (Fischwerte 1/2/3), nicht nur Anzahl.

Wichtig: Wenn interne Repr. Fischwerte ignoriert, optimiert der Bot auf falsches Ziel.

## 4) Suchkern (starker Standard)

Pflicht-Bausteine:

- Iterative Deepening.
- Negamax + Alpha-Beta.
- PVS (erste Variante voll, Rest Null-Window + Re-Search).
- Aspiration Windows ab mittlerer Tiefe.
- TT mit `EXACT/LOWER/UPPER` Bounds.
- Quiescence Search statt `depth == 0` sofort static eval.

### Selektive Verfahren (konservativ starten)

- LMR (late move reductions).
- LMP (late move pruning) fuer spaete ruhige Zuege.
- Futility / Reverse Futility auf kleinen Tiefen.
- Null-Move-Pruning + Verification Search.
- Singular Extension (nur wenn TT-Move klar dominiert).

Faustregel: Erst konservative Schwellen setzen, danach datengetrieben tunen.

## 5) Move Ordering (groesster Elo-Hebel pro Aufwand)

Empfohlene Reihenfolge:

1. TT-Move
2. starke taktische Zuege (Captures, Connectivity-Swing)
3. Killer-Moves
4. Counter-Move
5. History/Continuation-History

Deterministische Tie-Breaks erzwingen (kein Zufall).

## 6) QSearch fuer Piranhas (nicht nur Capture)

Noisy-Set sollte enthalten:

- Captures
- Nicht-Captures mit starkem Connectivity-Effekt (Komponenten-Verbesserung, hoher lokaler Swing)

Stabilisierung:

- Top-K noisy moves pro Knoten.
- Delta-Pruning.
- Stand-pat nur, wenn sinnvoll.

## 7) Evaluation (schnell + korrekt)

Kernfeatures:

- Groesster zusammenhaengender Schwarmwert (wertbasiert).
- Komponentenanzahl.
- Spread/Kohesion.
- Mobilitaet (selektiv berechnen, nicht immer).
- Center/Structure/Links.
- 1-ply Threats: one-move-connect / one-move-disconnect.

Phasenlogik:

- Frueh/Mitte: etwas mehr Mobilitaet/Struktur.
- Spaet: Schwarmabschluss/Threats deutlich hoeher gewichten.

## 8) Zeitmanagement (Hardcap zuerst)

Empfohlene Politik:

- Hard deadline z. B. `1.85s`.
- Interne Reserve fuer Rueckgabe/Overhead.
- Naechste Iteration nur starten, wenn Restzeit > prognostische Iterationskosten + Safety.
- `best_move_so_far` jederzeit gueltig halten.

Wenn Time-Management instabil ist, ist jede Elo-Verbesserung wertlos.

## 9) Persistentes Suchgedaechtnis ueber Zuege

Sehr effektiv:

- TT ueber Zuege persistent (nicht pro Zug clearen).
- Histories nicht resetten, sondern pro Root-Aufruf dämpfen (decay).
- Reply-Cache (vorgeplante Antworten).
- Root-Subtree-Reuse (PV-Mapping auf Folgepositionen).

## 10) Opening-Book (deterministisch)

- Nur stabile, qualitativ klare Eintraege.
- Book nur bis fruehe Ply-Grenze (z. B. 12-16).
- Fallback immer auf normale Suche.
- Keine Zufallsauswahl im Matchbetrieb.

Prioritaet am Root: `Book > Subtree-Reuse > Reply-Cache > TT/History`.

## 11) Anti-Shuffle / Repetition

- Wiederholungen als negatives Signal in Eval/Search.
- Besser adaptiv nach Wiederholungsdistanz statt fixer Strafwert.
- Deterministisch bleiben.

## 12) Telemetrie (pro Zug verpflichtend)

Mindestens loggen:

- depth, nodes, qnodes, nps
- tt_probes, tt_hits, tt_hit_rate
- fail-high/fail-low
- best-move-changes
- reply hits, subtree reuse hits, book hits
- verification nodes, singular extensions
- elapsed_ms, hardcap margin

Ohne Telemetrie kein serioeses Tuning.

## 13) Tests (muss gruen sein)

- Regeltests komplett.
- make/unmake + hash reversibel.
- Determinismus: gleiche Position + Config => gleicher Zug/Score/Depth.
- Timecap-Tests mit vielen Wiederholungen (kein Deadline-Miss).
- Book/Subtree/Verification spezifische Tests.

## 14) Benchmark- und Tuning-Loop

Standard-Loop:

1. Feste Bench-Positionen (opening/mid/endgame).
2. A/B Bench gegen Baseline.
3. Selfplay Matches.
4. SPRT-Gate fuer Merge (`H1` akzeptiert).

Nur mergen wenn:

- keine Timecap-Regression
- keine Legalitaets-Regression
- SPRT positiv

## 15) Reihenfolge fuer einen neuen Bot (empfohlen)

1. Regelkorrektheit + value-aware state.
2. ID + Alpha-Beta + PVS + TT + gutes Ordering.
3. Hardcap-robustes Time Management.
4. QSearch + konservative Pruning-Techniken.
5. Persistentes Suchgedaechtnis (TT/History/Reply/Subtree).
6. Opening-Book.
7. Datengetriebenes Tuning (SPRT).

## 16) Rust-Implementierungshinweise (nur pragmatisch)

- Hotpaths ohne unnoetige Allokationen.
- Feste Buffer fuer Move-Listen/Scratch.
- make/unmake statt dauerndes Clonen von State.
- Daten lokal/kompakt halten (Cache-freundlich).
- Deterministische Iterationsreihenfolgen erzwingen.

---

Das ist eine starke, turniertaugliche Baseline. Danach kommt Elo vor allem aus sauberem Tuning, nicht aus immer komplexeren Einzelideen.
