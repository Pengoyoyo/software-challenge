# Fragenkatalog – Cython-Bot (Software Challenge 2026)

Fragen zum Verständnis des Codes. Ziel: Prüfen, ob der Teilnehmer seinen eigenen Bot wirklich versteht.

---

## 1. Board-Repräsentation

**Q:** Das Spielfeld wird als `int8[100]` gespeichert. Wie werden Team und Figurenwert in einem einzigen Byte codiert?

> Erwartete Antwort: Die unteren 2 Bits codieren das Team (0=leer, 1=Team1, 2=Team2, 3=Tintenfisch), die Bits 2–4 codieren den Wert (Fischzahl). Codierung über `make_field`: `(team & 0x3) | ((value & 0x7) << 2)`.

---

**Q:** Warum wird das Board als 1D-Array mit Index `x * 10 + y` gespeichert statt als 2D-Array?

> Erwartete Antwort: Cache-Lokalität und einfachere Pointer-Arithmetik in C. Ein 1D-Array ermöglicht `memcpy` für Kopiervorgänge.

---

## 2. Zugberechnung / Spielregeln

**Q:** Wie berechnet `c_get_target` das Zielfeld eines Zugs? Warum werden Tintenfische beim Zählen nicht übersprungen?

> Erwartete Antwort: Es werden alle Fische (beider Teams) auf der Linie in beiden Richtungen gezählt, inklusive dem ziehenden Fisch selbst. Tintenfische zählen nicht mit, da sie `TEAM_SQUID` haben und nicht Team ONE/TWO. Der Zählwert ergibt die Sprungweite. Laut Spielregeln sind Tintenfische für die Sprungweite transparent.

---

**Q:** Warum ist ein Zug ungültig, wenn zwischen Start und Ziel ein gegnerischer Fisch steht?

> Erwartete Antwort: Fische springen über alle gleichteamigen Felder, dürfen aber nicht durch gegnerische Felder hindurchspringen – nur auf sie landen (Schlagen). Diese Logik steckt in `c_is_move_valid`: Die Felder entlang des Pfads werden geprüft, und ein `opp_team`-Feld vor dem Ziel macht den Zug illegal.

---

## 3. Evaluation

**Q:** Was bedeutet `best_value` in `SwarmData`? Wann ergibt die Bedingung `our_data.best_value == our_material` einen Sieg?

> Erwartete Antwort: `best_value` ist die Summe der Fischzahlen aller Fische im größten zusammenhängenden Schwarm. Wenn dieser Wert gleich `our_material` (Gesamtwert aller eigenen Fische) ist, sind alle Fische in einem einzigen Schwarm – das ist die Siegbedingung des Spiels.

---

**Q:** Erkläre `eg_phase` und wie er die Bewertungsgewichte beeinflusst.

> Erwartete Antwort: `eg_phase` ist ein Wert zwischen 0 (Frühspiel) und 1 (Endspiel), getrieben durch die Anzahl noch lebender Figuren und die aktuelle Zugnummer. Je höher `eg_phase`, desto stärker werden Endspielgewichte (`W_LATE_*`) auf die Basisgewichte addiert. Das implementiert eine "tapered evaluation" – Konnektivität wird mit Fortschreiten des Spiels wichtiger.

---

**Q:** Warum wird `W_ISOLATED` eingeführt? Reicht `W_MATERIAL` nicht aus?

> Erwartete Antwort: `W_MATERIAL` bestraft den Verlust einer Figur global. `W_ISOLATED` bestraft zusätzlich Figuren, die außerhalb des Hauptschwarms stehen. Eine Figur außerhalb des Schwarms hat denselben Materialwert, ist aber strategisch wertlos, weil sie nicht zur Siegbedingung beiträgt. Die Constraint `W_MATERIAL > W_ISOLATED` stellt sicher, dass der Verlust einer Figur immer schlimmer ist als das Isoliert-Sein.

---

**Q:** Was ist der `CONNECT_BONUS` und unter welchen Bedingungen wird er vergeben?

> Erwartete Antwort: Ein großer Bonus (3768.0), wenn ein Team mit genau 2 Schwärmen und ≤8 Figuren mit einem einzigen Zug alle Figuren verbinden könnte. Der Bonus wird durch `c_has_one_move_connect` geprüft, das alle Züge durchspielt und `c_is_connected` aufruft.

---

## 4. Suche / Negamax

**Q:** Warum wird das Ergebnis von `negamax` negiert, wenn der Zug des Gegners berechnet wird?

> Erwartete Antwort: Negamax nutzt die Symmetrie von Nullsummenspielen: Der Score für den Gegner ist das Negativ des eigenen Scores. Statt zwei getrennte Bewertungsfunktionen zu schreiben, dreht man das Vorzeichen und ruft dieselbe Funktion rekursiv auf.

---

**Q:** Was ist die Transpositionstabelle (TT)? Warum wird ein 4-Slot-Cluster verwendet statt eines einzelnen Eintrags pro Hash?

> Erwartete Antwort: Die TT speichert bereits berechnete Stellungen, um doppelte Arbeit zu vermeiden. Das Cluster mit 4 Slots reduziert Kollisionsverluste: Mehrere Positionen mit demselben Bucket-Index können parallel gespeichert werden. Bei einem Eintrag würde eine neue Stellung eine alte direkt überschreiben.

---

**Q:** Wie funktioniert Null-Move-Pruning? Warum ist es bei `depth < 4` oder in der Endphase deaktiviert?

> Erwartete Antwort: Beim Null-Move wird dem Gegner ein "freier Zug" gegeben, indem `board.turn` erhöht wird ohne Figur zu bewegen. Wenn der Gegner trotzdem keine Beta-Überschreitung erreicht, kann der Zug sicher gepruned werden. Bei `depth < 4` oder zu wenig Figuren ist der Suchraum zu klein oder taktische Gewinnzüge können übersehen werden (Zugzwang-Problem).

---

**Q:** Was sind Killer-Moves und warum werden zwei pro Tiefe gespeichert?

> Erwartete Antwort: Killer-Moves sind Züge, die auf derselben Tiefe zuletzt einen Beta-Cutoff erzeugt haben. Sie werden bevorzugt probiert, weil ruhige Züge, die in einem Ast gut waren, oft auch in einem anderen gut sind. Zwei Killer pro Tiefe erhöhen die Trefferquote ohne zu viel Overhead.

---

**Q:** Erkläre Late Move Reduction (LMR). Welche Züge sind davon ausgenommen?

> Erwartete Antwort: LMR reduziert die Suchtiefe für späte Züge in der Zugliste (nach den ersten 3), da diese statistisch selten die beste Wahl sind. Die Reduktion beträgt 1 bis 3 Ebenen, abhängig von Tiefe und Zugindex. Ausgenommen sind: PV-Züge, Schlagzüge (captures), Züge in flachen Tiefen (`depth < 3`). Wenn ein LMR-Zug `> alpha` liefert, wird er mit voller Tiefe wiederholt.

---

**Q:** Was ist Principal Variation Search (PVS)?

> Erwartete Antwort: Nach dem ersten Zug (der mutmaßlich beste) wird für alle folgenden Züge zunächst ein Null-Fenster-Suche `[-alpha-1, -alpha]` gemacht. Liefert diese einen besseren Score als `alpha`, deutet das darauf hin, dass der Zug wirklich besser ist, und es folgt eine volle Re-Suche. Das spart Rechenzeit, weil die meisten Züge durch das Null-Fenster schnell widerlegt werden.

---

**Q:** Warum wird `c_is_connected(board, opp_team)` am Anfang von `negamax` geprüft, bevor Züge generiert werden?

> Erwartete Antwort: Wenn der Gegner bereits vollständig verbunden ist, hat man bereits verloren – man muss keine weiteren Züge suchen. Diese frühe Erkennung spart die komplette Zuggeneration und -suche für hoffnungslose Stellungen.

---

## 5. Quiescence Search

**Q:** Was ist Quiescence Search und warum ist es hier auf Schlagzüge beschränkt?

> Erwartete Antwort: Quiescence Search verlängert die Suche an "lauten" Positionen (mit Schlagzügen), um den Horizont-Effekt zu vermeiden. Ohne sie würde ein Schlagzug als letzter Zug bewertet, auch wenn der Gegner sofort zurückschlagen kann. Auf ruhige Züge wird verzichtet, weil diese den Suchbaum zu stark aufblasen würden.

---

**Q:** Was ist `stand_pat` in der Quiescence-Suche?

> Erwartete Antwort: `stand_pat` ist der statische Evaluierungswert der aktuellen Position ohne weitere Züge. Wenn er bereits `>= beta` ist, wird sofort zurückgegeben (Beta-Cutoff). Er dient als untere Schranke: Wenn keine Schlagzüge die Position verbessern, ist der aktuelle Wert das Ergebnis.

---

## 6. Zobrist-Hashing

**Q:** Warum verwendet Zobrist-Hashing XOR für inkrementelle Updates?

> Erwartete Antwort: XOR ist selbstinvers: `h XOR x XOR x = h`. Das erlaubt, eine Figur zu "entfernen" (XOR mit ihrem Wert) und zu "platzieren" (XOR mit dem neuen Wert) ohne die ganze Stellung neu zu hashen. Außerdem ist XOR deterministisch und reihenfolgeunabhängig.

---

**Q:** Wie wird der Hash inkrementell bei einem Zug aktualisiert?

> Erwartete Antwort: Man XOR-t heraus: den Turn-Wert der alten Zugnummer, die Figur am Startfeld. Man XOR-t herein: den Turn-Wert der neuen Zugnummer, ggf. die geschlagene Figur (heraus), die Figur am Zielfeld (hinein).

---

## 7. Aspiration Windows

**Q:** Was passiert, wenn der Score außerhalb des Aspiration-Fensters fällt?

> Erwartete Antwort: Bei einem "Fail Low" (Score ≤ Untergrenze) oder "Fail High" (Score ≥ Obergrenze) wird das Fenster um den Faktor 4 vergrößert und die Suche wiederholt. Das verhindert, dass ein schlechtes Fenster einen guten Zug verpasst.

---

## 8. Zeitmanagement

**Q:** Wie verhindert der Bot, dass er seine Zeitgrenze überschreitet?

> Erwartete Antwort: `check_timeout()` wird alle 1024 Knoten aufgerufen und setzt `g_timeout_flag`. Ist das Flag gesetzt, bricht die Suche sofort ab und der beste bisher gefundene Zug wird zurückgegeben. `TIME_USAGE_FRACTION = 0.88` sorgt dafür, dass die Suche schon bei 88% der Zeit stoppt, um Überziehen durch Latenz zu vermeiden.

---

## 9. History-Heuristik

**Q:** Wie funktioniert die History-Heuristik und warum wird sie zwischen den Tiefen "gealtert"?

> Erwartete Antwort: `g_history[key]` speichert für jeden Zug (kodiert als `(start_x*10+start_y)*8+direction`), wie oft er einen Beta-Cutoff erzeugt hat, gewichtet mit `depth²`. Höhere History-Scores bedeuten bessere Zugsortierung. Das Altern (`>>= 1` nach jeder Tiefe, `* 7/8` beim Reset) verhindert, dass alte Informationen aus früheren Iterationen die aktuelle zu stark beeinflussen.

---

## 10. Counter-Move-Tabelle

**Q:** Was ist eine Counter-Move-Tabelle und wie wird sie indiziert?

> Erwartete Antwort: Für jeden Zug des Gegners (indiziert über `move_key`) wird der Zug gespeichert, der diesen Gegenzug am häufigsten widerlegt hat. Beim Sortieren wird dieser "Counter Move" bevorzugt probiert. Die Tabelle wird pro Iteration zurückgesetzt, da Counter Moves kontextabhängig sind.

---

## 11. Setup / Cython

**Q:** Was macht die Direktive `# cython: boundscheck=False, wraparound=False` am Dateianfang?

> Erwartete Antwort: Sie deaktiviert die Laufzeit-Prüfungen von Cython: `boundscheck=False` verhindert, dass bei jedem Array-Zugriff die Grenzen geprüft werden. `wraparound=False` deaktiviert negative Indizes. Beide Optimierungen erhöhen die Performance deutlich, erfordern aber korrekte manuelle Indexberechnung.

---

**Q:** Was ist der Unterschied zwischen `cdef`, `cpdef` und `def` in Cython?

> Erwartete Antwort: `def` = reines Python, aufrufbar von Python, langsam. `cdef` = reines C, nicht von Python aufrufbar, schnellstmöglich. `cpdef` = Hybrid, von Python und C aufrufbar, leichter Overhead durch Python-Dispatch.

---

**Q:** Warum werden `noexcept nogil` Funktionen verwendet?

> Erwartete Antwort: `noexcept` signalisiert, dass die Funktion keine Python-Exception wirft – Cython muss keinen Exception-Check nach dem Aufruf einfügen. `nogil` gibt den Global Interpreter Lock frei, was für Multi-Threading nötig wäre, aber auch für reine C-Funktionen sinnvoll ist.

---
