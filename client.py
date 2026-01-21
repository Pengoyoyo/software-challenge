import time
from typing import Callable

from socha import GameState, Move, RulesEngine, TeamEnum, Coordinate
from socha.api.networking.game_client import IClientHandler
from socha.starter import Starter


# Zeitlimit pro Zug in Sekunden (mit größerem Puffer für Sicherheit)
TIME_LIMIT = 1.8

# Große Werte für Gewinn/Verlust
INF = 1_000_000
WIN_SCORE = 100_000

# Transposition Table Flags
TT_EXACT = 0  # Exakter Wert
TT_LOWER = 1  # Untere Schranke (Alpha-Cutoff)
TT_UPPER = 2  # Obere Schranke (Beta-Cutoff)


class TimeoutException(Exception):
    """Wird geworfen wenn das Zeitlimit erreicht ist."""

    pass


# ============================================================================
# Evaluierungsfunktionen
# ============================================================================


def groesster_schwarm(
    game_state: GameState, team: TeamEnum
) -> tuple[int, list[Coordinate]]:
    """Berechnet den Wert und die Positionen des größten Schwarms eines Teams."""
    max_value = 0
    groesster = []

    for schwarm in RulesEngine.swarms_of_team(game_state.board, team):
        value = sum(game_state.board.get_field(pos).get_value() for pos in schwarm)
        if value > max_value:
            max_value = value
            groesster = schwarm

    return max_value, groesster


def anzahl_schwaerme(game_state: GameState, team: TeamEnum) -> int:
    """Zählt die Anzahl der Schwärme eines Teams."""
    return len(RulesEngine.swarms_of_team(game_state.board, team))


def material(game_state: GameState, team: TeamEnum) -> int:
    """Berechnet den Materialwert eines Teams."""
    score = 0
    for row in game_state.board.map:
        for ft in row:
            t = ft.get_team()
            if t == team:
                score += ft.get_value()
    return score


def einzelfische(game_state: GameState, team: TeamEnum) -> int:
    """Berechnet den Wert aller isolierten Fische (1er-Schwärme)."""
    value = 0
    for schwarm in RulesEngine.swarms_of_team(game_state.board, team):
        if len(schwarm) == 1:
            value += game_state.board.get_field(schwarm[0]).get_value()
    return value


def mean(lst: list[int]) -> float:
    """Berechnet den Durchschnitt einer Liste."""
    return sum(lst) / len(lst) if lst else 0


def distanz_zum_schwarm(game_state: GameState, team: TeamEnum) -> float:
    """Berechnet die Gesamtdistanz aller Fische zum Zentrum des größten Schwarms."""
    _, schwarm = groesster_schwarm(game_state, team)
    if not schwarm:
        return 0

    ziel_x = mean([pos.x for pos in schwarm])
    ziel_y = mean([pos.y for pos in schwarm])

    score = 0.0
    for x, row in enumerate(game_state.board.map):
        for y, ft in enumerate(row):
            t = ft.get_team()
            if t == team:
                pos = Coordinate(x, y)
                if pos not in schwarm:
                    dx = pos.x - ziel_x
                    dy = pos.y - ziel_y
                    score += (dx * dx + dy * dy) ** 0.5
    return score


def schwarm_kompaktheit(game_state: GameState, team: TeamEnum) -> float:
    """Berechnet wie kompakt der größte Schwarm ist (niedriger = besser)."""
    _, schwarm = groesster_schwarm(game_state, team)
    if len(schwarm) <= 1:
        return 0

    cx = mean([pos.x for pos in schwarm])
    cy = mean([pos.y for pos in schwarm])

    total_dist = 0.0
    for pos in schwarm:
        dx = pos.x - cx
        dy = pos.y - cy
        total_dist += (dx * dx + dy * dy) ** 0.5

    return total_dist / len(schwarm)


def check_winner(game_state: GameState) -> TeamEnum | None:
    """
    Prüft ob ein Team gewonnen hat.
    Ein Team gewinnt wenn:
    - Der Gegner keine Fische mehr hat
    - Der Gegner keinen Schwarm mehr hat (alle gefangen)
    - Das Spiel zu Ende ist (Zug 60) und man mehr Punkte hat
    """
    team_one_swarms = RulesEngine.swarms_of_team(game_state.board, TeamEnum.One)
    team_two_swarms = RulesEngine.swarms_of_team(game_state.board, TeamEnum.Two)

    # Wenn ein Team keine Schwärme mehr hat
    if not team_one_swarms:
        return TeamEnum.Two
    if not team_two_swarms:
        return TeamEnum.One

    # Bei Spielende (Zug 60)
    if game_state.turn >= 60:
        score_one = groesster_schwarm(game_state, TeamEnum.One)[0]
        score_two = groesster_schwarm(game_state, TeamEnum.Two)[0]
        if score_one > score_two:
            return TeamEnum.One
        elif score_two > score_one:
            return TeamEnum.Two
        # Unentschieden - kein Gewinner

    return None


def evaluate(game_state: GameState, our_team: TeamEnum, opp_team: TeamEnum) -> float:
    """
    Erweiterte Evaluierungsfunktion für Spielzustände.
    Positive Werte = gut für our_team, negative = schlecht.
    """
    # Prüfe auf Gewinn/Verlust
    winner = check_winner(game_state)
    if winner == our_team:
        return WIN_SCORE
    elif winner == opp_team:
        return -WIN_SCORE

    value = 0.0

    # Größter Schwarm (sehr wichtig - das ist das Hauptziel!)
    our_schwarm_value = groesster_schwarm(game_state, our_team)[0]
    opp_schwarm_value = groesster_schwarm(game_state, opp_team)[0]
    value += (our_schwarm_value - opp_schwarm_value) * 17.74

    # Anzahl Schwärme (weniger ist besser - ein großer Schwarm ist das Ziel)
    our_num_swarms = anzahl_schwaerme(game_state, our_team)
    opp_num_swarms = anzahl_schwaerme(game_state, opp_team)
    value -= (our_num_swarms - 1) * 3  # Strafe für mehr als 1 Schwarm
    value += (opp_num_swarms - 1) * 3

    # Materialvorteil
    our_material = material(game_state, our_team)
    opp_material = material(game_state, opp_team)
    value += (our_material - opp_material) * 2

    # Einzelfische sind schlecht (leicht zu fangen, nicht im Schwarm)
    our_einzelfische = einzelfische(game_state, our_team)
    opp_einzelfische = einzelfische(game_state, opp_team)
    value -= our_einzelfische * 4
    value += opp_einzelfische * 4

    # Distanz zum Schwarm (Fische sollten zusammenbleiben)
    our_dist = distanz_zum_schwarm(game_state, our_team)
    opp_dist = distanz_zum_schwarm(game_state, opp_team)
    value -= our_dist * 0.63
    value += opp_dist * 0.63

    # Kompaktheit des Schwarms
    our_kompakt = schwarm_kompaktheit(game_state, our_team)
    opp_kompakt = schwarm_kompaktheit(game_state, opp_team)
    value -= our_kompakt * 0
    value += opp_kompakt * 0

    return value


# ============================================================================
# Move Ordering (für besseres Pruning)
# ============================================================================


def order_moves(
    game_state: GameState, moves: list[Move], maximizing: bool
) -> list[Move]:
    """
    Sortiert Züge nach ihrer voraussichtlichen Güte.
    Schnelle Heuristik ohne perform_move() - nur Zielfeld-Analyse.
    """
    current_team = RulesEngine.get_team_on_turn(game_state.turn)
    opp_team = current_team.opponent()
    board = game_state.board

    # Berechne Schwarm-Positionen einmal
    our_swarm_positions: set[Coordinate] = set()
    for schwarm in RulesEngine.swarms_of_team(board, current_team):
        our_swarm_positions.update(schwarm)

    opp_positions: set[Coordinate] = set()
    for schwarm in RulesEngine.swarms_of_team(board, opp_team):
        opp_positions.update(schwarm)

    def move_score(move: Move) -> float:
        """Schnelle heuristische Bewertung ohne perform_move()."""
        score = 0.0

        # Berechne Zielposition
        target = move.start
        for _ in range(4):  # Max 4 Schritte in eine Richtung
            next_pos = target.move(move.direction)
            if not (0 <= next_pos.x < 10 and 0 <= next_pos.y < 10):
                break
            field = board.get_field(next_pos)
            if field.get_team() is not None:
                break
            target = next_pos

        # Fängt der Zug einen Gegner? (Ziel neben Gegner)
        for dx in [-1, 0, 1]:
            for dy in [-1, 0, 1]:
                if dx == 0 and dy == 0:
                    continue
                neighbor = Coordinate(target.x + dx, target.y + dy)
                if neighbor in opp_positions:
                    score += 10  # Potenziell Fang

        # Bewegt sich Richtung eigenem Schwarm?
        if target in our_swarm_positions:
            score += 5  # Verbindet sich mit Schwarm

        # Bewegt sich weg vom Rand? (Zentrum ist besser)
        center_dist = abs(target.x - 4.5) + abs(target.y - 4.5)
        score -= center_dist * 0.5

        return score

    # Sortiere absteigend nach Score (beste Züge zuerst)
    return sorted(moves, key=move_score, reverse=True)


# ============================================================================
# Alpha-Beta Pruning mit iterativer Vertiefung
# ============================================================================


class AlphaBetaSearch:
    """Alpha-Beta Pruning Suchklasse mit iterativer Vertiefung."""

    def __init__(self, our_team: TeamEnum):
        self.our_team = our_team
        self.opp_team = our_team.opponent()
        self.start_time = 0.0
        self.time_limit = TIME_LIMIT
        self.nodes_searched = 0
        self.tt_hits = 0
        # Transposition Table: hash -> (score, depth, flag, best_move)
        self.transposition_table: dict[int, tuple[float, int, int, Move | None]] = {}

    def is_timeout(self) -> bool:
        """Prüft ob das Zeitlimit erreicht ist."""
        return time.time() - self.start_time >= self.time_limit

    def check_timeout(self) -> None:
        """Wirft Exception wenn Timeout erreicht."""
        if self.is_timeout():
            raise TimeoutException()

    def get_state_hash(self, game_state: GameState) -> int:
        """Berechnet einen Hash für den Spielzustand."""
        # Schneller Hash: Nur Positionen und Werte als String
        parts = []
        for x, row in enumerate(game_state.board.map):
            for y, ft in enumerate(row):
                team = ft.get_team()
                if team is not None:
                    # Format: "x,y,team,value"
                    t = 1 if team == TeamEnum.One else 2
                    parts.append(f"{x}{y}{t}{ft.get_value()}")
        return hash(("".join(parts), game_state.turn))

    def alpha_beta(
        self,
        game_state: GameState,
        depth: int,
        alpha: float,
        beta: float,
        maximizing: bool,
    ) -> tuple[float, Move | None]:
        """
        Alpha-Beta Pruning Algorithmus mit Transposition Table.

        Args:
            game_state: Aktueller Spielzustand
            depth: Verbleibende Suchtiefe
            alpha: Beste garantierte Bewertung für Maximierer
            beta: Beste garantierte Bewertung für Minimierer
            maximizing: True wenn wir maximieren, False wenn minimieren

        Returns:
            (Bewertung, bester Zug)
        """
        self.check_timeout()
        self.nodes_searched += 1

        alpha_orig = alpha
        state_hash = self.get_state_hash(game_state)

        # Transposition Table Lookup
        if state_hash in self.transposition_table:
            tt_score, tt_depth, tt_flag, tt_move = self.transposition_table[state_hash]
            if tt_depth >= depth:
                self.tt_hits += 1
                if tt_flag == TT_EXACT:
                    return tt_score, tt_move
                elif tt_flag == TT_LOWER:
                    alpha = max(alpha, tt_score)
                elif tt_flag == TT_UPPER:
                    beta = min(beta, tt_score)

                if alpha >= beta:
                    return tt_score, tt_move

        # Spielende erreicht?
        winner = check_winner(game_state)
        if winner == self.our_team:
            return WIN_SCORE - (60 - depth), None  # Früher gewinnen ist besser
        elif winner == self.opp_team:
            return -WIN_SCORE + (60 - depth), None  # Später verlieren ist besser

        # Blattknoten - evaluiere
        if depth == 0:
            return evaluate(game_state, self.our_team, self.opp_team), None

        moves = game_state.possible_moves()
        if not moves:
            # Keine Züge möglich - Bewertung zurückgeben
            return evaluate(game_state, self.our_team, self.opp_team), None

        # Move Ordering: TT-Move zuerst, dann normale Sortierung
        tt_best_move = None
        if state_hash in self.transposition_table:
            tt_best_move = self.transposition_table[state_hash][3]

        if tt_best_move is not None and tt_best_move in moves:
            # TT-Move an den Anfang
            moves = [tt_best_move] + [m for m in moves if m != tt_best_move]
        elif depth >= 2:
            # Normale Move Ordering nur bei höheren Tiefen
            moves = order_moves(game_state, moves, maximizing)

        best_move = moves[0]

        if maximizing:
            max_eval = -INF
            for move in moves:
                self.check_timeout()
                new_state = game_state.perform_move(move)
                eval_score, _ = self.alpha_beta(
                    new_state, depth - 1, alpha, beta, False
                )

                if eval_score > max_eval:
                    max_eval = eval_score
                    best_move = move

                alpha = max(alpha, eval_score)
                if beta <= alpha:
                    break  # Beta-Cutoff

            # Transposition Table Store
            if max_eval <= alpha_orig:
                tt_flag = TT_UPPER
            elif max_eval >= beta:
                tt_flag = TT_LOWER
            else:
                tt_flag = TT_EXACT
            self.transposition_table[state_hash] = (max_eval, depth, tt_flag, best_move)

            return max_eval, best_move
        else:
            min_eval = INF
            for move in moves:
                self.check_timeout()
                new_state = game_state.perform_move(move)
                eval_score, _ = self.alpha_beta(new_state, depth - 1, alpha, beta, True)

                if eval_score < min_eval:
                    min_eval = eval_score
                    best_move = move

                beta = min(beta, eval_score)
                if beta <= alpha:
                    break  # Alpha-Cutoff

            # Transposition Table Store
            if min_eval <= alpha_orig:
                tt_flag = TT_UPPER
            elif min_eval >= beta:
                tt_flag = TT_LOWER
            else:
                tt_flag = TT_EXACT
            self.transposition_table[state_hash] = (min_eval, depth, tt_flag, best_move)

            return min_eval, best_move

    def iterative_deepening(self, game_state: GameState) -> Move:
        """
        Iterative Vertiefung - sucht mit steigender Tiefe bis Timeout.
        Gibt den besten gefundenen Zug zurück.
        """
        self.start_time = time.time()
        self.nodes_searched = 0

        moves = game_state.possible_moves()
        if len(moves) == 1:
            return moves[0]  # Nur ein Zug möglich

        best_move = moves[0]
        best_score = -INF

        # Bestimme ob wir maximieren (wir sind am Zug)
        current_team = RulesEngine.get_team_on_turn(game_state.turn)
        maximizing = current_team == self.our_team

        depth = 1
        max_depth = 20  # Sicherheitslimit

        while depth <= max_depth and not self.is_timeout():
            try:
                score, move = self.alpha_beta(game_state, depth, -INF, INF, maximizing)

                if move is not None:
                    best_move = move
                    best_score = score

                elapsed = time.time() - self.start_time
                print(
                    f"Tiefe {depth}: Score={score:.1f}, Knoten={self.nodes_searched}, "
                    f"TT-Hits={self.tt_hits}, TT-Size={len(self.transposition_table)}, Zeit={elapsed:.2f}s"
                )

                # Wenn wir einen Gewinnzug gefunden haben, abbrechen
                if abs(score) >= WIN_SCORE - 100:
                    print(f"Gewinnzug gefunden bei Tiefe {depth}!")
                    break

                depth += 1

            except TimeoutException:
                elapsed = time.time() - self.start_time
                print(
                    f"Timeout bei Tiefe {depth} nach {elapsed:.2f}s, {self.nodes_searched} Knoten"
                )
                break

        return best_move


# ============================================================================
# Client Handler
# ============================================================================


class AlphaBetaLogic(IClientHandler):
    """Client Handler mit Alpha-Beta Pruning."""

    def __init__(self) -> None:
        self.game_state: GameState | None = None
        self.our_team: TeamEnum | None = None
        self.searcher: AlphaBetaSearch | None = None

    def on_update(self, game_state: GameState) -> None:
        """Wird aufgerufen wenn ein neuer Spielzustand empfangen wird."""
        self.game_state = game_state

        # Bestimme unser Team beim ersten Update
        if self.our_team is None:
            # Wir sind das Team das als nächstes dran ist
            self.our_team = RulesEngine.get_team_on_turn(game_state.turn)
            self.searcher = AlphaBetaSearch(self.our_team)
            print(f"Spiele als Team: {self.our_team}")

    def calculate_move(self) -> Move:
        """Berechnet den besten Zug mittels Alpha-Beta Pruning."""
        assert self.game_state is not None
        assert self.searcher is not None

        print(f"\n=== Zug {self.game_state.turn + 1} ===")

        best_move = self.searcher.iterative_deepening(self.game_state)

        print(f"Gewählter Zug: {best_move.start} -> {best_move.direction}")

        return best_move

    def on_game_over(self, result) -> None:
        """Wird aufgerufen wenn das Spiel endet."""
        print(f"\n=== Spielende ===")
        print(f"Ergebnis: {result}")


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    print("Starte Alpha-Beta Pruning Bot...")
    Starter(AlphaBetaLogic())
