import random
import time

from socha import GameState, Move, RulesEngine, TeamEnum, Coordinate
from socha.api.networking.game_client import IClientHandler
from socha.starter import Starter


# Zeitlimit pro Zug in Sekunden
TIME_LIMIT = 1.75

# Große Werte für Gewinn/Verlust
INF = 1_000_000
WIN_SCORE = 100_000

# Transposition Table Flags
TT_EXACT = 0
TT_LOWER = 1
TT_UPPER = 2

# LMR Konstanten
LMR_DEPTH_LIMIT = 3  # Ab dieser Tiefe LMR anwenden
LMR_MOVE_LIMIT = 4   # Ab diesem Zug-Index LMR anwenden


class TimeoutException(Exception):
    pass


# ============================================================================
# Zobrist Hashing - vorberechnete Zufallszahlen für schnelles Hashing
# ============================================================================

random.seed(42)  # Deterministisch für Reproduzierbarkeit

# Zobrist-Tabelle: [x][y][team][value] -> random 64-bit int
# team: 0=One, 1=Two; value: 1-4
ZOBRIST_TABLE = [
    [
        [
            [random.getrandbits(64) for _ in range(5)]  # value 0-4 (0 unused)
            for _ in range(2)  # team
        ]
        for _ in range(10)  # y
    ]
    for _ in range(10)  # x
]

# Zobrist für Zug-Nummer
ZOBRIST_TURN = [random.getrandbits(64) for _ in range(61)]


def compute_zobrist(game_state: GameState) -> int:
    """Berechnet Zobrist-Hash für einen Spielzustand."""
    h = ZOBRIST_TURN[game_state.turn]
    for x, row in enumerate(game_state.board.map):
        for y, ft in enumerate(row):
            team = ft.get_team()
            if team is not None:
                t = 0 if team == TeamEnum.One else 1
                v = ft.get_value()
                h ^= ZOBRIST_TABLE[x][y][t][v]
    return h


# ============================================================================
# Optimierte Evaluierungsfunktionen
# ============================================================================


def get_swarm_data(game_state: GameState, team: TeamEnum) -> tuple[int, int, set]:
    """
    Berechnet Schwarm-Daten in einem Durchlauf.
    Returns: (größter_schwarm_wert, anzahl_schwärme, positionen_größter_schwarm)
    """
    swarms = RulesEngine.swarms_of_team(game_state.board, team)
    if not swarms:
        return 0, 0, set()

    max_value = 0
    best_swarm = []

    for schwarm in swarms:
        value = sum(game_state.board.get_field(pos).get_value() for pos in schwarm)
        if value > max_value:
            max_value = value
            best_swarm = schwarm

    return max_value, len(swarms), {(p.x, p.y) for p in best_swarm}


def evaluate_fast(game_state: GameState, our_team: TeamEnum, opp_team: TeamEnum) -> float:
    """
    Schnelle Evaluierungsfunktion - berechnet alles in möglichst wenigen Durchläufen.
    """
    # Hole alle Schwarm-Daten in einem Aufruf pro Team
    our_swarm_val, our_num_swarms, our_swarm_pos = get_swarm_data(game_state, our_team)
    opp_swarm_val, opp_num_swarms, opp_swarm_pos = get_swarm_data(game_state, opp_team)

    # Gewinn/Verlust Check
    if our_num_swarms == 0:
        return -WIN_SCORE
    if opp_num_swarms == 0:
        return WIN_SCORE

    if game_state.turn >= 60:
        if our_swarm_val > opp_swarm_val:
            return WIN_SCORE
        elif opp_swarm_val > our_swarm_val:
            return -WIN_SCORE

    value = 0.0

    # Schwarmwert (Hauptziel)
    value += (our_swarm_val - opp_swarm_val) * 18.0

    # Anzahl Schwärme (weniger = besser)
    value -= (our_num_swarms - 1) * 4.0
    value += (opp_num_swarms - 1) * 4.0

    # Materialwert und Position in einem Durchlauf
    our_material = 0
    opp_material = 0
    our_isolated = 0
    opp_isolated = 0
    our_dist_sum = 0.0
    opp_dist_sum = 0.0

    # Schwarm-Zentren berechnen (swarm_pos enthält (x,y) Tupel)
    if our_swarm_pos:
        our_cx = sum(p[0] for p in our_swarm_pos) / len(our_swarm_pos)
        our_cy = sum(p[1] for p in our_swarm_pos) / len(our_swarm_pos)
    else:
        our_cx, our_cy = 4.5, 4.5

    if opp_swarm_pos:
        opp_cx = sum(p[0] for p in opp_swarm_pos) / len(opp_swarm_pos)
        opp_cy = sum(p[1] for p in opp_swarm_pos) / len(opp_swarm_pos)
    else:
        opp_cx, opp_cy = 4.5, 4.5

    for x, row in enumerate(game_state.board.map):
        for y, ft in enumerate(row):
            team = ft.get_team()
            if team is None:
                continue

            val = ft.get_value()

            if team == our_team:
                our_material += val
                if (x, y) not in our_swarm_pos:
                    # Distanz zum Schwarm
                    dx = x - our_cx
                    dy = y - our_cy
                    our_dist_sum += (dx * dx + dy * dy) ** 0.5
                    # Ist isoliert? (1er-Schwarm)
                    # Approximation: nicht im größten Schwarm = potentiell isoliert
                    our_isolated += val
            else:
                opp_material += val
                if (x, y) not in opp_swarm_pos:
                    dx = x - opp_cx
                    dy = y - opp_cy
                    opp_dist_sum += (dx * dx + dy * dy) ** 0.5
                    opp_isolated += val

    # Material
    value += (our_material - opp_material) * 2.0

    # Isolierte Fische (schlecht)
    value -= our_isolated * 3.0
    value += opp_isolated * 3.0

    # Distanz zum Schwarm
    value -= our_dist_sum * 0.7
    value += opp_dist_sum * 0.7

    return value


# ============================================================================
# Move Ordering Heuristiken
# ============================================================================


class MoveOrderer:
    """Verwaltet Killer Moves und History Heuristic für besseres Move Ordering."""

    def __init__(self):
        # Killer Moves: [depth] -> [move1, move2]
        self.killer_moves: list[list[Move | None]] = [[None, None] for _ in range(30)]
        # History Heuristic: (start_x, start_y, direction) -> score
        self.history: dict[tuple, int] = {}

    def update_killer(self, move: Move, depth: int) -> None:
        """Speichert einen Killer Move für diese Tiefe."""
        if depth < len(self.killer_moves):
            killers = self.killer_moves[depth]
            if move != killers[0]:
                killers[1] = killers[0]
                killers[0] = move

    def update_history(self, move: Move, depth: int) -> None:
        """Erhöht den History-Score für einen erfolgreichen Zug."""
        key = (move.start.x, move.start.y, int(move.direction))
        self.history[key] = self.history.get(key, 0) + depth * depth

    def get_history_score(self, move: Move) -> int:
        """Holt den History-Score für einen Zug."""
        key = (move.start.x, move.start.y, int(move.direction))
        return self.history.get(key, 0)

    def is_killer(self, move: Move, depth: int) -> bool:
        """Prüft ob ein Zug ein Killer Move ist."""
        if depth < len(self.killer_moves):
            return move in self.killer_moves[depth]
        return False

    def order_moves(
        self,
        game_state: GameState,
        moves: list[Move],
        depth: int,
        tt_move: Move | None
    ) -> list[Move]:
        """
        Sortiert Züge nach ihrer voraussichtlichen Güte.
        Reihenfolge: TT-Move > Killer Moves > History > Heuristik
        """
        if not moves:
            return moves

        current_team = RulesEngine.get_team_on_turn(game_state.turn)
        opp_team = current_team.opponent()
        board = game_state.board

        # Positionen einmal berechnen (als Tupel für Hashbarkeit)
        our_positions: set[tuple] = set()
        opp_positions: set[tuple] = set()
        for schwarm in RulesEngine.swarms_of_team(board, current_team):
            our_positions.update((p.x, p.y) for p in schwarm)
        for schwarm in RulesEngine.swarms_of_team(board, opp_team):
            opp_positions.update((p.x, p.y) for p in schwarm)

        def move_priority(move: Move) -> float:
            """Berechnet Priorität für Sortierung (höher = besser)."""
            score = 0.0

            # TT-Move hat höchste Priorität
            if tt_move is not None and move == tt_move:
                return 100000.0

            # Killer Moves
            if self.is_killer(move, depth):
                score += 5000.0

            # History Score
            score += self.get_history_score(move) * 0.1

            # Heuristische Bewertung
            target = move.start
            direction_vec = move.direction.to_vector()
            for _ in range(10):
                next_pos = target.add_vector(direction_vec)
                if not (0 <= next_pos.x < 10 and 0 <= next_pos.y < 10):
                    break
                field = board.get_field(next_pos)
                if field.get_team() is not None:
                    break
                target = next_pos

            # Fängt Gegner?
            for dx in [-1, 0, 1]:
                for dy in [-1, 0, 1]:
                    if dx == 0 and dy == 0:
                        continue
                    nx, ny = target.x + dx, target.y + dy
                    if 0 <= nx < 10 and 0 <= ny < 10:
                        if (nx, ny) in opp_positions:
                            score += 50.0

            # Verbindet mit Schwarm?
            if (target.x, target.y) in our_positions:
                score += 30.0

            # Zentrum bevorzugen
            center_dist = abs(target.x - 4.5) + abs(target.y - 4.5)
            score -= center_dist * 2.0

            return score

        return sorted(moves, key=move_priority, reverse=True)


# ============================================================================
# Alpha-Beta Suche mit allen Optimierungen
# ============================================================================


class AlphaBetaSearch:
    """Hochoptimierte Alpha-Beta Suche."""

    def __init__(self, our_team: TeamEnum):
        self.our_team = our_team
        self.opp_team = our_team.opponent()
        self.start_time = 0.0
        self.time_limit = TIME_LIMIT
        self.nodes_searched = 0
        self.tt_hits = 0
        self.tt_cutoffs = 0

        # Transposition Table: hash -> (score, depth, flag, best_move)
        self.tt: dict[int, tuple[float, int, int, Move | None]] = {}

        # Move Ordering
        self.move_orderer = MoveOrderer()

        # Eval Cache
        self.eval_cache: dict[int, float] = {}

    def is_timeout(self) -> bool:
        return time.time() - self.start_time >= self.time_limit

    def check_timeout(self) -> None:
        if self.is_timeout():
            raise TimeoutException()

    def get_cached_eval(self, state_hash: int, game_state: GameState) -> float:
        """Evaluierung mit Caching."""
        if state_hash in self.eval_cache:
            return self.eval_cache[state_hash]
        score = evaluate_fast(game_state, self.our_team, self.opp_team)
        self.eval_cache[state_hash] = score
        return score

    def alpha_beta(
        self,
        game_state: GameState,
        state_hash: int,
        depth: int,
        alpha: float,
        beta: float,
        maximizing: bool,
        is_pv: bool,  # Principal Variation Node
    ) -> tuple[float, Move | None]:
        """Alpha-Beta mit TT, Killer, History, LMR."""
        self.check_timeout()
        self.nodes_searched += 1

        alpha_orig = alpha

        # TT Lookup
        tt_move = None
        if state_hash in self.tt:
            tt_score, tt_depth, tt_flag, tt_move = self.tt[state_hash]
            if tt_depth >= depth:
                self.tt_hits += 1
                if tt_flag == TT_EXACT:
                    return tt_score, tt_move
                elif tt_flag == TT_LOWER:
                    alpha = max(alpha, tt_score)
                elif tt_flag == TT_UPPER:
                    beta = min(beta, tt_score)
                if alpha >= beta:
                    self.tt_cutoffs += 1
                    return tt_score, tt_move

        # Terminal Check
        our_swarms = RulesEngine.swarms_of_team(game_state.board, self.our_team)
        opp_swarms = RulesEngine.swarms_of_team(game_state.board, self.opp_team)

        if not our_swarms:
            return -WIN_SCORE + (60 - depth), None
        if not opp_swarms:
            return WIN_SCORE - (60 - depth), None

        if game_state.turn >= 60:
            our_val = sum(
                sum(game_state.board.get_field(p).get_value() for p in s)
                for s in our_swarms
            )
            opp_val = sum(
                sum(game_state.board.get_field(p).get_value() for p in s)
                for s in opp_swarms
            )
            # Finde größten Schwarm
            our_best = max(
                sum(game_state.board.get_field(p).get_value() for p in s)
                for s in our_swarms
            )
            opp_best = max(
                sum(game_state.board.get_field(p).get_value() for p in s)
                for s in opp_swarms
            )
            if our_best > opp_best:
                return WIN_SCORE - (60 - depth), None
            elif opp_best > our_best:
                return -WIN_SCORE + (60 - depth), None

        # Leaf Node
        if depth == 0:
            return self.get_cached_eval(state_hash, game_state), None

        moves = game_state.possible_moves()
        if not moves:
            return self.get_cached_eval(state_hash, game_state), None

        # Move Ordering
        moves = self.move_orderer.order_moves(game_state, moves, depth, tt_move)

        best_move = moves[0]
        moves_searched = 0

        if maximizing:
            max_eval = -INF
            for move in moves:
                self.check_timeout()
                new_state = game_state.perform_move(move)
                new_hash = compute_zobrist(new_state)

                # Late Move Reduction
                reduction = 0
                if (
                    depth >= LMR_DEPTH_LIMIT
                    and moves_searched >= LMR_MOVE_LIMIT
                    and not is_pv
                ):
                    reduction = 1

                # Search with possible reduction
                if reduction > 0:
                    # Reduced search
                    eval_score, _ = self.alpha_beta(
                        new_state, new_hash, depth - 1 - reduction,
                        alpha, beta, False, False
                    )
                    # Re-search if promising
                    if eval_score > alpha:
                        eval_score, _ = self.alpha_beta(
                            new_state, new_hash, depth - 1,
                            alpha, beta, False, is_pv and moves_searched == 0
                        )
                else:
                    eval_score, _ = self.alpha_beta(
                        new_state, new_hash, depth - 1,
                        alpha, beta, False, is_pv and moves_searched == 0
                    )

                if eval_score > max_eval:
                    max_eval = eval_score
                    best_move = move

                if eval_score > alpha:
                    alpha = eval_score
                    # Update history for good moves
                    self.move_orderer.update_history(move, depth)

                if beta <= alpha:
                    # Update killer moves on cutoff
                    self.move_orderer.update_killer(move, depth)
                    break

                moves_searched += 1

            # TT Store
            if max_eval <= alpha_orig:
                tt_flag = TT_UPPER
            elif max_eval >= beta:
                tt_flag = TT_LOWER
            else:
                tt_flag = TT_EXACT
            self.tt[state_hash] = (max_eval, depth, tt_flag, best_move)

            return max_eval, best_move
        else:
            min_eval = INF
            for move in moves:
                self.check_timeout()
                new_state = game_state.perform_move(move)
                new_hash = compute_zobrist(new_state)

                # Late Move Reduction
                reduction = 0
                if (
                    depth >= LMR_DEPTH_LIMIT
                    and moves_searched >= LMR_MOVE_LIMIT
                    and not is_pv
                ):
                    reduction = 1

                if reduction > 0:
                    eval_score, _ = self.alpha_beta(
                        new_state, new_hash, depth - 1 - reduction,
                        alpha, beta, True, False
                    )
                    if eval_score < beta:
                        eval_score, _ = self.alpha_beta(
                            new_state, new_hash, depth - 1,
                            alpha, beta, True, is_pv and moves_searched == 0
                        )
                else:
                    eval_score, _ = self.alpha_beta(
                        new_state, new_hash, depth - 1,
                        alpha, beta, True, is_pv and moves_searched == 0
                    )

                if eval_score < min_eval:
                    min_eval = eval_score
                    best_move = move

                if eval_score < beta:
                    beta = eval_score
                    self.move_orderer.update_history(move, depth)

                if beta <= alpha:
                    self.move_orderer.update_killer(move, depth)
                    break

                moves_searched += 1

            # TT Store
            if min_eval <= alpha_orig:
                tt_flag = TT_UPPER
            elif min_eval >= beta:
                tt_flag = TT_LOWER
            else:
                tt_flag = TT_EXACT
            self.tt[state_hash] = (min_eval, depth, tt_flag, best_move)

            return min_eval, best_move

    def iterative_deepening(self, game_state: GameState) -> Move:
        """Iterative Vertiefung mit Aspiration Windows."""
        self.start_time = time.time()
        self.nodes_searched = 0
        self.tt_hits = 0
        self.tt_cutoffs = 0

        moves = game_state.possible_moves()
        if len(moves) == 1:
            return moves[0]

        best_move = moves[0]
        best_score = -INF

        current_team = RulesEngine.get_team_on_turn(game_state.turn)
        maximizing = current_team == self.our_team

        state_hash = compute_zobrist(game_state)
        depth = 1
        max_depth = 30

        # Aspiration Window
        asp_window = 50.0

        while depth <= max_depth and not self.is_timeout():
            try:
                # Aspiration Windows: starte mit engem Fenster
                if depth >= 4 and abs(best_score) < WIN_SCORE - 1000:
                    alpha = best_score - asp_window
                    beta = best_score + asp_window
                else:
                    alpha = -INF
                    beta = INF

                score, move = self.alpha_beta(
                    game_state, state_hash, depth, alpha, beta, maximizing, True
                )

                # Aspiration fail - re-search with full window
                if score <= alpha or score >= beta:
                    score, move = self.alpha_beta(
                        game_state, state_hash, depth, -INF, INF, maximizing, True
                    )

                if move is not None:
                    best_move = move
                    best_score = score

                elapsed = time.time() - self.start_time
                nps = int(self.nodes_searched / elapsed) if elapsed > 0 else 0
                print(
                    f"d{depth}: {score:.0f} | {self.nodes_searched}n "
                    f"{self.tt_hits}h {nps}nps {elapsed:.2f}s"
                )

                if abs(score) >= WIN_SCORE - 100:
                    print(f"Gewinnzug bei Tiefe {depth}!")
                    break

                depth += 1

            except TimeoutException:
                elapsed = time.time() - self.start_time
                print(f"Timeout d{depth} nach {elapsed:.2f}s, {self.nodes_searched}n")
                break

        return best_move


# ============================================================================
# Client Handler
# ============================================================================


class AlphaBetaLogic(IClientHandler):
    def __init__(self) -> None:
        self.game_state: GameState | None = None
        self.our_team: TeamEnum | None = None
        self.searcher: AlphaBetaSearch | None = None

    def on_update(self, game_state: GameState) -> None:
        self.game_state = game_state
        if self.our_team is None:
            self.our_team = RulesEngine.get_team_on_turn(game_state.turn)
            self.searcher = AlphaBetaSearch(self.our_team)
            print(f"Team: {self.our_team}")

    def calculate_move(self) -> Move:
        assert self.game_state is not None
        assert self.searcher is not None

        print(f"\n=== Zug {self.game_state.turn + 1} ===")
        best_move = self.searcher.iterative_deepening(self.game_state)
        print(f"-> {best_move.start} {best_move.direction}")

        return best_move

    def on_game_over(self, result) -> None:
        print(f"\n=== Ende ===\n{result}")


if __name__ == "__main__":
    print("Starte optimierten Bot...")
    Starter(AlphaBetaLogic())
