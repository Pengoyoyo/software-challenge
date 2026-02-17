import time
from typing import Callable

from socha import GameState, Move, RulesEngine, TeamEnum, Coordinate
from socha.api.networking.game_client import IClientHandler
from socha.starter import Starter


TIME_LIMIT = 1.8
INF = 1_000_000
WIN_SCORE = 100_000

TT_EXACT = 0
TT_LOWER = 1
TT_UPPER = 2


class TimeoutException(Exception):
    pass


def groesster_schwarm(
    game_state: GameState, team: TeamEnum
) -> tuple[int, list[Coordinate]]:
    max_value = 0
    groesster = []

    for schwarm in RulesEngine.swarms_of_team(game_state.board, team):
        value = sum(game_state.board.get_field(pos).get_value() for pos in schwarm)
        if value > max_value:
            max_value = value
            groesster = schwarm

    return max_value, groesster


def anzahl_schwaerme(game_state: GameState, team: TeamEnum) -> int:
    return len(RulesEngine.swarms_of_team(game_state.board, team))


def material(game_state: GameState, team: TeamEnum) -> int:
    score = 0
    for row in game_state.board.map:
        for ft in row:
            t = ft.get_team()
            if t == team:
                score += ft.get_value()
    return score


def einzelfische(game_state: GameState, team: TeamEnum) -> int:
    value = 0
    for schwarm in RulesEngine.swarms_of_team(game_state.board, team):
        if len(schwarm) == 1:
            value += game_state.board.get_field(schwarm[0]).get_value()
    return value


def mean(lst: list[int]) -> float:
    return sum(lst) / len(lst) if lst else 0


def distanz_zum_schwarm(game_state: GameState, team: TeamEnum) -> float:
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
    team_one_swarms = RulesEngine.swarms_of_team(game_state.board, TeamEnum.One)
    team_two_swarms = RulesEngine.swarms_of_team(game_state.board, TeamEnum.Two)

    if not team_one_swarms:
        return TeamEnum.Two
    if not team_two_swarms:
        return TeamEnum.One

    if game_state.turn >= 60:
        score_one = groesster_schwarm(game_state, TeamEnum.One)[0]
        score_two = groesster_schwarm(game_state, TeamEnum.Two)[0]
        if score_one > score_two:
            return TeamEnum.One
        elif score_two > score_one:
            return TeamEnum.Two

    return None


def evaluate(game_state: GameState, our_team: TeamEnum, opp_team: TeamEnum) -> float:
    winner = check_winner(game_state)
    if winner == our_team:
        return WIN_SCORE
    elif winner == opp_team:
        return -WIN_SCORE

    value = 0.0

    our_schwarm_value = groesster_schwarm(game_state, our_team)[0]
    opp_schwarm_value = groesster_schwarm(game_state, opp_team)[0]
    value += (our_schwarm_value - opp_schwarm_value) * 17.74

    our_num_swarms = anzahl_schwaerme(game_state, our_team)
    opp_num_swarms = anzahl_schwaerme(game_state, opp_team)
    value -= (our_num_swarms - 1) * 3.0
    value += (opp_num_swarms - 1) * 3.0

    our_material = material(game_state, our_team)
    opp_material = material(game_state, opp_team)
    value += (our_material - opp_material) * 2.0

    our_einzelfische = einzelfische(game_state, our_team)
    opp_einzelfische = einzelfische(game_state, opp_team)
    value -= our_einzelfische * 4.0
    value += opp_einzelfische * 4.0

    our_dist = distanz_zum_schwarm(game_state, our_team)
    opp_dist = distanz_zum_schwarm(game_state, opp_team)
    value -= our_dist * 0.63
    value += opp_dist * 0.63

    return value


def order_moves(
    game_state: GameState, moves: list[Move], maximizing: bool
) -> list[Move]:
    current_team = RulesEngine.get_team_on_turn(game_state.turn)
    opp_team = current_team.opponent()
    board = game_state.board

    our_swarm_positions: set[Coordinate] = set()
    for schwarm in RulesEngine.swarms_of_team(board, current_team):
        our_swarm_positions.update(schwarm)

    opp_positions: set[Coordinate] = set()
    for schwarm in RulesEngine.swarms_of_team(board, opp_team):
        opp_positions.update(schwarm)

    def move_score(move: Move) -> float:
        score = 0.0

        target = move.start
        for _ in range(4):
            next_pos = target.move(move.direction)
            if not (0 <= next_pos.x < 10 and 0 <= next_pos.y < 10):
                break
            field = board.get_field(next_pos)
            if field.get_team() is not None:
                break
            target = next_pos

        for dx in [-1, 0, 1]:
            for dy in [-1, 0, 1]:
                if dx == 0 and dy == 0:
                    continue
                neighbor = Coordinate(target.x + dx, target.y + dy)
                if neighbor in opp_positions:
                    score += 10

        if target in our_swarm_positions:
            score += 5

        center_dist = abs(target.x - 4.5) + abs(target.y - 4.5)
        score -= center_dist * 0.5

        return score

    return sorted(moves, key=move_score, reverse=True)


class AlphaBetaSearch:
    def __init__(self, our_team: TeamEnum):
        self.our_team = our_team
        self.opp_team = our_team.opponent()
        self.start_time = 0.0
        self.time_limit = TIME_LIMIT
        self.nodes_searched = 0
        self.tt_hits = 0
        self.transposition_table: dict[int, tuple[float, int, int, Move | None]] = {}

    def is_timeout(self) -> bool:
        return time.time() - self.start_time >= self.time_limit

    def check_timeout(self) -> None:
        if self.is_timeout():
            raise TimeoutException()

    def get_state_hash(self, game_state: GameState) -> int:
        parts = []
        for x, row in enumerate(game_state.board.map):
            for y, ft in enumerate(row):
                team = ft.get_team()
                if team is not None:
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
        self.check_timeout()
        self.nodes_searched += 1

        alpha_orig = alpha
        state_hash = self.get_state_hash(game_state)

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

        winner = check_winner(game_state)
        if winner == self.our_team:
            return WIN_SCORE - (60 - depth), None
        elif winner == self.opp_team:
            return -WIN_SCORE + (60 - depth), None

        if depth == 0:
            return evaluate(game_state, self.our_team, self.opp_team), None

        moves = game_state.possible_moves()
        if not moves:
            return evaluate(game_state, self.our_team, self.opp_team), None

        tt_best_move = None
        if state_hash in self.transposition_table:
            tt_best_move = self.transposition_table[state_hash][3]

        if tt_best_move is not None and tt_best_move in moves:
            moves = [tt_best_move] + [m for m in moves if m != tt_best_move]
        elif depth >= 2:
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
                    break

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
                    break

            if min_eval <= alpha_orig:
                tt_flag = TT_UPPER
            elif min_eval >= beta:
                tt_flag = TT_LOWER
            else:
                tt_flag = TT_EXACT
            self.transposition_table[state_hash] = (min_eval, depth, tt_flag, best_move)

            return min_eval, best_move

    def iterative_deepening(self, game_state: GameState) -> Move:
        self.start_time = time.time()
        self.nodes_searched = 0

        moves = game_state.possible_moves()
        if len(moves) == 1:
            return moves[0]

        best_move = moves[0]
        best_score = -INF

        current_team = RulesEngine.get_team_on_turn(game_state.turn)
        maximizing = current_team == self.our_team

        depth = 1
        max_depth = 20

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
            print(f"Spiele als Team: {self.our_team}")

    def calculate_move(self) -> Move:
        assert self.game_state is not None
        assert self.searcher is not None

        print(f"\n=== Zug {self.game_state.turn + 1} ===")

        best_move = self.searcher.iterative_deepening(self.game_state)

        print(f"Gewählter Zug: {best_move.start} -> {best_move.direction}")

        return best_move

    def on_game_over(self, result) -> None:
        print(f"\n=== Spielende ===")
        print(f"Ergebnis: {result}")


if __name__ == "__main__":
    print("Starte Alpha-Beta Pruning Bot...")
    Starter(AlphaBetaLogic())
