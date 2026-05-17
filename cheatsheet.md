# Cheat Sheet – Cython-Bot (Software Challenge 2026)

## Index

- [[#Board-Repräsentation]]
- [[#Spielregeln / Zugberechnung]]
- [[#Evaluation]]
	- [[#Schwarm-Analyse]]
	- [[#Bewertungsgewichte]]
	- [[#Tapered Evaluation]]
	- [[#Connect Bonus]]
- [[#Negamax / Alpha-Beta]]
	- [[#Transpositionstabelle (TT)]]
	- [[#Null-Move Pruning]]
	- [[#Futility Pruning]]
- [[#Zugsortierung]]
	- [[#Killer Moves]]
	- [[#History Heuristik]]
	- [[#Counter Moves]]
- [[#Late Move Reduction (LMR)]]
- [[#Principal Variation Search (PVS)]]
- [[#Aspiration Windows]]
- [[#Quiescence Search]]
- [[#Zobrist-Hashing]]
- [[#Zeitmanagement]]
- [[#Iterative Deepening (ID)]]
- [[#Cython-Dateiarten]]
- [[#Cython-Grundlagen]]
- [[#Wichtige Konstanten]]

---

## Board-Repräsentation

### Feldkodierung (1 Byte pro Feld)
```
Bits 0–1: Team   (0=leer, 1=Team1, 2=Team2, 3=Tintenfisch)
Bits 2–4: Wert   (Fischzahl der Figur, 1–5)
```
- `make_field(team, value)` → `(team & 0x3) | (value << 2)`
- `get_team(field)` → `field & 0x3`
- `get_value(field)` → `(field >> 2) & 0x7`

### Board-Layout
- `int8[100]` – 1D-Array, Index = `x * 10 + y`
- 10×10-Spielfeld, x = Spalte, y = Reihe
- 1D statt 2D: bessere Cache-Lokalität, `memcpy` für Kopien möglich

---

## Spielregeln / Zugberechnung

### Sprungweite (`c_get_target`)
1. Zähle alle Fische (Team1 + Team2) auf der Linie in **beide** Richtungen + sich selbst
2. Sprungweite = dieser Gesamtzählwert
3. Tintenfische sind transparent (zählen nicht mit, blockieren nicht)

### Zugvalidierung (`c_is_move_valid`)
Ein Zug ist **ungültig** wenn:
- Zielfeld außerhalb des Boards (< 0 oder ≥ 10)
- Zielfeld = Startfeld (kein Zug)
- Zielfeld gehört eigenem Team
- Zielfeld ist Tintenfisch
- **Gegnerische Figur liegt zwischen Start und Ziel** (darf nicht durchspringen)

### Siegbedingung
- Alle eigenen Fische in **einem** zusammenhängenden Schwarm → `best_value == total_material`
- Gegner hat keine Figuren mehr
- Nach Zug 60: Wer den größeren Hauptschwarm hat, gewinnt (Tiebreaker: Gesamtwert, dann Anzahl)

---

## Evaluation

### Schwarm-Analyse
BFS über alle eigenen Figuren → ergibt zusammenhängende Schwärme (`compute_swarm_data`).

| Feld | Bedeutung |
|------|-----------|
| `best_value` | Summe der Fischzahlen im **größten** Schwarm |
| `num_swarms` | Anzahl getrennter Schwärme |
| `best_swarm_size` | Anzahl Figuren im Hauptschwarm |
| `spread` | Chebyshev-Abstand der Schwarm-Zentroide voneinander |

### Bewertungsgewichte
| Variable | Wert | Bedeutung |
|----------|------|-----------|
| `W_BEST_SWARM` | 54.0 | Hauptschwarm-Wert-Differenz |
| `W_SWARM_COUNT` | 16.0 | Strafe pro extra Schwarm |
| `W_MATERIAL` | 23.0 | Gesamtmaterial-Differenz |
| `W_ISOLATED` | 6.0 | Strafe für Figuren außerhalb Hauptschwarm |
| `W_DISTANCE` | 2.0 | Strafe für Distanz isolierter Figuren zum Zentrum |
| `W_LINKS` | 2.0 | Bonus pro benachbartes Figurenpaar |
| `W_SPREAD` | 5.8 | Strafe für Schwarm-Spreizung |

> **Invariante:** `W_MATERIAL > W_ISOLATED` → jede verlorene Figur ist netto negativ.

### Tapered Evaluation
```
eg_phase = max(piece_phase, turn_phase)
piece_phase = (16 - eigene - gegnerische Figuren) / 12   [0..1]
turn_phase  = (turn - 20) / 40                            [0..1]
```
Endspielgewichte (`W_LATE_*`) werden mit `eg_phase` skaliert und addiert.
→ Konnektivität wird später im Spiel wichtiger.

### Connect Bonus
- Wenn Team genau **2 Schwärme** und **≤ 8 Figuren** hat:
  → prüfe ob irgendein Zug alle Figuren verbindet (`c_has_one_move_connect`)
  → wenn ja: +3768 Punkte (fast-Sieg-Signal)

---

## Negamax / Alpha-Beta

### Grundprinzip
```
negamax(board, depth, α, β) → score aus Sicht des ziehenden Spielers
```
- Score wird **negiert** beim rekursiven Aufruf: `score = -negamax(..., -β, -α)`
- Symmetrie von Nullsummenspielen: was gut für mich ist, ist schlecht für den Gegner

### Alpha-Beta Pruning
- `α` = bestes, was ich schon garantieren kann
- `β` = bestes, was der Gegner garantieren kann
- `α >= β` → Beta-Cutoff, restliche Züge werden nicht mehr untersucht

### Transpositionstabelle (TT)
- Speichert berechnete Positionen: `hash → (score, depth, flag, best_move)`
- 3 Flags: `TT_EXACT` (exaktes Ergebnis), `TT_LOWER` (Beta-Cutoff), `TT_UPPER` (Alpha-Cutoff)
- **4-Slot-Cluster** pro Hash-Bucket: reduziert Kollisionsverluste
- Ersetzungsstrategie: gleicher Hash → überschreiben wenn Tiefe besser; sonst ältester/flachster Slot

### Null-Move Pruning
```
Gegner bekommt einen "freien Zug" (board.turn += 1, kein echter Zug)
Wenn Gegner trotzdem Beta nicht schlägt → aktueller Knoten sicher prunen
```
- Deaktiviert bei: `depth < 4`, Endspiel (`turn >= 56`), wenig Figuren (Zugzwang-Gefahr)
- Reduktion: `NULL_MOVE_R = 2` Ebenen

### Futility Pruning
- Bei `depth <= 3` und nicht-PV-Knoten:
	- Wenn `static_eval - 360 * depth >= β` → Knoten überspringen (hoffnungslos)
	- Wenn `static_eval + 390 * depth <= α` → späte ruhige Züge überspringen

---

## Zugsortierung

Priorität (höchste zuerst):

| Priorität | Typ | Score |
|-----------|-----|-------|
| 1 | TT-Move (aus vorheriger Iteration) | 1.000.000 |
| 2 | Schlagzüge (MVV) | 100.000 + Wert × 1000 |
| 3 | Killer-Move Slot 0 | 50.000 |
| 4 | Counter-Move | 45.000 |
| 5 | Killer-Move Slot 1 | 40.000 |
| 6 | History-Heuristik | variabel |
| 7 | Zentrumsnähe | kleiner Bonus |

### Killer Moves
- Zwei ruhige Züge pro Tiefe, die zuletzt einen Beta-Cutoff erzeugt haben
- Werden zwischen Ästen desselben Tiefenniveaus übertragen

### History Heuristik
- `g_history[start*8+dir] += depth²` bei Beta-Cutoff
- Altern nach jeder ID-Iteration: `* 7/8` (damit alte Info verfällt)

### Counter Moves
- Für jeden Gegner-Zug wird der beste Widerlegungszug gespeichert
- Indiziert über `(start_x*10+start_y)*8+direction`

---

## Late Move Reduction (LMR)

```
Wenn depth >= 3 und move_num >= 3:
    reduction = 1
    Wenn depth >= 6 und move_num >= 8:   reduction = 2
    Wenn depth >= 10 und move_num >= 14: reduction = 3
```
- Ausnahmen: PV-Knoten, Schlagzüge, flache Tiefen
- Bei LMR: erst Null-Fenster mit reduzierter Tiefe → wenn score > α: volle Re-Suche

---

## Principal Variation Search (PVS)

```
Erster Zug:  Volle Suche [-β, -α]
Folge-Züge:  Null-Fenster [-α-1, -α]
             → score > α UND score < β: Re-Suche mit vollem Fenster
```
Spart Rechenzeit, weil die meisten Züge durch das Null-Fenster schnell widerlegt werden.

---

## Aspiration Windows

```
Starte mit Fenster [best_score - δ, best_score + δ], δ = 100
Fail Low  (score ≤ Untergrenze): δ *= 4, Untergrenze neu setzen, wiederholen
Fail High (score ≥ Obergrenze): δ *= 4, Obergrenze neu setzen, wiederholen
```
Beschleunigt ab Tiefe 4. Verhindert fälschliches Pruning bei unerwarteten Ergebnissen.

---

## Quiescence Search

- Wird bei `depth == 0` aufgerufen (statt direkt evaluate)
- Betrachtet nur **Schlagzüge** → verhindert Horizont-Effekt
- `stand_pat` = statischer Eval ohne weiteren Zug → dient als untere Alpha-Schranke
- Abbruch bei `stand_pat >= β` oder `qdepth >= 10`

---

## Zobrist-Hashing

### Initialisierung
- Zufällige 64-Bit-Zahlen für jede Kombination: `[x][y][team][value]`
- Separate Zufallszahlen pro Zugnummer: `ZOBRIST_TURN[61]`
- Pseudozufalls-Generator: xorshift64

### Inkrementelles Update
```
h ^= ZOBRIST_TURN[old_turn]            → alten Turn heraus
h ^= ZOBRIST_TURN[new_turn]            → neuen Turn hinein
h ^= ZOBRIST_PIECE[sx][sy][t][v]       → Figur vom Start heraus
h ^= ZOBRIST_PIECE[tx][ty][ct][cv]     → geschlagene Figur heraus (falls vorhanden)
h ^= ZOBRIST_PIECE[tx][ty][t][v]       → Figur am Ziel hinein
```
XOR ist selbstinvers → kein Neuberechnen der gesamten Stellung nötig.

---

## Zeitmanagement

- `TIME_LIMIT = 1.9s` (Puffer vor Server-Timeout von 2s)
- `TIME_USAGE_FRACTION = 0.88` → stoppt ID-Loop bei 88% der Zeit
- `check_timeout()` alle **1024 Knoten** aufgerufen → setzt `g_timeout_flag`
- Bei Timeout: aktuellster `best_move` aus TT wird zurückgegeben

---

## Iterative Deepening (ID)

```
depth = 1, 2, 3, ...
→ Suche immer vollständig abschließen bevor depth++
→ TT speichert beste Züge aus vorherigen Iterationen (wichtig für Zugsortierung)
→ Timeout → letzten vollständigen best_move nehmen
```
Vorteil: Falls Zeit knapp wird, hat man immer ein valides Ergebnis.

---

## Cython-Dateiarten

| Endung | Bedeutung | Analogie |
|--------|-----------|----------|
| `.pyx` | Implementierungsdatei – enthält den eigentlichen Code | wie `.c` in C |
| `.pxd` | Deklarationsdatei – nur Typ- und Funktions-Signaturen, kein ausführbarer Code | wie `.h` in C |
| `.pyd` | Kompiliertes Modul unter **Windows** (unter Linux: `.so`) – wird beim Build erzeugt | wie `.dll` |

### Warum `.pxd`?
- Andere `.pyx`-Dateien können mit `cimport` die deklarierten `cdef`-Symbole importieren
- Ohne `.pxd` sind `cdef`-Funktionen für andere Module unsichtbar
- Im Projekt: `search.pyx` importiert via `cimport` aus `board.pxd` und `evaluate.pxd` → direkter C-Aufruf ohne Python-Overhead

```
board.pxd   ← deklariert: CBoard, CMove, c_generate_moves, ...
board.pyx   ← implementiert alles davon
search.pyx  ← cimport board → kann c_generate_moves direkt als C-Aufruf nutzen
```

---

## Cython-Grundlagen

### Deklarationsarten

| Deklaration | Aufrufbar von | Geschwindigkeit |
|-------------|--------------|-----------------|
| `def` | Python only | langsam |
| `cpdef` | Python + C | mittel |
| `cdef` | C only | schnell |

### Wichtige Direktiven
```
boundscheck=False  → kein Array-Grenzcheck
wraparound=False   → keine negativen Indizes
cdivision=True     → C-Division (kein Python-ZeroDivisionError-Check)
```

### `noexcept nogil`
- `noexcept` → Cython fügt keinen Exception-Check nach dem Aufruf ein
- `nogil` → gibt den Python GIL frei (für reine C-Funktionen sinnvoll)

### Structs
```cython
cdef struct CMove:
    int start_x, start_y, direction, target_x, target_y

cdef struct CMoveList:
    CMove moves[200]
    int count
```
Stack-allokiert, kein Heap-Overhead.

---

## Wichtige Konstanten

| Konstante | Wert | Bedeutung |
|-----------|------|-----------|
| `WIN_SCORE` | 100.000 | Siegwert (± erkannter Gewinn/Verlust) |
| `INF` | 1.000.000 | Initialer Alpha/Beta-Wert |
| `TT_CLUSTER_SIZE` | 4 | Slots pro TT-Bucket |
| `TT_CLUSTER_COUNT` | 262.144 | Anzahl Buckets (= 2^18) |
| `MAX_DEPTH` | 40 | Maximale Suchtiefe |
| `NULL_MOVE_R` | 2 | Null-Move-Reduktion |
| `QSEARCH_MAX_DEPTH` | 10 | Max Quiescence-Tiefe |
| `CONNECT_BONUS` | 3.768 | Bonus für One-Move-Connect |
| `HISTORY_CAP` | 1.000.000 | Max History-Wert |
