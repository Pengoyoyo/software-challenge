from socha import GameState, Move, RulesEngine, TeamEnum
from socha.api.networking.game_client import IClientHandler
from socha.starter import Starter

from cython_core.search import iterative_deepening, init_search

TIME_LIMIT = 1.8


class CythonLogic(IClientHandler):
    def __init__(self):
        self.game_state: GameState | None = None
        self.our_team: TeamEnum | None = None
        self.our_team_int: int = 0

        init_search()
        print("Cython-Module geladen")

    def on_update(self, game_state: GameState) -> None:
        self.game_state = game_state
        if self.our_team is None:
            self.our_team = RulesEngine.get_team_on_turn(game_state.turn)
            self.our_team_int = 1 if int(self.our_team) == 0 else 2
            print(f"Team: {self.our_team} (int: {self.our_team_int})")

    def calculate_move(self) -> Move:
        assert self.game_state is not None

        print(f"\n=== Zug {self.game_state.turn + 1} ===")

        best_move = iterative_deepening(self.game_state, self.our_team_int, TIME_LIMIT)

        print(f"-> {best_move.start} {best_move.direction}")
        return best_move

    def on_game_over(self, result) -> None:
        print(f"\n=== Ende ===\n{result}")


if __name__ == "__main__":
    print("=" * 50)
    print("Cython-optimierter Bot gestartet")
    print("=" * 50)
    Starter(CythonLogic())
