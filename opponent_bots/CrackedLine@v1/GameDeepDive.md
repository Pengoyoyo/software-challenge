# Piranhas: Deep Dive fuer neue Bots

Dieses Dokument erklaert das Spiel in Tiefe und uebersetzt jede wichtige Regel direkt in Bot-Anforderungen. Es ist als Referenz fuer einen neuen Bot gedacht (z. B. in Rust).

## 1) Kernziel des Spiels

Piranhas ist ein Verbindungsspiel mit wertigen Steinen.

- Primaerziel: Einen zusammenhaengenden Schwarm bilden, bevor der Gegner es tut.
- Falls keine fruehe Entscheidung faellt, entscheidet spaeter die Qualitaet der besten verbundenen Gruppe.

Praktische Konsequenz fuer den Bot:

- Suche darf nicht nur auf Material oder Mobilitaet optimieren.
- Verbindungspotenzial (jetzt + in 1-2 Zuegen) ist zentral.

## 2) Spielfeld und Objekte

- Brettgroesse: `10x10`.
- Zwei Spieler (typisch Rot/Blau).
- Zwei Krakenfelder (blockierende Sonderfelder fuer Zielfeld, aber beim Ueberspringen speziell behandelt).
- Fische haben Werte (`1`, `2`, `3`) und gehoeren einem Spieler.

Bot-Relevanz:

- Interne Repraesentation muss Fischwert und Besitzer getrennt/effizient halten.
- Endspielwertung ist wertbasiert, daher Werte niemals wegabstrahieren.

## 3) Was ist ein Schwarm?

Ein Schwarm ist eine zusammenhaengende Komponente eigener Fische unter `8er-Nachbarschaft` (horizontal, vertikal, diagonal).

Das heisst:

- Diagonale Kontakte zaehlen als verbunden.
- Ein Spieler ist voll verbunden, wenn alle seine Fische in einer einzigen Komponente liegen.

Bot-Relevanz:

- Komponentenanalyse ist Hotpath in Eval und Terminalchecks.
- Du brauchst schnelle Funktionen fuer:
  - `component_count(player)`
  - `largest_component_value(player)`
  - `is_connected(player)`

## 4) Zugregel: Distanz auf Linien

Die Distanz eines Zuges basiert auf der Linie des Startfeldes in gewaehlter Richtung.

- Distanz = Anzahl **aller Fische** auf dieser Linie.
- Linien sind: Reihe, Spalte, beide Diagonalen.
- Es wird nicht lokal gezaehlt, sondern auf der ganzen Linie.

Bot-Relevanz:

- `line_count` muss konstant schnell sein.
- Inkrementelle Zaehler fuer Reihen/Spalten/Diagonalen sind sehr hilfreich.

## 5) Blockieren und Ueberspringen

Wichtige Bewegungslogik:

- Eigene Fische duerfen uebersprungen werden.
- Kraken duerfen uebersprungen werden.
- Gegnerische Fische blockieren den Weg, ausser am finalen Zielfeld (Capture).

Bot-Relevanz:

- Movegen braucht schnelle Legality-Filter:
  - fruehe Rejects fuer unmoegliche Ziele
  - nur dann detaillierte Pfadpruefung
- Fehler hier ruinieren alle spaeteren Elo-Verbesserungen.

## 6) Zielfeld-Regeln

- Zielfeld darf nicht eigener Fisch sein.
- Zielfeld darf nicht Kraken sein.
- Zielfeld darf ein gegnerischer Fisch sein (Capture).

Bot-Relevanz:

- Capture-Generierung und Voll-Movegen muessen denselben Rechtsrahmen teilen.
- QSearch sollte Captures und ausgewaehlte "laute" Connectivity-Zuege betrachten.

## 7) Spielende und Wertung

Typische Endreihenfolge:

1. Sofortige Verbindungsentscheidung (ein Spieler hat voll verbunden).
2. Spaeter/bei Rundengrenze Vergleich groesster verbundener Schwarm (wertbasiert).
3. Tie-Break ueber Gesamtwert.
4. Weitere Tie-Breaks (z. B. Anzahl), falls noetig.

Bot-Relevanz:

- Terminal-Scoring muss exakt der Turnierlogik folgen.
- Eval darf dieses Endkriterium nicht konterkarieren.

## 8) Typische taktische Motive

- One-move-connect: Ein Zug verbindet den eigenen Restschwarm voll.
- One-move-disconnect: Ein Zug trennt zentrale gegnerische Bruecke.
- False mobility: Viele legale Zuege, aber fast alle positionell schlecht.
- Pendel/Shuffle: Gleichwertige Hin-und-Her-Zuege ohne Fortschritt.

Bot-Relevanz:

- Sucherweiterungen fuer kritische Verbindungszuege sind sinnvoll.
- Anti-Shuffle/Repetition-Penalty verbessert praktische Spielstaerke stark.

## 9) Strategische Phasen

### Opening

- Ziel: Struktur aufbauen, nicht blind Material traden.
- Fokus auf Felder, die spaeter mehrere Komponenten verbinden.

Bot-Fokus:

- Book oder starke Root-Ordering-Hints.
- Keine zu aggressiven Prunes, solange Struktur offen ist.

### Midgame

- Hoechste Komplexitaet, groesster Suchnutzen.
- Balance aus Capture-Chancen und Verbindungskosten.

Bot-Fokus:

- Tiefe durch gutes Ordering + TT + LMR.
- Threat-Erkennung (connect/disconnect in 1 ply) priorisieren.

### Endgame

- Wertung dominiert: groesster verbundener Wertschwarm.
- Mobilitaet wird weniger wichtig als genaue Komponentenkontrolle.

Bot-Fokus:

- Hoeheres Gewicht auf Schwarmabschluss.
- Praezise Terminalnaehe-Bewertung, weniger Rauschen.

## 10) Was ein neuer Bot zwingend korrekt modellieren muss

- Wertige Fische (`1/2/3`) im State.
- Exakte Linien-Distanzregel.
- Exakte Blockade-/Ueberspringregeln.
- Kraken-Sonderbehandlung.
- 8er-Konnektivitaet.
- Endwertung inklusive Tie-Break-Reihenfolge.

Wenn einer dieser Punkte fehlt, optimiert der Bot auf das falsche Spiel.

## 11) Baseline fuer starke Suchlogik

Empfohlen als stabile Mindestbasis:

- Iterative Deepening.
- Negamax + Alpha-Beta + PVS.
- Transposition Table (Bound-Typen korrekt).
- Move Ordering (TT, taktisch, killer/history/counter).
- QSearch mit Capture + Connectivity-noisy moves.
- Striktes Zeitmanagement mit hartem Abbruch und "best so far".

Optional spaeter:

- Verification Search.
- Singular Extension.
- Subtree-Reuse ueber Zuege.
- Deterministisches Opening-Book.

## 12) Zeitlimit und Produktionsstabilitaet

Unter Turnierbedingungen ist Stabilitaet wichtiger als eine einzelne tiefe Iteration.

- Hardcap pro Zug strikt einhalten.
- Reserve fuer Rückgabe/IO/Overhead einplanen.
- Neue Iteration nur starten, wenn Restbudget sicher reicht.

Zielmetrik:

- `0` Deadline-Misses ueber sehr viele Suchaufrufe.

## 13) Telemetrie, die du immer loggen solltest

- depth, nodes, qnodes, nps
- tt_hit_rate
- fail-high, fail-low
- best-move-changes pro Iteration
- reply/subtree/book hits
- verification nodes, singular extensions
- elapsed und margin zum Hardcap

Ohne diese Werte ist ernsthaftes Tuning kaum moeglich.

## 14) Test-Gates fuer einen neuen Bot

- Regeltests (vollstaendig).
- make/unmake + hash Reversibility.
- Determinismus bei identischem Seed/State/Config.
- Timecap-Stresstest.
- Endwertungs- und Tie-Break-Tests.
- Fuzzing auf legalen Zufallspositionen.

Nur Features mergen, die alle Gates halten.

## 15) Priorisierte Build-Reihenfolge fuer ein neues Projekt

1. Korrektes State/Movegen/Terminal.
2. Solider Search-Kern ohne aggressive Prunes.
3. Zeitmanagement und Determinismus.
4. Move Ordering + TT-Qualitaet.
5. QSearch + konservative Selektion.
6. Persistente Suchgedaechtnis-Features.
7. Book + datengetriebenes Tuning.

Diese Reihenfolge minimiert Risiko und maximiert fruehe Elo-Gewinne.
