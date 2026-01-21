# Cython Core - Ausführliche Code-Dokumentation

Diese Dokumentation erklärt den Cython-Code Zeile für Zeile für Personen ohne C-Erfahrung.

## Was ist Cython?

Cython ist eine Programmiersprache, die Python-Syntax mit C-Typen kombiniert. Der Vorteil: Man kann Python-Code schreiben, der fast so schnell wie reines C läuft. Cython-Dateien haben die Endung `.pyx` (Implementierung) und `.pxd` (Header/Deklarationen).

---

# 1. board.pxd - Header-Datei (Deklarationen)

Diese Datei definiert, welche Funktionen und Klassen existieren - wie ein "Inhaltsverzeichnis".

```cython
# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True
```
**Compiler-Direktiven:**
- `language_level=3`: Verwende Python 3 Syntax
- `boundscheck=False`: Keine Prüfung ob Array-Zugriffe gültig sind (schneller, aber unsicher)
- `wraparound=False`: Negative Indizes wie `array[-1]` funktionieren nicht (schneller)
- `cdivision=True`: Verwende C-Division statt Python-Division (schneller, aber Division durch 0 = undefiniert)

```cython
ctypedef signed char int8
ctypedef unsigned long long uint64
```
**Typdefinitionen (wie `typedef` in C):**
- `int8`: Eine 8-Bit Zahl mit Vorzeichen (-128 bis 127)
- `uint64`: Eine 64-Bit Zahl ohne Vorzeichen (0 bis 18.446.744.073.709.551.615)

```cython
cdef int[8][2] DIRECTION_VECTORS
```
**C-Level Variable:** Ein 2D-Array mit 8 Zeilen und 2 Spalten für Richtungsvektoren.
- `cdef` = "C definition" - nur auf C-Ebene sichtbar, nicht von Python aus

```cython
cdef int8 make_field(int team, int value) noexcept nogil
cdef int get_team(int8 field) noexcept nogil
cdef int get_value(int8 field) noexcept nogil
```
**Funktionsdeklarationen:**
- `noexcept`: Die Funktion wirft keine Exceptions (erlaubt Optimierungen)
- `nogil`: Kann ohne Python's "Global Interpreter Lock" laufen (ermöglicht echte Parallelität)

```cython
cdef class CBoard:
    cdef int8[100] fields
    cdef public int turn
```
**Klasse mit C-Attributen:**
- `fields`: Array mit 100 Elementen (10x10 Spielfeld, als 1D-Array gespeichert)
- `public`: Das Attribut `turn` ist auch von Python aus zugreifbar

```cython
cpdef CBoard copy(self)
```
- `cpdef` = "C and Python definition" - von C UND Python aus aufrufbar

---

# 2. board.pyx - Spielbrett-Implementierung

## Importe und Konstanten

```cython
cimport cython
from libc.string cimport memcpy
```
- `cimport`: Importiert C-Level Deklarationen (nicht Python-Objekte)
- `memcpy`: C-Funktion zum schnellen Kopieren von Speicherbereichen

```cython
DEF TEAM_NONE = 0
DEF TEAM_ONE = 1
DEF TEAM_TWO = 2
DEF TEAM_SQUID = 3
DEF FIELD_EMPTY = 0
DEF FIELD_TYPE_SQUID = 6
```
**Kompilierzeit-Konstanten:** `DEF` definiert Konstanten, die zur Kompilierzeit ersetzt werden (wie `#define` in C).

## Richtungsvektoren

```cython
cdef int[8][2] DIRECTION_VECTORS = [
    [0, 1],    # 0: Up        (y+1)
    [1, 1],    # 1: UpRight   (x+1, y+1)
    [1, 0],    # 2: Right     (x+1)
    [1, -1],   # 3: DownRight (x+1, y-1)
    [0, -1],   # 4: Down      (y-1)
    [-1, -1],  # 5: DownLeft  (x-1, y-1)
    [-1, 0],   # 6: Left      (x-1)
    [-1, 1],   # 7: UpLeft    (x-1, y+1)
]
```
**8 mögliche Bewegungsrichtungen** auf dem Spielfeld. Jede Richtung ist ein Vektor [dx, dy].

## Bit-Manipulation für Feld-Kodierung

```cython
cdef int8 make_field(int team, int value) noexcept nogil:
    return <int8>((team & 0x3) | ((value & 0x7) << 2))
```
**Erklärt:**
Diese Funktion packt Team und Wert in ein einziges Byte:

```
Bit-Layout: VVVTT
            │││││
            │││└┴── Team (Bits 0-1): 0-3
            └┴┴──── Value (Bits 2-4): 0-7
```

- `team & 0x3`: Nimmt nur die unteren 2 Bits (Werte 0-3)
- `value & 0x7`: Nimmt nur die unteren 3 Bits (Werte 0-7)
- `<< 2`: Verschiebt um 2 Bits nach links
- `|`: Kombiniert beide Werte mit bitweisem ODER
- `<int8>`: Expliziter Cast (Typumwandlung) zu int8

**Beispiel:**
```
Team=1 (binär: 01), Value=3 (binär: 011)
make_field(1, 3):
  team & 0x3     = 00000001 (=1)
  value & 0x7    = 00000011 (=3)
  (value) << 2   = 00001100 (=12)
  Ergebnis: 01 | 00001100 = 00001101 (=13)
```

```cython
cdef int get_team(int8 field) noexcept nogil:
    return field & 0x3
```
**Extrahiert das Team** durch Maskieren mit `0x3` (binär: 11), behält nur die unteren 2 Bits.

```cython
cdef int get_value(int8 field) noexcept nogil:
    return (field >> 2) & 0x7
```
**Extrahiert den Wert:**
- `>> 2`: Verschiebt um 2 Bits nach rechts (Team-Bits fallen weg)
- `& 0x7`: Maskiert auf 3 Bits

## Die CBoard-Klasse

```cython
cdef class CBoard:
    def __cinit__(self):
        cdef int i
        for i in range(100):
            self.fields[i] = FIELD_EMPTY
        self.turn = 0
```
- `__cinit__`: C-Konstruktor (wird VOR `__init__` aufgerufen)
- Initialisiert alle 100 Felder als leer und Zug-Zähler auf 0

```cython
    cdef int8 get_field(self, int x, int y) noexcept nogil:
        return self.fields[x * 10 + y]
```
**2D zu 1D Konvertierung:**
- Das Spielfeld ist intern als 1D-Array gespeichert
- Position (x,y) → Index: `x * 10 + y`
- Bei einem 10x10 Brett: (3,5) → 3*10+5 = 35

```cython
    cdef void set_field(self, int x, int y, int8 field) noexcept nogil:
        self.fields[x * 10 + y] = field
```
Setzt ein Feld an Position (x,y).

```cython
    cpdef CBoard copy(self):
        cdef CBoard new_board = CBoard()
        cdef int i
        for i in range(100):
            new_board.fields[i] = self.fields[i]
        new_board.turn = self.turn
        return new_board
```
**Tiefe Kopie:** Erstellt ein komplett neues Board mit kopierten Werten.

## Spielstand-Konvertierung

```cython
cpdef CBoard from_game_state(object game_state):
```
Konvertiert ein Python-GameState-Objekt in das schnelle CBoard-Format.

```cython
    for y in range(10):
        for x in range(10):
            ft = game_state.board.map[y][x]
            ft_int = int(ft)

            if ft_int == FIELD_TYPE_SQUID:
                board.set_field(x, y, make_field(TEAM_SQUID, 0))
            else:
                py_team = ft.get_team()
                if py_team is None:
                    board.set_field(x, y, FIELD_EMPTY)
                else:
                    team_int = TEAM_ONE if int(py_team) == 0 else TEAM_TWO
                    value = ft.get_value()
                    board.set_field(x, y, make_field(team_int, value))
```
Iteriert über alle Felder und konvertiert:
- Kraken → TEAM_SQUID
- Leere Felder → FIELD_EMPTY
- Spielerfiguren → Team + Wert kombiniert

## Fische zählen auf einer Linie

```cython
cdef int count_fish_on_line(CBoard board, int x, int y, int dx, int dy) noexcept nogil:
    cdef int count = 0
    cdef int nx = x + dx
    cdef int ny = y + dy
    cdef int8 field
    cdef int field_team

    while 0 <= nx < 10 and 0 <= ny < 10:
        field = board.get_field(nx, ny)
        field_team = get_team(field)
        if field_team == TEAM_ONE or field_team == TEAM_TWO:
            count += 1
        nx += dx
        ny += dy

    return count
```
**Algorithmus:**
1. Starte bei (x+dx, y+dy)
2. Gehe in Richtung (dx, dy) weiter
3. Zähle alle Fische (Felder mit Team 1 oder 2)
4. Stoppe am Spielfeldrand

## Zielposition berechnen

```cython
cpdef tuple get_target_position(CBoard board, int start_x, int start_y, int direction):
    cdef int dx = DIRECTION_VECTORS[direction][0]
    cdef int dy = DIRECTION_VECTORS[direction][1]

    cdef int fish_count = 1  # Die Figur selbst
    fish_count += count_fish_on_line(board, start_x, start_y, dx, dy)
    fish_count += count_fish_on_line(board, start_x, start_y, -dx, -dy)

    cdef int target_x = start_x + (fish_count * dx)
    cdef int target_y = start_y + (fish_count * dy)

    return (target_x, target_y)
```
**Spielregel:** Eine Figur bewegt sich so viele Felder, wie Fische auf der gesamten Linie (in beide Richtungen + die Figur selbst) stehen.

## Zug ausführen

```cython
cpdef CBoard apply_move(CBoard board, int start_x, int start_y, int direction):
    cdef CBoard new_board = board.copy()
    cdef int target_x, target_y
    cdef int8 moving_piece = board.get_field(start_x, start_y)

    target_x, target_y = get_target_position(board, start_x, start_y, direction)

    new_board.set_field(start_x, start_y, FIELD_EMPTY)
    new_board.set_field(target_x, target_y, moving_piece)
    new_board.turn = board.turn + 1

    return new_board
```
**Ablauf:**
1. Kopiere das Brett
2. Berechne Zielposition
3. Leere Startfeld
4. Setze Figur auf Zielfeld
5. Erhöhe Zugzähler

## Zug-Validierung

```cython
cdef bint is_move_valid(...) noexcept nogil:
```
- `bint`: Boolean-Typ in Cython (0=False, 1=True)

```cython
    if target_x < 0 or target_x >= 10 or target_y < 0 or target_y >= 10:
        return False  # Außerhalb des Spielfelds
```

```cython
    if target_x == start_x and target_y == start_y:
        return False  # Keine Bewegung
```

```cython
    field = board.get_field(target_x, target_y)
    field_team = get_team(field)

    if field_team == team:
        return False  # Eigene Figur am Ziel
    if field_team == TEAM_SQUID:
        return False  # Krake am Ziel
```

```cython
    nx = start_x + dx
    ny = start_y + dy
    while nx != target_x or ny != target_y:
        field = board.get_field(nx, ny)
        field_team = get_team(field)
        if field_team == opp_team:
            return False  # Gegnerische Figur im Weg
        nx += dx
        ny += dy

    return True
```
Prüft ob der Weg frei von gegnerischen Figuren ist.

## Züge generieren

```cython
cpdef list generate_moves(CBoard board, int team):
    cdef list moves = []

    for x in range(10):
        for y in range(10):
            field = board.get_field(x, y)
            if get_team(field) != team:
                continue  # Nicht unsere Figur

            for d in range(8):  # 8 Richtungen
                target_x, target_y = get_target_position(board, x, y, d)
                if is_move_valid(board, x, y, d, team, target_x, target_y):
                    moves.append((x, y, d, target_x, target_y))

    return moves
```
Findet alle gültigen Züge für ein Team.

---

# 3. evaluate.pyx - Stellungsbewertung

## Datenstruktur für Schwärme

```cython
cdef struct SwarmData:
    int best_value          # Punktwert des besten Schwarms
    int num_swarms          # Anzahl der Schwärme
    int best_swarm_size     # Größe des besten Schwarms
    double center_x         # X-Zentrum des besten Schwarms
    double center_y         # Y-Zentrum des besten Schwarms
    uint64 best_swarm_mask_lo  # Bitmaske für Positionen 0-63
    uint64 best_swarm_mask_hi  # Bitmaske für Positionen 64-99
```
Ein `struct` ist wie eine Python-Klasse, aber ohne Methoden - nur Daten.

## Schwarm-Berechnung (BFS)

```cython
cdef SwarmData compute_swarm_data(CBoard board, int team) noexcept:
```

**Breadth-First Search (BFS)** findet zusammenhängende Gruppen:

```cython
    cdef bint[100] visited        # Wurde Feld schon besucht?
    cdef int[100] queue_x         # X-Koordinaten-Warteschlange
    cdef int[100] queue_y         # Y-Koordinaten-Warteschlange
    cdef int qfront, qback        # Zeiger für Warteschlange
```

**Warteschlangen-Prinzip:**
- `qfront`: Index des nächsten zu verarbeitenden Elements
- `qback`: Index für das nächste einzufügende Element
- Wenn `qfront < qback`: Es gibt noch Elemente zu verarbeiten

```cython
    for x in range(10):
        for y in range(10):
            idx = x * 10 + y
            if visited[idx]:
                continue

            field = board.fields[idx]
            if get_team(field) != team:
                continue

            # Neuen Schwarm gefunden - starte BFS
            data.num_swarms += 1

            # Initialisiere Warteschlange
            qfront = 0
            qback = 0
            queue_x[qback] = x
            queue_y[qback] = y
            qback += 1
            visited[idx] = True

            while qfront < qback:
                cx = queue_x[qfront]
                cy = queue_y[qfront]
                qfront += 1  # Element aus Warteschlange entfernen

                # Verarbeite aktuelles Feld
                swarm_value += get_value(board.fields[cx * 10 + cy])
                swarm_size += 1

                # Prüfe alle 8 Nachbarn
                for i in range(8):
                    nx = cx + NEIGHBOR_OFFSETS[i][0]
                    ny = cy + NEIGHBOR_OFFSETS[i][1]

                    if nx < 0 or nx >= 10 or ny < 0 or ny >= 10:
                        continue  # Außerhalb

                    nidx = nx * 10 + ny
                    if visited[nidx]:
                        continue  # Schon besucht

                    if get_team(board.fields[nidx]) == team:
                        visited[nidx] = True
                        queue_x[qback] = nx
                        queue_y[qback] = ny
                        qback += 1  # Element hinzufügen
```

## Bitmaske für Schwarm-Mitgliedschaft

```cython
    if swarm_value > data.best_value:
        # Speichere Positionen als Bitmaske
        data.best_swarm_mask_lo = 0
        data.best_swarm_mask_hi = 0
        for i in range(swarm_size):
            bidx = current_swarm_indices[i]
            if bidx < 64:
                data.best_swarm_mask_lo |= (1ULL << bidx)
            else:
                data.best_swarm_mask_hi |= (1ULL << (bidx - 64))
```

**Bit-Operationen erklärt:**
- `1ULL`: Die Zahl 1 als unsigned long long (64 Bit)
- `1ULL << bidx`: Verschiebt die 1 um `bidx` Stellen nach links
  - `1ULL << 0` = 0000...0001 (Position 0)
  - `1ULL << 3` = 0000...1000 (Position 3)
- `|=`: Setzt das Bit (bitweises ODER und Zuweisung)

**Warum zwei Masken?**
- Ein `uint64` hat nur 64 Bits
- Das Brett hat 100 Positionen
- Daher: Positionen 0-63 in `mask_lo`, Positionen 64-99 in `mask_hi`

## Prüfen ob Position im Schwarm

```cython
cdef bint is_in_best_swarm(SwarmData* data, int idx) noexcept:
    if idx < 64:
        return (data.best_swarm_mask_lo & (1ULL << idx)) != 0
    else:
        return (data.best_swarm_mask_hi & (1ULL << (idx - 64))) != 0
```
- `SwarmData*`: Zeiger auf SwarmData (Pointer, wie Referenz aber auf C-Ebene)
- `&`: Bitweises UND - ergibt nur dann nicht-null, wenn das Bit gesetzt ist

## Bewertungsfunktion

```cython
cpdef double evaluate(CBoard board, int our_team):
    cdef int opp_team = TEAM_TWO if our_team == TEAM_ONE else TEAM_ONE
```
Bestimmt das gegnerische Team.

```cython
    # Gewinn/Verlust-Bedingungen
    if our_data.num_swarms == 0:
        return -WIN_SCORE  # Wir haben verloren
    if opp_data.num_swarms == 0:
        return WIN_SCORE   # Wir haben gewonnen

    if board.turn >= 60:  # Spielende nach 60 Zügen
        if our_data.best_value > opp_data.best_value:
            return WIN_SCORE
        elif opp_data.best_value > our_data.best_value:
            return -WIN_SCORE
```

```cython
    cdef double value = 0.0

    # Schwarm-Wert-Differenz (sehr wichtig: Faktor 18)
    value += (our_data.best_value - opp_data.best_value) * 18.0

    # Weniger Schwärme ist besser (Strafe für Fragmentierung)
    value -= (our_data.num_swarms - 1) * 4.0
    value += (opp_data.num_swarms - 1) * 4.0
```

```cython
    # Material-Zählung und Isolations-Strafen
    for x in range(10):
        for y in range(10):
            ...
            if t == our_team:
                our_material += val
                if not is_in_best_swarm(&our_data, idx):
                    # Isolierte Figur - berechne Distanz zum Schwarm-Zentrum
                    dx = x - our_data.center_x
                    dy = y - our_data.center_y
                    our_dist += sqrt(dx * dx + dy * dy)
                    our_isolated += val
```
- `&our_data`: Adresse von `our_data` (Pointer erstellen)
- `sqrt(dx*dx + dy*dy)`: Euklidische Distanz (Pythagoras)

```cython
    value += (our_material - opp_material) * 2.0   # Material-Vorteil
    value -= our_isolated * 3.0                    # Strafe für isolierte Figuren
    value += opp_isolated * 3.0                    # Bonus wenn Gegner isoliert
    value -= our_dist * 0.7                        # Strafe für Distanz zum Zentrum
    value += opp_dist * 0.7                        # Bonus wenn Gegner weit weg
```

---

# 4. search.pyx - Suchalgorithmus

## Transpositionstabelle

```cython
cdef struct TTEntry:
    uint64 hash_key     # Zobrist-Hash der Position
    double score        # Bewertung
    int depth           # Suchtiefe bei Berechnung
    int flag            # Typ: EXACT, LOWER, UPPER
    int move_start_x    # Bester Zug (Start-X)
    int move_start_y    # Bester Zug (Start-Y)
    int move_direction  # Bester Zug (Richtung)
    int move_target_x   # Bester Zug (Ziel-X)
    int move_target_y   # Bester Zug (Ziel-Y)
```

```cython
cdef TTEntry* tt = NULL
```
- `TTEntry*`: Zeiger auf TTEntry-Array
- `NULL`: Null-Zeiger (noch nicht initialisiert)

```cython
cpdef void init_search():
    global tt
    if tt == NULL:
        tt = <TTEntry*>malloc(TT_SIZE * sizeof(TTEntry))
```
- `malloc`: Reserviert Speicher auf dem Heap (wie `new` in C++)
- `sizeof(TTEntry)`: Größe einer TTEntry-Struktur in Bytes
- `TT_SIZE * sizeof(TTEntry)`: Speicher für 1.048.576 Einträge

## SearchState-Klasse

```cython
cdef class SearchState:
    cdef int[30][2] killer_moves  # Killer-Züge pro Tiefe
    cdef int[800] history         # History-Heuristik
```

**Killer Moves:** Züge, die in Geschwister-Knoten einen Beta-Cutoff verursacht haben - oft auch hier gut.

**History Heuristik:** Statistik darüber, welche Züge historisch gut waren.

```cython
    cdef inline bint is_timeout(self):
        cdef double elapsed = (<double>clock() / CLOCKS_PER_SEC) - self.start_time
        return elapsed >= self.time_limit
```
- `inline`: Compiler soll Funktion an Aufrufstelle einfügen (schneller)
- `clock()`: CPU-Zeit in "Ticks"
- `CLOCKS_PER_SEC`: Ticks pro Sekunde
- Division ergibt Sekunden

```cython
    cdef inline void update_killer(self, int move_idx, int depth):
        if depth < 30 and move_idx >= 0:
            if self.killer_moves[depth][0] != move_idx:
                self.killer_moves[depth][1] = self.killer_moves[depth][0]
                self.killer_moves[depth][0] = move_idx
```
Speichert die zwei besten Killer-Züge pro Tiefe (LIFO-Prinzip).

```cython
    cdef inline void update_history(self, int start_x, int start_y, int direction, int depth):
        cdef int key = (start_x * 10 + start_y) * 8 + direction
        if key < 800:
            self.history[key] += depth * depth
```
**History-Key-Berechnung:**
- 100 Positionen × 8 Richtungen = 800 mögliche Züge
- `depth * depth`: Tiefere Cutoffs sind wertvoller (quadratische Gewichtung)

## Zug-Sortierung

```cython
cdef list order_moves(SearchState state, list moves, int depth, tuple tt_move):
    for i, m in enumerate(moves):
        score = 0

        # TT-Zug bekommt höchste Priorität
        if tt_move is not None and m[0] == tt_move[0] and m[1] == tt_move[1] and m[2] == tt_move[2]:
            score += 100000

        # Killer-Züge
        if depth < 30:
            if i == state.killer_moves[depth][0]:
                score += 5000
            elif i == state.killer_moves[depth][1]:
                score += 4000

        # History-Wert
        score += state.get_history(m[0], m[1], m[2])

        # Bevorzuge Züge zur Mitte
        score -= abs(m[3] - 5) * 10 + abs(m[4] - 5) * 10

        scored.append((score, i, m))

    scored.sort(reverse=True)  # Höchster Score zuerst
```

## Alpha-Beta Suche

```cython
cdef tuple alpha_beta(
    SearchState state,
    CBoard board,
    uint64 state_hash,
    int depth,
    double alpha,    # Beste garantierte Bewertung für MAX
    double beta,     # Beste garantierte Bewertung für MIN
    bint maximizing  # True = MAX-Spieler, False = MIN-Spieler
):
```

**Alpha-Beta erklärt:**
- `alpha`: "Ich (MAX) kann mindestens diesen Wert erreichen"
- `beta`: "Der Gegner (MIN) kann mich auf höchstens diesen Wert beschränken"
- Wenn `alpha >= beta`: Abschneiden (Cutoff) - dieser Ast ist irrelevant

```cython
    # Transpositionstabelle prüfen
    cdef int tt_idx = <int>(state_hash % TT_SIZE)
    cdef TTEntry* entry = &tt[tt_idx]

    if entry.hash_key == state_hash:
        if entry.depth >= depth:
            state.tt_hits += 1

            if entry.flag == TT_EXACT:
                return (entry.score, tt_move)  # Exakter Wert bekannt
            elif entry.flag == TT_LOWER:
                alpha = max(alpha, entry.score)  # Mindestens dieser Wert
            elif entry.flag == TT_UPPER:
                beta = min(beta, entry.score)  # Höchstens dieser Wert

            if alpha >= beta:
                return (entry.score, tt_move)  # Cutoff
```

```cython
    if depth == 0:
        return (evaluate(board, state.our_team), None)  # Blattknoten
```

```cython
    if maximizing:
        best_score = -INF
        for i, m in enumerate(moves):
            new_board = apply_move(board, m[0], m[1], m[2])
            new_hash = update_hash_move(...)

            score, _ = alpha_beta(state, new_board, new_hash, depth - 1, alpha, beta, False)

            if score > best_score:
                best_score = score
                best_move = m

            if score > alpha:
                alpha = score
                state.update_history(m[0], m[1], m[2], depth)

            if beta <= alpha:
                state.update_killer(i, depth)
                break  # Beta-Cutoff
```

**MAX-Spieler:**
- Will höchsten Wert
- Aktualisiert `alpha` nach oben
- Cutoff wenn `alpha >= beta` (MIN würde diesen Ast nie wählen)

```cython
    else:  # minimizing
        best_score = INF
        for i, m in enumerate(moves):
            ...
            if score < best_score:
                best_score = score
                best_move = m

            if score < beta:
                beta = score

            if beta <= alpha:
                break  # Alpha-Cutoff
```

**MIN-Spieler:**
- Will niedrigsten Wert
- Aktualisiert `beta` nach unten
- Cutoff wenn `beta <= alpha` (MAX würde diesen Ast nie wählen)

```cython
    # TT-Eintrag speichern
    cdef int flag
    if best_score <= alpha_orig:
        flag = TT_UPPER  # Wir haben keinen besseren Zug gefunden
    elif best_score >= beta:
        flag = TT_LOWER  # Es gibt einen Cutoff
    else:
        flag = TT_EXACT  # Exakter Wert

    entry.hash_key = state_hash
    entry.score = best_score
    entry.depth = depth
    entry.flag = flag
    # ... Zug speichern
```

## Iterative Deepening

```cython
cpdef object iterative_deepening(object game_state, int our_team, double time_limit):
    cdef int depth = 1

    while depth <= 30:
        try:
            score, returned_move = alpha_beta(
                state, board, state_hash, depth,
                -INF, INF, maximizing
            )

            if returned_move is not None:
                best_move = returned_move
                best_score = score

            # Abbruch wenn gewonnen/verloren
            if abs(score) >= WIN_SCORE - 100:
                break

            depth += 1

            # Zeit-Management
            if elapsed > time_limit * 0.6:
                break

        except:  # Timeout
            break
```

**Iterative Deepening:**
1. Suche erst mit Tiefe 1
2. Dann Tiefe 2, 3, 4, ...
3. Stoppe bei Zeitüberschreitung
4. Nutze bestes Ergebnis der letzten vollständigen Suche

**Vorteile:**
- Immer einen gültigen Zug verfügbar
- TT-Einträge von vorherigen Iterationen verbessern Sortierung
- Gute Zeitkontrolle

---

# 5. zobrist.pyx - Zobrist-Hashing

## Konzept

Zobrist-Hashing erstellt eindeutige Hash-Werte für Spielpositionen durch XOR-Verknüpfung von Zufallszahlen.

```cython
cdef uint64[10][10][4][5] ZOBRIST_PIECE  # [x][y][team][value]
cdef uint64[61] ZOBRIST_TURN             # [turn]
```

## Initialisierung

```cython
cpdef void init_zobrist():
    global _initialized
    if _initialized:
        return

    srand(42)  # Fester Seed für Reproduzierbarkeit

    for x in range(10):
        for y in range(10):
            for t in range(4):    # Teams 0-3
                for v in range(5):  # Werte 0-4
                    ZOBRIST_PIECE[x][y][t][v] = _rand64()

    for i in range(61):
        ZOBRIST_TURN[i] = _rand64()
```

## 64-Bit Zufallszahl generieren

```cython
cdef inline uint64 _rand64() noexcept nogil:
    return (
        (<uint64>rand() << 48) ^
        (<uint64>rand() << 32) ^
        (<uint64>rand() << 16) ^
        <uint64>rand()
    )
```

**Erklärt:**
- `rand()` gibt nur ~15-16 Bit Zufall
- 4× aufrufen und versetzt kombinieren für 64 Bit
- `<< 48`: Bits 48-63
- `<< 32`: Bits 32-47
- `<< 16`: Bits 16-31
- Ohne Shift: Bits 0-15
- `^`: XOR kombiniert alle

## Hash berechnen

```cython
cpdef uint64 compute_hash(CBoard board):
    cdef uint64 h = ZOBRIST_TURN[board.turn]

    for x in range(10):
        for y in range(10):
            field = board.get_field(x, y)
            team = get_team(field)
            if team != 0:
                value = get_value(field)
                h ^= ZOBRIST_PIECE[x][y][team][value]

    return h
```

**XOR-Eigenschaften:**
- `a ^ a = 0` (selbst-invers)
- `a ^ 0 = a` (neutrales Element)
- Kommutativ: `a ^ b = b ^ a`
- Assoziativ: `(a ^ b) ^ c = a ^ (b ^ c)`

## Inkrementelles Hash-Update

```cython
cpdef uint64 update_hash_move(
    uint64 old_hash,
    int old_turn, int new_turn,
    int start_x, int start_y,
    int target_x, int target_y,
    int team, int value
):
    cdef uint64 h = old_hash

    # Zug-Nummer ändern
    h ^= ZOBRIST_TURN[old_turn]   # Alten Turn entfernen
    h ^= ZOBRIST_TURN[new_turn]   # Neuen Turn hinzufügen

    # Figur bewegen
    h ^= ZOBRIST_PIECE[start_x][start_y][team][value]   # Von Start entfernen
    h ^= ZOBRIST_PIECE[target_x][target_y][team][value] # Zu Ziel hinzufügen

    return h
```

**Warum funktioniert das?**
- XOR ist selbst-invers: `h ^ x ^ x = h`
- Um etwas zu "entfernen", XOR es einfach nochmal
- Um etwas "hinzuzufügen", XOR es einmal
- Viel schneller als kompletten Hash neu zu berechnen

---

# Glossar wichtiger Begriffe

| Begriff | Erklärung |
|---------|-----------|
| `cdef` | C-Definition, nur auf C-Ebene sichtbar |
| `cpdef` | C+Python-Definition, von beiden aufrufbar |
| `ctypedef` | Typ-Alias (wie C's typedef) |
| `cimport` | Import von C-Deklarationen |
| `DEF` | Kompilierzeit-Konstante |
| `noexcept` | Garantiert keine Exception |
| `nogil` | Läuft ohne Global Interpreter Lock |
| `inline` | Funktion wird an Aufrufstelle eingesetzt |
| `bint` | Boolean als int (0/1) |
| `uint64` | Unsigned 64-bit integer |
| `int8` | Signed 8-bit integer |
| `struct` | C-Struktur (Datencontainer) |
| `malloc` | Speicher auf Heap reservieren |
| `free` | Heap-Speicher freigeben |
| `<<` | Bit-Shift links |
| `>>` | Bit-Shift rechts |
| `&` | Bitweises UND / Adress-Operator |
| `\|` | Bitweises ODER |
| `^` | Bitweises XOR |
| `~` | Bitweises NOT |
