import random
from socha import FieldType, TeamEnum, Move, RulesEngine, GameState
from socha.api.networking.game_client import IClientHandler
from socha.starter import Starter


class Logic(IClientHandler):
    game_state: GameState
    MAX_DEPTH = 2

    def get_winner_local(self, state: GameState) -> TeamEnum:
        found_red = False
        found_blue = False

        for row in state.board.map:
            for f in row:
                t = f.get_team()
                if t == TeamEnum.One:
                    found_red = True
                elif t == TeamEnum.Two:
                    found_blue = True

        if not found_red and found_blue:
            return TeamEnum.Two
        if not found_blue and found_red:
            return TeamEnum.One
        return None

    def on_update(self, state: GameState) -> None:
        self.game_state = state

    # --- Bewertungsfunktion ----
    def eval_state(self, state: GameState, team: TeamEnum) -> float:
        # check win
        winner = self.get_winner_local(state)
        if winner == team:
            return 1e9
        if winner == team.opponent():
            return -1e9

        # Material: eigene - gegnerische Fischwerte
        my_score = 0
        opp_score = 0
        for row in state.board.map:
            for ft in row:
                t = ft.get_team()
                if t is None:
                    continue
                v = ft.get_value()
                if t == team:
                    my_score += v
                else:
                    opp_score += v

        return my_score - opp_score

    # --- MiniMax + AlphaBeta ----
    def minimax(
        self,
        state: GameState,
        depth: int,
        alpha: float,
        beta: float,
        maximizing: bool,
        me: TeamEnum,
    ) -> float:

        winner = self.get_winner_local(state)
        if depth == 0 or winner is not None:
            return self.eval_state(state, me)

        moves = state.possible_moves()
        if not moves:
            return self.eval_state(state, me)

        if maximizing:
            best = -1e18
            for mv in moves:
                sim = state.deepcopy()
                sim.perform_move_mut(mv)
                val = self.minimax(sim, depth - 1, alpha, beta, False, me)
                best = max(best, val)
                alpha = max(alpha, val)
                if beta <= alpha:
                    break
            return best

        else:
            best = +1e18
            for mv in moves:
                sim = state.deepcopy()
                sim.perform_move_mut(mv)
                val = self.minimax(sim, depth - 1, alpha, beta, True, me)
                best = min(best, val)
                beta = min(beta, val)
                if beta <= alpha:
                    break
            return best

    # --- Move-Auswahl ----
    def calculate_move(self) -> Move:
        moves = self.game_state.possible_moves()
        me = RulesEngine.get_team_on_turn(self.game_state.turn)

        best_val = -1e18
        best_moves = []

        for mv in moves:
            sim = self.game_state.deepcopy()
            sim.perform_move_mut(mv)

            score = self.minimax(
                sim, self.MAX_DEPTH - 1, -1e18, 1e18, False, me  # Gegner am Zug
            )

            if score > best_val:
                best_val = score
                best_moves = [mv]
            elif score == best_val:
                best_moves.append(mv)

        return random.choice(best_moves)


if __name__ == "__main__":
    Starter(logic=Logic())
    
