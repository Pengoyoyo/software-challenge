import random
import time
from dataclasses import dataclass

from socha import GameState, Move, RulesEngine, TeamEnum, Coordinate
from socha.api.networking.game_client import IClientHandler
from socha.starter import Starter


TIME_LIMIT = 1.75
INF = 1_000_000
WIN_SCORE = 100_000

TT_EXACT = 0
TT_LOWER = 1
TT_UPPER = 2

LMR_DEPTH_LIMIT = 3
LMR_MOVE_LIMIT = 4


class TimeoutException(Exception):
    pass


# ============================================================================
# Zobrist Hashing - vorberechnete Tabellen
# ============================================================================

random.seed(42)

# [x][y][team][value] -> random 64-bit
ZOBRIST_PIECE = [
    [
        [[random.getrandbits(64) for _ in range(5)] for _ in range(2)]
        for _ in range(10)
    ]
    for _ in range(10)
]
ZOBRIST_TURN = [random.getrandbits(64) for _ in range(61)]


def compute_zobrist_full(game_state: GameState) -> int:
    """Vollständige Zobrist-Berechnung (nur initial)."""
    h = ZOBRIST_TURN[game_state.turn]
    for x, row in enumerate(game_state.board.map):
        for y, ft in enumerate(row):
            team = ft.get_team()
            if team is not None:
                t = 0 if team == TeamEnum.One else 1
                h ^= ZOBRIST_PIECE[x][y][t][ft.get_value()]
    return h


def zobrist_update_move(
    old_hash: int,
    old_turn: int,
    move: Move,
    game_state: GameState,
    new_state: GameState
) -> int:
    """Inkrementelles Zobrist-Update nach einem Zug."""
    h = old_hash

    # Turn update
    h ^= ZOBRIST_TURN[old_turn]
    h ^= ZOBRIST_TURN[new_state.turn]

    # Finde Start-Feld Info
    start = move.start
    old_field = game_state.board.get_field(start)
    old_team = old_field.get_team()
    old_value = old_field.get_value()

    if old_team is not None:
        t = 0 if old_team == TeamEnum.One else 1
        # Entferne Figur von alter Position
        h ^= ZOBRIST_PIECE[start.x][start.y][t][old_value]

    # Finde Ziel-Position (simuliere Bewegung)
    target = start
    board = game_state.board
    direction_vec = move.direction.to_vector()
    for _ in range(10):
        next_pos = target.add_vector(direction_vec)
        if not (0 <= next_pos.x < 10 and 0 <= next_pos.y < 10):
            break
        field = board.get_field(next_pos)
        if field.get_team() is not None:
            break
        target = next_pos

    # Hole neue Figur-Info vom neuen State
    new_field = new_state.board.get_field(target)
    new_team = new_field.get_team()
    new_value = new_field.get_value()

    if new_team is not None:
        t = 0 if new_team == TeamEnum.One else 1
        # Füge Figur an neuer Position hinzu
        h ^= ZOBRIST_PIECE[target.x][target.y][t][new_value]

    # Prüfe ob Gegner gefangen wurde (Wert könnte sich geändert haben)
    # Das passiert wenn die Figur einen Gegner "frisst"
    if old_value != new_value and old_team is not None:
        # Der Wert hat sich geändert - wir müssen auch die gefangenen Figuren updaten
        # Das ist komplizierter, also fallback auf full recompute wenn Wert sich ändert
        return compute_zobrist_full(new_state)

    return h


# ============================================================================
# Schwarm-Cache Struktur
# ============================================================================

@dataclass
class NodeContext:
    """Cached Daten für einen Suchknoten."""
    our_swarms: list
    opp_swarms: list
    our_swarm_val: int
    opp_swarm_val: int
    our_best_swarm: set
    opp_best_swarm: set
    our_num_swarms: int
    opp_num_swarms: int


def build_node_context(
    game_state: GameState,
    our_team: TeamEnum,
    opp_team: TeamEnum
) -> NodeContext:
    """Berechnet alle Schwarm-Daten einmalig für einen Knoten."""
    board = game_state.board

    our_swarms = RulesEngine.swarms_of_team(board, our_team)
    opp_swarms = RulesEngine.swarms_of_team(board, opp_team)

    # Bester Schwarm für uns
    our_best_val = 0
    our_best = []
    for s in our_swarms:
        val = sum(board.get_field(p).get_value() for p in s)
        if val > our_best_val:
            our_best_val = val
            our_best = s

    # Bester Schwarm für Gegner
    opp_best_val = 0
    opp_best = []
    for s in opp_swarms:
        val = sum(board.get_field(p).get_value() for p in s)
        if val > opp_best_val:
            opp_best_val = val
            opp_best = s

    # Koordinaten als (x,y) Tupel speichern (hashbar)
    our_best_set = {(p.x, p.y) for p in our_best}
    opp_best_set = {(p.x, p.y) for p in opp_best}

    return NodeContext(
        our_swarms=our_swarms,
        opp_swarms=opp_swarms,
        our_swarm_val=our_best_val,
        opp_swarm_val=opp_best_val,
        our_best_swarm=our_best_set,
        opp_best_swarm=opp_best_set,
        our_num_swarms=len(our_swarms),
        opp_num_swarms=len(opp_swarms),
    )


# ============================================================================
# Evaluierung mit Context
# ============================================================================

def evaluate_with_context(
    game_state: GameState,
    ctx: NodeContext,
    our_team: TeamEnum,
    opp_team: TeamEnum
) -> float:
    """Schnelle Evaluierung mit vorberechneten Schwarm-Daten."""

    # Terminal Check
    if ctx.our_num_swarms == 0:
        return -WIN_SCORE
    if ctx.opp_num_swarms == 0:
        return WIN_SCORE

    if game_state.turn >= 60:
        if ctx.our_swarm_val > ctx.opp_swarm_val:
            return WIN_SCORE
        elif ctx.opp_swarm_val > ctx.our_swarm_val:
            return -WIN_SCORE

    value = 0.0

    # Schwarmwert
    value += (ctx.our_swarm_val - ctx.opp_swarm_val) * 18.0

    # Anzahl Schwärme
    value -= (ctx.our_num_swarms - 1) * 4.0
    value += (ctx.opp_num_swarms - 1) * 4.0

    # Schwarm-Zentren (ctx.our_best_swarm enthält (x,y) Tupel)
    if ctx.our_best_swarm:
        our_cx = sum(p[0] for p in ctx.our_best_swarm) / len(ctx.our_best_swarm)
        our_cy = sum(p[1] for p in ctx.our_best_swarm) / len(ctx.our_best_swarm)
    else:
        our_cx, our_cy = 4.5, 4.5

    if ctx.opp_best_swarm:
        opp_cx = sum(p[0] for p in ctx.opp_best_swarm) / len(ctx.opp_best_swarm)
        opp_cy = sum(p[1] for p in ctx.opp_best_swarm) / len(ctx.opp_best_swarm)
    else:
        opp_cx, opp_cy = 4.5, 4.5

    # Board-Durchlauf für Material, Distanz, Isolation
    our_material = 0
    opp_material = 0
    our_isolated = 0
    opp_isolated = 0
    our_dist = 0.0
    opp_dist = 0.0

    for x, row in enumerate(game_state.board.map):
        for y, ft in enumerate(row):
            team = ft.get_team()
            if team is None:
                continue

            val = ft.get_value()

            if team == our_team:
                our_material += val
                if (x, y) not in ctx.our_best_swarm:
                    dx = x - our_cx
                    dy = y - our_cy
                    our_dist += (dx * dx + dy * dy) ** 0.5
                    our_isolated += val
            else:
                opp_material += val
                if (x, y) not in ctx.opp_best_swarm:
                    dx = x - opp_cx
                    dy = y - opp_cy
                    opp_dist += (dx * dx + dy * dy) ** 0.5
                    opp_isolated += val

    value += (our_material - opp_material) * 2.0
    value -= our_isolated * 3.0
    value += opp_isolated * 3.0
    value -= our_dist * 0.7
    value += opp_dist * 0.7

    return value


# ============================================================================
# Move Ordering
# ============================================================================

class MoveOrderer:
    def __init__(self):
        self.killer_moves: list[list[Move | None]] = [[None, None] for _ in range(30)]
        self.history: dict[tuple, int] = {}

    def update_killer(self, move: Move, depth: int) -> None:
        if depth < len(self.killer_moves):
            killers = self.killer_moves[depth]
            if move != killers[0]:
                killers[1] = killers[0]
                killers[0] = move

    def update_history(self, move: Move, depth: int) -> None:
        key = (move.start.x, move.start.y, int(move.direction))
        self.history[key] = self.history.get(key, 0) + depth * depth

    def order_moves_fast(
        self,
        moves: list[Move],
        depth: int,
        tt_move: Move | None,
        our_positions: set[Coordinate],
        opp_positions: set[Coordinate],
        board
    ) -> list[Move]:
        """Schnelles Move Ordering ohne extra swarms_of_team Aufrufe."""
        if not moves:
            return moves

        killers = self.killer_moves[depth] if depth < len(self.killer_moves) else [None, None]

        def priority(move: Move) -> float:
            # TT-Move
            if tt_move is not None and move == tt_move:
                return 100000.0

            score = 0.0

            # Killer
            if move in killers:
                score += 5000.0

            # History
            key = (move.start.x, move.start.y, int(move.direction))
            score += self.history.get(key, 0) * 0.1

            # Zielposition berechnen
            target = move.start
            direction_vec = move.direction.to_vector()
            for _ in range(10):
                next_pos = target.add_vector(direction_vec)
                if not (0 <= next_pos.x < 10 and 0 <= next_pos.y < 10):
                    break
                if board.get_field(next_pos).get_team() is not None:
                    break
                target = next_pos

            # Fängt Gegner?
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nx, ny = target.x + dx, target.y + dy
                    if 0 <= nx < 10 and 0 <= ny < 10:
                        if (nx, ny) in opp_positions:
                            score += 50.0

            # Verbindet mit Schwarm?
            if (target.x, target.y) in our_positions:
                score += 30.0

            # Zentrum
            score -= (abs(target.x - 4.5) + abs(target.y - 4.5)) * 2.0

            return score

        return sorted(moves, key=priority, reverse=True)


# ============================================================================
# Alpha-Beta Suche
# ============================================================================

class AlphaBetaSearch:
    def __init__(self, our_team: TeamEnum):
        self.our_team = our_team
        self.opp_team = our_team.opponent()
        self.start_time = 0.0
        self.nodes_searched = 0
        self.tt_hits = 0

        self.tt: dict[int, tuple[float, int, int, Move | None]] = {}
        self.move_orderer = MoveOrderer()
        self.eval_cache: dict[int, float] = {}

    def is_timeout(self) -> bool:
        return time.time() - self.start_time >= TIME_LIMIT

    def check_timeout(self) -> None:
        if self.is_timeout():
            raise TimeoutException()

    def alpha_beta(
        self,
        game_state: GameState,
        state_hash: int,
        depth: int,
        alpha: float,
        beta: float,
        maximizing: bool,
        is_pv: bool,
    ) -> tuple[float, Move | None]:
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
                    return tt_score, tt_move

        # Baue Context einmal für diesen Knoten
        ctx = build_node_context(game_state, self.our_team, self.opp_team)

        # Terminal Check mit Context
        if ctx.our_num_swarms == 0:
            return -WIN_SCORE + (60 - depth), None
        if ctx.opp_num_swarms == 0:
            return WIN_SCORE - (60 - depth), None

        if game_state.turn >= 60:
            if ctx.our_swarm_val > ctx.opp_swarm_val:
                return WIN_SCORE - (60 - depth), None
            elif ctx.opp_swarm_val > ctx.our_swarm_val:
                return -WIN_SCORE + (60 - depth), None

        # Leaf
        if depth == 0:
            if state_hash in self.eval_cache:
                return self.eval_cache[state_hash], None
            score = evaluate_with_context(game_state, ctx, self.our_team, self.opp_team)
            self.eval_cache[state_hash] = score
            return score, None

        moves = game_state.possible_moves()
        if not moves:
            score = evaluate_with_context(game_state, ctx, self.our_team, self.opp_team)
            return score, None

        # Positionen für Move Ordering aus Context (als Tupel)
        our_positions = set()
        for s in ctx.our_swarms:
            our_positions.update((p.x, p.y) for p in s)
        opp_positions = set()
        for s in ctx.opp_swarms:
            opp_positions.update((p.x, p.y) for p in s)

        moves = self.move_orderer.order_moves_fast(
            moves, depth, tt_move, our_positions, opp_positions, game_state.board
        )

        best_move = moves[0]
        moves_searched = 0

        if maximizing:
            max_eval = -INF
            for move in moves:
                self.check_timeout()
                new_state = game_state.perform_move(move)
                new_hash = zobrist_update_move(
                    state_hash, game_state.turn, move, game_state, new_state
                )

                # LMR
                reduction = 0
                if depth >= LMR_DEPTH_LIMIT and moves_searched >= LMR_MOVE_LIMIT and not is_pv:
                    reduction = 1

                if reduction > 0:
                    eval_score, _ = self.alpha_beta(
                        new_state, new_hash, depth - 1 - reduction,
                        alpha, beta, False, False
                    )
                    if eval_score > alpha:
                        eval_score, _ = self.alpha_beta(
                            new_state, new_hash, depth - 1,
                            alpha, beta, False, False
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
                    self.move_orderer.update_history(move, depth)

                if beta <= alpha:
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
                new_hash = zobrist_update_move(
                    state_hash, game_state.turn, move, game_state, new_state
                )

                reduction = 0
                if depth >= LMR_DEPTH_LIMIT and moves_searched >= LMR_MOVE_LIMIT and not is_pv:
                    reduction = 1

                if reduction > 0:
                    eval_score, _ = self.alpha_beta(
                        new_state, new_hash, depth - 1 - reduction,
                        alpha, beta, True, False
                    )
                    if eval_score < beta:
                        eval_score, _ = self.alpha_beta(
                            new_state, new_hash, depth - 1,
                            alpha, beta, True, False
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

            if min_eval <= alpha_orig:
                tt_flag = TT_UPPER
            elif min_eval >= beta:
                tt_flag = TT_LOWER
            else:
                tt_flag = TT_EXACT
            self.tt[state_hash] = (min_eval, depth, tt_flag, best_move)

            return min_eval, best_move

    def iterative_deepening(self, game_state: GameState) -> Move:
        self.start_time = time.time()
        self.nodes_searched = 0
        self.tt_hits = 0

        moves = game_state.possible_moves()
        if len(moves) == 1:
            return moves[0]

        best_move = moves[0]
        best_score = -INF

        current_team = RulesEngine.get_team_on_turn(game_state.turn)
        maximizing = current_team == self.our_team

        state_hash = compute_zobrist_full(game_state)
        depth = 1

        asp_window = 50.0

        while depth <= 30 and not self.is_timeout():
            try:
                if depth >= 4 and abs(best_score) < WIN_SCORE - 1000:
                    alpha = best_score - asp_window
                    beta = best_score + asp_window
                else:
                    alpha, beta = -INF, INF

                score, move = self.alpha_beta(
                    game_state, state_hash, depth, alpha, beta, maximizing, True
                )

                if score <= alpha or score >= beta:
                    score, move = self.alpha_beta(
                        game_state, state_hash, depth, -INF, INF, maximizing, True
                    )

                if move is not None:
                    best_move = move
                    best_score = score

                elapsed = time.time() - self.start_time
                nps = int(self.nodes_searched / elapsed) if elapsed > 0 else 0
                print(f"d{depth}: {score:.0f} | {self.nodes_searched}n {self.tt_hits}h {nps}nps {elapsed:.2f}s")

                if abs(score) >= WIN_SCORE - 100:
                    break

                depth += 1

            except TimeoutException:
                break

        return best_move


# ============================================================================
# Client Handler
# ============================================================================

class Logic(IClientHandler):
    def __init__(self):
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
    print("Bot v2 - Inkrementelles Zobrist + Schwarm-Cache")
    Starter(Logic())
